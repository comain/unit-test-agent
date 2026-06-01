from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.engine.project_summary import ProjectSummaryArtifacts
from uta.language.python.context_builder import PythonContextBuilder


class PythonProjectSummaryProvider:
    language = "python"

    def __init__(self, repo_path: str | Path, *, max_files: int = 500):
        self.repo_path = Path(repo_path)
        self.max_files = max_files

    def sync(self) -> ProjectSummaryArtifacts:
        from uta.engine import project_summary_artifacts as artifacts

        repo = self.repo_path
        ctx_dir = repo / ".uta_cache" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        index = PythonContextBuilder(repo).export_project_context(
            output_dir=repo / ".uta_cache" / "python_context",
            max_files=self.max_files,
        )

        context_path = ctx_dir / artifacts.CONTEXT_SUMMARY_FILENAME
        context_path.write_text(_build_python_context_summary(repo, index), encoding="utf-8")
        artifacts.logger.info("Wrote %s", context_path)

        guidance_path = ctx_dir / artifacts.TEST_GUIDANCE_FILENAME
        guidance_path.write_text(_build_python_test_guidance(repo, index), encoding="utf-8")
        artifacts.logger.info("Wrote %s", guidance_path)

        repo_summary = repo / artifacts.REPO_SUMMARY_FILENAME
        if not repo_summary.exists() or artifacts._is_uta_generated_summary(repo_summary):
            repo_summary.write_text(_build_python_repo_summary(repo, index), encoding="utf-8")
            artifacts.logger.info("Created or refreshed %s", repo_summary)
        else:
            artifacts.logger.info("Leaving existing %s unchanged (not UTA-generated)", repo_summary)

        return ProjectSummaryArtifacts(
            repo_summary_abs=str(repo_summary.resolve()),
            context_summary_abs=str(context_path.resolve()),
            test_guidance_abs=str(guidance_path.resolve()),
            compile_facts_abs=str((ctx_dir / artifacts.COMPILE_FACTS_FILENAME).resolve()),
        )


def _marker_paths(repo: Path) -> List[str]:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "noxfile.py")
    return [marker for marker in markers if (repo / marker).exists()]


def _test_markers(repo: Path) -> Dict[str, Any]:
    files = []
    frameworks: Counter[str] = Counter()
    for root in ("tests", "test"):
        test_root = repo / root
        if not test_root.exists():
            continue
        for path in sorted(test_root.rglob("*.py"))[:40]:
            if "__pycache__" in path.parts:
                continue
            files.append(path.relative_to(repo).as_posix())
            body = path.read_text(encoding="utf-8", errors="replace")[:20_000]
            if "pytest" in body or body.count("assert ") >= 2:
                frameworks["pytest"] += 1
            if "unittest" in body or "TestCase" in body:
                frameworks["unittest"] += 1
    return {"files": files[:40], "frameworks": dict(frameworks)}


def _symbol_counts(index: Dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for symbol in index.get("symbols") or []:
        counts[str(symbol.get("kind") or "unknown")] += 1
    return counts


def _build_python_context_summary(repo: Path, index: Dict[str, Any]) -> str:
    markers = _marker_paths(repo)
    tests = _test_markers(repo)
    counts = _symbol_counts(index)
    selection = index.get("selection") or {}
    lines = [
        "# UTA project summary (machine-generated)",
        "",
        "Read this file for Python layout and coarse parser statistics before writing tests.",
        "",
        f"- Repository root: `{repo.resolve()}`",
        "- Language: `python`",
        f"- Parsed production files: `{len(index.get('files') or [])}`",
        f"- Selection cap: `{selection.get('max_files', '')}`",
        f"- Skipped by cap: `{selection.get('skipped_count', 0)}`",
        "",
        "## Packaging And Tooling Markers",
    ]
    if markers:
        lines.extend(f"- `{marker}`" for marker in markers)
    else:
        lines.append("- No standard Python packaging marker found.")
    lines.extend(["", "## Parsed Symbol Overview"])
    if counts:
        for kind, count in sorted(counts.items()):
            lines.append(f"- **{kind}** count: {count}")
    else:
        lines.append("- No symbols parsed.")
    lines.extend(["", "## Test Layout"])
    if tests["files"]:
        lines.extend(f"- `{path}`" for path in tests["files"][:20])
    else:
        lines.append("- No tests found under `tests/` or `test/`.")
    if tests["frameworks"]:
        lines.extend(["", "## Observed Test Framework Signals"])
        for name, count in sorted(tests["frameworks"].items()):
            lines.append(f"- `{name}`: {count} sampled files")
    lines.extend(
        [
            "",
            "## Related UTA context files",
            "",
            "- `.uta_cache/python_context/index.json`",
            "- per-target Python context markdown/json under `.uta_cache/python_context/`",
            "",
        ]
    )
    return "\n".join(lines)


def _build_python_test_guidance(repo: Path, index: Dict[str, Any]) -> str:
    tests = _test_markers(repo)
    frameworks = tests["frameworks"]
    preferred = "pytest" if frameworks.get("pytest", 0) >= frameworks.get("unittest", 0) else "unittest"
    lines = [
        "# Test Generation Guidance",
        "",
        "_UTA-generated cached guidance for Python test generation._",
        "",
        "## Source-of-Truth Lookup Order",
        "1. Target source file and sibling modules in the same package",
        "2. `.uta_cache/python_context/index.json` and target context markdown/json",
        "3. Real existing tests in this repository",
        "4. Packaging metadata and dependency files (`pyproject.toml`, `setup.py`, `requirements*.txt`, `tox.ini`)",
        "",
        "## Test Construction Constraints",
        f"- Prefer `{preferred}` style when adding new Python tests unless the target package clearly uses another style.",
        "- Keep generated tests under the configured Python test roots (`tests`, `tests/uta_generated`).",
        "- Use repo-local imports and avoid importing production modules only to inspect them during planning.",
        "- Preserve Python 2 compatibility for Python 2 targets: avoid f-strings, annotations, and Python 3-only syntax.",
        "- Run coverage and mutation checks through UTA enforcement scripts so line-diff gates use one evidence contract.",
        "",
        "## Parsed Scope",
        f"- Parsed production files: `{len(index.get('files') or [])}`",
        f"- Parsed symbols: `{len(index.get('symbols') or [])}`",
        "",
    ]
    return "\n".join(lines)


def _build_python_repo_summary(repo: Path, index: Dict[str, Any]) -> str:
    from uta.engine import project_summary_artifacts as artifacts

    return "\n".join(
        [
            artifacts.UTA_GENERATED_MARKER,
            "",
            "# Project overview (UTA)",
            "",
            "This file was generated by the Unit Test Agent. Replace or edit it to document",
            "architecture, domain vocabulary, and test conventions for your team.",
            "",
            "**Language**: `python`",
            f"**Parsed production files**: {len(index.get('files') or [])}",
            f"**Parsed symbols**: {len(index.get('symbols') or [])}",
            "",
            "For structured stats and package breakdown, read:",
            f"`{repo / '.uta_cache' / 'context' / artifacts.CONTEXT_SUMMARY_FILENAME}`",
            "",
        ]
    )
