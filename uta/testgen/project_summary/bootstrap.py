from __future__ import annotations

import logging
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any, List, Optional

from uta.testgen.project_summary.constants import (
    OPENCODE_INIT_MERGE_HEADER,
    OPENCODE_INIT_OUTPUT_FILENAME,
    REPO_SUMMARY_FILENAME,
    UTA_GENERATED_MARKER,
)

logger = logging.getLogger("uta")


def _read_text(path: Path, limit: int = 80_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _is_uta_generated_summary(path: Path) -> bool:
    if not path.exists():
        return False
    head = _read_text(path, 400)
    return UTA_GENERATED_MARKER in head


def _has_authoritative_repo_summary(path: Path) -> bool:
    """Return whether an existing repo summary should block bootstrap regeneration."""
    return path.exists() and path.stat().st_size > 20 and not _is_uta_generated_summary(path)


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


def maybe_run_project_bootstrap(
    repo_path: str,
    session_id: Optional[str] = None,
    timeout: Optional[int] = None,
) -> bool:
    """Ask the configured harness to introduce itself to the repository."""
    from agent_core.harness.lifecycle import (
        BootstrapUnsupportedError,
        WorkspaceBootstrapRequest,
        bootstrap_harness_workspace,
    )

    from uta.shared.config import settings
    from uta.testgen.harness import create_agent_harness

    repo = Path(repo_path)
    summary = repo / REPO_SUMMARY_FILENAME
    if not settings.opencode_init_slash_enabled:
        logger.info("Skipping project bootstrap - disabled by settings")
        return False
    if _has_authoritative_repo_summary(summary):
        return False

    seconds = timeout if timeout is not None else settings.opencode_init_slash_timeout
    request = WorkspaceBootstrapRequest(
        purpose="project-summary",
        timeout_seconds=max(1, int(seconds or 1)),
    )
    try:
        result = bootstrap_harness_workspace(
            create_agent_harness(), repo_path=repo, request=request
        )
    except BootstrapUnsupportedError:
        logger.info("Configured harness does not support project bootstrap; skipping")
        return False
    except Exception as exc:
        logger.warning("Project bootstrap failed: %s", exc)
        return True

    harvested = _harvest_opencode_init_artifacts(repo)
    init_text = result.output_text
    if not _is_meaningful_init_output(init_text):
        init_text = textwrap.dedent(
            f"""\
            _Project bootstrap did not yield meaningful output._

            Observed behavior:
            - session_id: `{result.session_id or "none"}`
            - bootstrap did not produce a concrete repo summary artifact
            - output contained no concrete repo summary artifact or actionable bootstrap text

            Recommendation:
            - rely on `.uta_cache/context/project_summary.md`
            - add a manual `.uta_summary.md` for stable repo conventions
            - or configure `UTA_PROJECT_BOOTSTRAP_FALLBACK_COMMAND` for deterministic bootstrap
            """
        ).strip()
    _write_opencode_init_output(repo, result.session_id or "none", init_text)

    if not harvested and not (summary.exists() and summary.stat().st_size > 20):
        logger.info(
            "After bootstrap, %s still missing or empty - generation prompts will rely on "
            "project_summary.md until you add a repo summary or run a shell init command.",
            REPO_SUMMARY_FILENAME,
        )
    return True


maybe_run_opencode_init_slash = maybe_run_project_bootstrap


def maybe_run_project_init_command(repo_path: str, init_command: Optional[str]) -> bool:
    """If ``.uta_summary.md`` is missing and ``init_command`` is set, run it in repo root."""
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
