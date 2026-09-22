from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.testgen.project_summary.constants import COMPILE_FACTS_FILENAME


def _read_text(path: Path, limit: int = 80_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _extract_pom_coords(pom_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    m = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", pom_text)
    if m:
        out["artifactId"] = m.group(1).strip()
    m = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", pom_text)
    if m:
        out["groupId"] = m.group(1).strip()
    m = re.search(r"<version>\s*([^<]+?)\s*</version>", pom_text)
    if m:
        out["version"] = m.group(1).strip()
    m = re.search(r"<java\.version>\s*([^<]+?)\s*</java\.version>", pom_text)
    if m:
        out["java_version"] = m.group(1).strip()
    return out


def _list_modules(repo: Path, module: Optional[str]) -> List[str]:
    if module:
        return [module]
    mods: List[str] = []
    root_pom = repo / "pom.xml"
    if root_pom.exists():
        text = _read_text(root_pom)
        for m in re.finditer(r"<module>\s*([^<]+?)\s*</module>", text):
            mods.append(m.group(1).strip())
    return mods[:40]


def _discover_nearby_api_repos(repo: Path) -> List[str]:
    candidates: List[Path] = []
    seen: set[str] = set()

    def has_java_sources(path: Path) -> bool:
        if (path / "src" / "main" / "java").is_dir():
            return True
        try:
            for child in path.iterdir():
                if child.is_dir() and (child / "src" / "main" / "java").is_dir():
                    return True
        except OSError:
            return False
        return False

    for base in [repo.parent, repo.parent.parent, Path.home()]:
        if not base or not base.exists():
            continue
        try:
            for child in base.iterdir():
                child_name = child.name.lower()
                if child.is_dir() and (
                    child_name == "api"
                    or child_name.endswith("-api")
                    or "-api-" in child_name
                    or child_name.endswith("_api")
                    or "_api_" in child_name
                ) and has_java_sources(child):
                    resolved = str(child.resolve())
                    if resolved not in seen:
                        seen.add(resolved)
                        candidates.append(child)
        except OSError:
            continue
    return [str(p.resolve()) for p in candidates[:8]]


def _sample_test_files(repo: Path, limit: int = 30) -> List[Path]:
    out: List[Path] = []
    for path in repo.rglob("*Test.java"):
        if "target" in path.parts or ".uta_cache" in path.parts:
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _infer_test_patterns(repo: Path) -> Dict[str, Any]:
    files = _sample_test_files(repo)
    patterns = Counter()
    for path in files:
        body = _read_text(path, limit=20_000)
        lowered = body.lower()
        if "org.junit.test" in body or "@test" in lowered:
            patterns["junit4"] += 1
        if "mockitojunitrunner" in lowered:
            patterns["mockito_runner"] += 1
        if "@mock" in lowered or "mock(" in lowered:
            patterns["mockito_usage"] += 1
        if "setaccessible(true)" in lowered or ".getdeclaredfield(" in lowered or ".getdeclaredmethod(" in lowered:
            patterns["reflection"] += 1
        if "proxy.newproxyinstance" in lowered or " invocationhandler" in lowered:
            patterns["proxy"] += 1
        if "assertthat(" in lowered:
            patterns["assertj"] += 1
    return {
        "sampled_files": len(files),
        "patterns": patterns,
    }


def _build_test_generation_guidance_markdown(repo_path: str, module: Optional[str]) -> str:
    repo = Path(repo_path)
    nearby_api_repos = _discover_nearby_api_repos(repo)
    observed = _infer_test_patterns(repo)
    patterns: Counter = observed["patterns"]
    compile_facts_path = repo / ".uta_cache" / "context" / COMPILE_FACTS_FILENAME
    compile_facts: List[str] = []
    if compile_facts_path.exists():
        for line in _read_text(compile_facts_path, limit=20_000).splitlines():
            line = line.strip()
            if line.startswith("- "):
                compile_facts.append(line[2:].strip())

    lines = [
        "# Test Generation Guidance",
        "",
        "_UTA-generated cached guidance for test generation. Reuse this across runs before rediscovering the same repo constraints._",
        "",
        "## Source-of-Truth Lookup Order",
        "1. Target source file and nearby source files in the same module",
        "2. `.uta_cache/context/class_map.md`, `dependency_map.md`, `process_flows.md`, `call_graph.md`",
        "3. Real source and existing tests in the current repo",
    ]
    if nearby_api_repos:
        lines.append("4. Nearby sibling API/source repos:")
        for path in nearby_api_repos:
            lines.append(f"   - `{path}`")
        lines.append("5. Only as a last resort, compiled artifacts or local Maven cache")
    else:
        lines.append("4. Nearby sibling API/source repos if they exist and share the package prefix")
        lines.append("5. Only as a last resort, compiled artifacts or local Maven cache")

    lines.extend(
        [
            "",
            "## Test Construction Constraints",
            "- Verify exact numeric types, enum constants, and value-object constructors before finalizing assertions.",
            "- Prefer bounded edits and avoid giant one-shot rewrites for large or dependency-heavy tests.",
            "- In batch mode, write all requested files first, then do one shared compile/test validation pass.",
            "- When the production code uses a pager helper (`PagerCollector`, `collectList`, `collectAll`, page loops), mocked page-loader calls must terminate: return data for early pages and an empty final page instead of a constant non-empty list.",
            "- Prefer source-of-truth definitions from source repos over guessing from jars, `.class` files, or error text.",
            "- Do not unpack jars, run decompilers, or inspect `.class` files when sibling/current source can answer the API question.",
            "",
            "## Observed Repo Test Patterns",
            f"- Sampled test files: `{observed['sampled_files']}`",
            f"- JUnit 4 style seen: `{'yes' if patterns['junit4'] else 'no'}`",
            f"- Mockito runner usage seen: `{'yes' if patterns['mockito_runner'] else 'no'}`",
            f"- Mockito usage seen: `{'yes' if patterns['mockito_usage'] else 'no'}`",
            f"- Reflection-heavy tests seen: `{'yes' if patterns['reflection'] else 'no'}`",
            f"- Proxy/stub patterns seen: `{'yes' if patterns['proxy'] else 'no'}`",
            "",
            "## How To Use This File",
            "- Read this guidance before exploring jars or local Maven cache.",
            "- Reuse these constraints across retries instead of rediscovering them from compile failures.",
            "- When a missing type is found in a sibling source repo, treat that source as authoritative.",
            "",
        ]
    )
    if compile_facts:
        lines.extend(
            [
                "## Cached Compile-Critical Facts",
                "- Reuse these concrete facts before rediscovering them from a failed compile.",
            ]
        )
        lines.extend(f"- {fact}" for fact in compile_facts[:20])
        lines.append("")
    return "\n".join(lines)
