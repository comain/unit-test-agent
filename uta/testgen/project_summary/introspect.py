from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from uta.testgen.project_summary.constants import (
    COMPILE_FACTS_FILENAME,
    SESSION_RETROSPECT_FILENAME,
    STAGE_INTROSPECT_FILENAME,
)


def _read_text(path: Path, limit: int = 80_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


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
