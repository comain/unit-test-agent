"""Project-level documentation for agent prompts.

Design: a repo-root `.uta_summary.md` holds human- or tool-written overview; UTA also
writes `.uta_cache/context/project_summary.md` after parsing (Maven + graph stats).

Baseline (when OpenCode is running): if ``.uta_summary.md`` is missing or tiny, UTA sends
the OpenCode slash command ``/init`` on the session, then harvests ``AGENTS.md`` /
``CLAUDE.md`` (or ``.opencode/AGENTS.md``) into ``.uta_summary.md`` when present.

If there is no session or ``/init`` leaves no artifact, optional ``UTA_OPENCODE_INIT_COMMAND``
shell fallback may still run.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import textwrap
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from uta.language.java.parse.models import CodeGraph

logger = logging.getLogger("uta")

REPO_SUMMARY_FILENAME = ".uta_summary.md"
CONTEXT_SUMMARY_FILENAME = "project_summary.md"
TEST_GUIDANCE_FILENAME = "test_generation_guidance.md"
OPENCODE_INIT_OUTPUT_FILENAME = "opencode_init_output.md"
SESSION_RETROSPECT_FILENAME = "session_retrospect.md"
COMPILE_FACTS_FILENAME = "compile_fix_facts.md"
STAGE_INTROSPECT_FILENAME = "introspect.md"
UTA_GENERATED_MARKER = "<!-- uta-generated -->"
OPENCODE_INIT_MERGE_HEADER = "<!-- UTA: merged from OpenCode /init output -->\n\n"


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


def _harvest_opencode_init_artifacts(repo: Path) -> bool:
    """If ``.uta_summary.md`` is still small, copy from common ``/init`` outputs."""
    dest = repo / REPO_SUMMARY_FILENAME
    if _has_authoritative_repo_summary(dest):
        return True
    candidates = [
        repo / "AGENTS.md",
        repo / "CLAUDE.md",
        repo / ".opencode" / "AGENTS.md",
        repo / ".opencode" / "init.md",
    ]
    for src in candidates:
        if not src.is_file() or src.stat().st_size < 30:
            continue
        try:
            body = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        dest.write_text(OPENCODE_INIT_MERGE_HEADER + body, encoding="utf-8")
        logger.info("Wrote %s from OpenCode artifact %s", dest, src)
        return True
    return False


def _extract_init_text(
    client: Any,
    session_id: str,
    visited: Optional[set[str]] = None,
) -> str:
    """Extract useful `/init` output, following delegated Task sub-sessions when present."""
    visited = visited or set()
    if session_id in visited:
        return ""
    visited.add(session_id)

    try:
        messages = client.get_messages(session_id)
    except Exception:
        return ""

    chunks: List[str] = []
    delegated: List[str] = []
    for msg in messages:
        info = msg.get("info", {})
        if info.get("sessionID") != session_id:
            continue
        if info.get("role") != "assistant":
            continue
        for part in msg.get("parts", []):
            ptype = part.get("type")
            if ptype == "text":
                text = (part.get("text") or "").strip()
                if text:
                    chunks.append(text)
            elif ptype == "tool":
                state = part.get("state", {}) or {}
                output = (state.get("output") or "").strip()
                if output:
                    chunks.append(output)
                nested = (
                    (state.get("metadata") or {}).get("sessionId")
                    or (part.get("metadata") or {}).get("sessionId")
                )
                if nested:
                    delegated.append(str(nested))
    for nested in delegated:
        child = _extract_init_text(client, nested, visited)
        if child:
            chunks.append(child)
    return "\n\n".join(chunks).strip()


def _is_meaningful_init_output(body: str) -> bool:
    text = body.strip()
    if len(text) < 80:
        return False
    generic_markers = [
        "analyzing the initial command",
        "interpreting `/init` intent",
        "refining the `/init` interpretation",
        "execute /init command",
        "i am initialized and ready to help",
        "lacking context",
    ]
    lowered = text.lower()
    return not any(marker in lowered for marker in generic_markers)


def _write_opencode_init_output(repo: Path, session_id: str, body: str) -> None:
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = ctx_dir / OPENCODE_INIT_OUTPUT_FILENAME
    payload = [
        "# OpenCode /init output",
        "",
        f"- session_id: `{session_id}`",
        "",
        body or "_No textual /init output was captured from the session._",
        "",
    ]
    out.write_text("\n".join(payload), encoding="utf-8")


def write_session_retrospect(repo_path: str, retrospect: Dict[str, Any]) -> str:
    repo = Path(repo_path)
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = ctx_dir / SESSION_RETROSPECT_FILENAME
    hints = retrospect.get("hints") or []
    compile_facts = retrospect.get("compile_facts") or []
    observations = retrospect.get("observations") or []
    lines = [
        "# Session Retrospect",
        "",
        "_UTA-generated retrospective hints extracted from the latest OpenCode session. Use these to improve first-attempt behavior on later runs._",
        "",
        f"- session_id: `{retrospect.get('session_id', '')}`",
        f"- tool_count: `{retrospect.get('tool_count', 0)}`",
        f"- patch_count: `{retrospect.get('patch_count', 0)}`",
        "",
        "## Prompt Improvements",
    ]
    if hints:
        lines.extend([f"- {hint}" for hint in hints])
    else:
        lines.append("- No concrete prompt improvements inferred from this session.")
    lines.extend(["", "## Compile-Critical Facts"])
    if compile_facts:
        lines.extend([f"- {fact}" for fact in compile_facts])
    else:
        lines.append("- No compile-critical API facts captured from this session.")
    lines.extend(["", "## Observations"])
    if observations:
        lines.extend([f"- {obs}" for obs in observations])
    else:
        lines.append("- No notable session observations captured.")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out.resolve())


def _safe_stage_name(stage: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", (stage or "").strip().lower()).strip("_")
    return normalized or "general"


def stage_introspect_path(repo_path: str, stage: str) -> str:
    """Return the per-stage introspection file path."""
    return str(
        Path(repo_path)
        / ".uta_cache"
        / "context"
        / "introspect"
        / _safe_stage_name(stage)
        / STAGE_INTROSPECT_FILENAME
    )


def ensure_stage_introspect_file(repo_path: str, stage: str) -> str:
    path = Path(stage_introspect_path(repo_path, stage))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        stage_name = _safe_stage_name(stage)
        path.write_text(
            "\n".join(
                [
                    f"# Stage Introspect: {stage_name}",
                    "",
                    "_UTA-generated cross-run lessons for this workflow stage. Read this before starting the stage and apply only lessons relevant to the current class._",
                    "",
                    "## Prompt Improvements",
                    "- No prior stage-specific lessons recorded yet.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return str(path.resolve())


def append_stage_introspect(
    repo_path: str,
    stage: str,
    hints: List[str],
    *,
    max_hints: int = 40,
) -> str:
    """Append new unique retrospect hints to a stage-scoped introspect file."""
    path = Path(ensure_stage_introspect_file(repo_path, stage))
    existing = _read_text(path, limit=80_000)
    merged: List[str] = []
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "No prior stage-specific lessons recorded yet." not in stripped:
            hint = stripped[2:].strip()
            if hint and hint not in merged:
                merged.append(hint)

    for hint in hints or []:
        clean = re.sub(r"\s+", " ", str(hint or "").strip())
        if clean and clean not in merged:
            merged.append(clean)
    if len(merged) > max_hints:
        merged = merged[-max_hints:]

    stage_name = _safe_stage_name(stage)
    lines = [
        f"# Stage Introspect: {stage_name}",
        "",
        "_UTA-generated cross-run lessons for this workflow stage. Read this before starting the stage and apply only lessons relevant to the current class._",
        "",
        "## Prompt Improvements",
    ]
    if merged:
        lines.extend(f"- {hint}" for hint in merged)
    else:
        lines.append("- No prior stage-specific lessons recorded yet.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path.resolve())


def merge_compile_fix_facts(repo_path: str, facts: List[str]) -> str:
    repo = Path(repo_path)
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = ctx_dir / COMPILE_FACTS_FILENAME

    merged: List[str] = []
    if out.exists():
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("- "):
                merged.append(line[2:].strip())

    for fact in facts:
        cleaned = fact.strip()
        if cleaned and cleaned not in merged:
            merged.append(cleaned)

    lines = [
        "# Compile Fix Facts",
        "",
        "_UTA-generated compile-critical API facts discovered during prior compile-fix loops. Reuse these before rediscovering the same mismatches._",
        "",
    ]
    if merged:
        lines.extend(f"- {fact}" for fact in merged[:50])
    else:
        lines.append("- No compile-critical facts captured yet.")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out.resolve())


def maybe_run_opencode_init_slash(
    repo_path: str,
    session_id: Optional[str],
    timeout: Optional[int] = None,
) -> bool:
    """Run OpenCode ``/init`` when ``.uta_summary.md`` is missing; harvest AGENTS/CLAUDE into it.

    Returns True if a ``/init`` round was attempted (even if harvest failed).
    """
    from uta.opencode.client import OpenCodeAuthClient as OpenCodeClient
    from uta.config import settings

    repo = Path(repo_path)
    summary = repo / REPO_SUMMARY_FILENAME
    if not settings.opencode_init_slash_enabled:
        logger.info("Skipping OpenCode init bootstrap — disabled by settings")
        return False
    if _has_authoritative_repo_summary(summary):
        return False
    configured_provider = settings.opencode_provider or ""
    configured_model = settings.opencode_model or ""
    if configured_provider and configured_model.startswith(f"{configured_provider}/"):
        configured_model = configured_model.split("/", 1)[1]
    elif "/" in configured_model:
        configured_provider, configured_model = configured_model.split("/", 1)
    to = timeout if timeout is not None else settings.opencode_init_slash_timeout
    client = OpenCodeClient()
    init_session_id = session_id
    created_temp_session = False
    if session_id:
        try:
            init_session_id = client.create_session(
                model_id=settings.opencode_model,
                provider_id=configured_provider or None
            )
            created_temp_session = True
        except Exception as exc:
            logger.warning(
                "Failed to create isolated OpenCode /init session, falling back to main session: %s",
                exc,
            )
            init_session_id = session_id
    if not init_session_id:
        logger.debug("Skipping OpenCode /init — no usable session_id")
        return False

    logger.info(
        "OpenCode init endpoint for project summary on %s session %s (timeout %ss)",
        "isolated" if created_temp_session else "shared",
        init_session_id,
        to,
    )
    try:
        user_info = client.send_message_and_get_user_info(
            init_session_id,
            "Initialize this repository and create AGENTS.md with project-specific guidance.",
            model_id=settings.opencode_model,
        )
    except Exception as exc:
        logger.warning("OpenCode /init message failed: %s", exc)
        if created_temp_session:
            try:
                client.delete_session(init_session_id)
            except Exception:
                pass
        return True

    model = (user_info.get("model") or {})
    client.init_session(
        init_session_id,
        message_id=user_info.get("id", ""),
        provider_id=configured_provider or model.get("providerID", ""),
        model_id=configured_model or model.get("modelID", ""),
    )
    # `/init` often succeeds via side effects (AGENTS/CLAUDE/.opencode files) without a
    # normal assistant text completion. Keep this bootstrap best-effort and bounded so it
    # does not stall the full generation run for many minutes.
    init_wait = to
    deadline = time.time() + init_wait
    event = None
    harvested = _harvest_opencode_init_artifacts(repo)
    while not harvested and time.time() < deadline:
        event = client.latest_completion(init_session_id)
        if event and event.get("type") == "error":
            logger.warning("OpenCode /init session encountered an error event: %s", event)
            break
        time.sleep(5)  # Polling interval
        harvested = _harvest_opencode_init_artifacts(repo)
    init_text = _extract_init_text(client, init_session_id)
    if not _is_meaningful_init_output(init_text):
        init_text = textwrap.dedent(
            f"""\
            _OpenCode init endpoint did not yield meaningful project bootstrap output._

            Observed behavior:
            - session_id: `{init_session_id}`
            - init endpoint did not produce a concrete repo summary artifact
            - output contained no concrete repo summary artifact or actionable bootstrap text

            Recommendation:
            - rely on `.uta_cache/context/project_summary.md`
            - add a manual `.uta_summary.md` for stable repo conventions
            - or configure `UTA_OPENCODE_INIT_COMMAND` for deterministic bootstrap
            """
        ).strip()
    _write_opencode_init_output(repo, init_session_id, init_text)
    if created_temp_session:
        try:
            client.delete_session(init_session_id)
        except Exception:
            logger.debug("Failed to delete isolated OpenCode /init session %s", init_session_id)

    if not harvested and event:
        et = event.get("type")
        if et != "completed":
            logger.warning("OpenCode /init finished with type=%s: %s", et, event)
    if not harvested and not (summary.exists() and summary.stat().st_size > 20):
        logger.info(
            "After /init, %s still missing or empty — generation prompts will rely on "
            "project_summary.md until you add a repo summary or run a shell init command.",
            REPO_SUMMARY_FILENAME,
        )
    return True


def maybe_run_project_init_command(repo_path: str, init_command: Optional[str]) -> bool:
    """If ``.uta_summary.md`` is missing and ``init_command`` is set, run it in repo root.

    Returns True if the command was launched (exit code may still be non-zero).
    """
    repo = Path(repo_path)
    summary = repo / REPO_SUMMARY_FILENAME
    if _has_authoritative_repo_summary(summary):
        return False
    if not init_command or not init_command.strip():
        return False
    logger.info("Running UTA_OPENCODE_INIT_COMMAND / opencode_init_command for project bootstrap")
    try:
        r = subprocess.run(
            init_command,
            cwd=repo_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
            env=os.environ.copy(),
        )
        if r.returncode != 0:
            logger.warning(
                "Project init command exited %s: %s",
                r.returncode,
                (r.stderr or r.stdout or "")[:500],
            )
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Project init command timed out (10m)")
        return True
    except Exception as e:
        logger.warning("Project init command failed: %s", e)
        return True


def _graph_stats(graph: "CodeGraph") -> Dict[str, Any]:
    kinds = Counter(n.kind for n in graph.nodes.values())
    packages: Counter[str] = Counter()
    for fqn, node in graph.nodes.items():
        if node.kind != "class":
            continue
        parts = fqn.split(".")
        if len(parts) >= 3:
            packages[".".join(parts[:3])] += 1
        elif len(parts) == 2:
            packages[parts[0]] += 1
    top_pkgs = packages.most_common(20)
    annos: Counter[str] = Counter()
    for node in graph.nodes.values():
        if node.kind != "class":
            continue
        for a in node.metadata.get("annotations", []) or []:
            annos[a] += 1
    top_annos = annos.most_common(15)
    return {
        "nodes_by_kind": dict(kinds),
        "top_packages": top_pkgs,
        "top_class_annotations": top_annos,
    }


def _build_context_summary_markdown(
    repo_path: str,
    graph: "CodeGraph",
    module: Optional[str],
) -> str:
    repo = Path(repo_path)
    lines: List[str] = [
        "# UTA project summary (machine-generated)",
        "",
        "Read this file for Maven layout and coarse graph statistics before writing tests.",
        "",
        f"- Repository root: `{repo.resolve()}`",
        f"- Maven module filter: `{module or '(all under scan path)'}`",
        "",
        "## Maven",
        "",
    ]
    root_pom = repo / "pom.xml"
    if root_pom.exists():
        coords = _extract_pom_coords(_read_text(root_pom))
        for k, v in coords.items():
            lines.append(f"- **{k}**: `{v}`")
        lines.append("")
        mods = _list_modules(repo, module)
        if mods:
            lines.append("### Modules (from root POM or CLI `--module`)")
            for m in mods:
                lines.append(f"- `{m}`")
            lines.append("")
    else:
        lines.append("_No root pom.xml found._")
        lines.append("")

    stats = _graph_stats(graph)
    lines.append("## Graph overview (parsed Java)")
    lines.append("")
    nbk = stats["nodes_by_kind"]
    for key in ("class", "interface", "method", "field"):
        if key in nbk:
            lines.append(f"- **{key}** count: {nbk[key]}")
    lines.append("")
    lines.append("### Top package prefixes (by class count)")
    for pkg, c in stats["top_packages"]:
        lines.append(f"- `{pkg}` — {c} classes")
    lines.append("")
    lines.append("### Frequent class-level annotations")
    for a, c in stats["top_class_annotations"]:
        lines.append(f"- `@{a}` — {c}")
    lines.append("")
    lines.append("## Related UTA context files")
    lines.append("")
    lines.append("Same directory as this file:")
    lines.append("- `class_map.md`, `dependency_map.md`, `process_flows.md`, `call_graph.md`")
    lines.append("")
    return "\n".join(lines)


def _build_repo_summary_markdown(
    repo_path: str,
    graph: "CodeGraph",
    module: Optional[str],
) -> str:
    stats = _graph_stats(graph)
    n_classes = stats["nodes_by_kind"].get("class", 0)
    lines = [
        UTA_GENERATED_MARKER,
        "",
        "# Project overview (UTA)",
        "",
        "This file was generated by the Unit Test Agent. Replace or edit it to document",
        "architecture, domain vocabulary, and test conventions for your team.",
        "",
        f"**Module scope**: `{module or 'default scan path'}`",
        f"**Classes in graph**: {n_classes}",
        "",
        "For structured stats and package breakdown, read:",
        f"`{Path(repo_path) / '.uta_cache' / 'context' / CONTEXT_SUMMARY_FILENAME}`",
        "",
        "## Maven coordinates (root `pom.xml`, best-effort)",
        "",
    ]
    root_pom = Path(repo_path) / "pom.xml"
    if root_pom.exists():
        coords = _extract_pom_coords(_read_text(root_pom))
        if coords:
            lines.append(" | ".join(f"**{k}**: `{v}`" for k, v in coords.items()))
        else:
            lines.append("_Could not parse coordinates._")
    else:
        lines.append("_No root pom.xml._")
    lines.append("")
    return "\n".join(lines)


def _is_uta_generated_summary(path: Path) -> bool:
    if not path.exists():
        return False
    head = _read_text(path, 400)
    return UTA_GENERATED_MARKER in head


def _has_authoritative_repo_summary(path: Path) -> bool:
    """Return whether an existing repo summary should block bootstrap regeneration."""
    return path.exists() and path.stat().st_size > 20 and not _is_uta_generated_summary(path)


def sync_project_summaries(
    repo_path: str,
    graph: "CodeGraph",
    module: Optional[str],
    *,
    language: str = "java",
    max_files: int = 500,
) -> Dict[str, str]:
    """Write ``project_summary.md`` under context; create or refresh ``.uta_summary.md`` when safe.

    Returns absolute paths for prompts: ``repo_summary_abs``, ``context_summary_abs``.
    """
    from uta.engine.project_summary import make_project_summary_provider

    provider = make_project_summary_provider(
        language,
        repo_path,
        graph=graph,
        module=module,
        max_files=max_files,
    )
    return provider.sync().as_dict()


def prompt_template_paths(repo_path: str, context_dir: Union[str, Path]) -> Dict[str, Any]:
    """Template kwargs for ``generate_test.txt`` (absolute paths, existence flag)."""
    repo = Path(repo_path)
    ctx = Path(context_dir)
    rs = repo / REPO_SUMMARY_FILENAME
    cs = ctx / CONTEXT_SUMMARY_FILENAME
    tg = ctx / TEST_GUIDANCE_FILENAME
    cf = ctx / COMPILE_FACTS_FILENAME
    return {
        "repo_summary_abs": str(rs.resolve()),
        "context_summary_abs": str(cs.resolve()),
        "test_guidance_abs": str(tg.resolve()),
        "compile_facts_abs": str(cf.resolve()),
        "compile_facts_exists": cf.exists() and cf.stat().st_size > 10,
        "test_guidance_exists": tg.exists() and tg.stat().st_size > 10,
        "repo_summary_exists": rs.exists() and rs.stat().st_size > 10,
    }
