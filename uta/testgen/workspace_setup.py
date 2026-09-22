"""Prepare the generation branch before a language backend runs."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from uta.testgen.git import output_text as _output_text
from uta.testgen.git import run_git as _git_run
from uta.testgen.graph.state import AgentState
from uta.testgen.progress import merge_phase_timings as _merge_phase_timings
from uta.testgen.workspace_guard import git_status_paths as _git_status_paths

logger = logging.getLogger("uta")


def _clean_rerun_artifacts(repo_path: str) -> None:
    """Remove stale untracked artifacts while preserving UTA runtime outputs."""
    try:
        result = _git_run(
            repo_path,
            "ls-files",
            "--others",
            "--exclude-standard",
            capture_output=True,
            check=False,
        )
        untracked = _output_text(result.stdout).splitlines()
    except Exception as exc:
        logger.warning("Could not list untracked rerun artifacts in %s: %s", repo_path, exc)
        return

    preserved_prefixes = (".uta_cache/", ".uta_reports/")
    preserved_files = {".uta_summary.md", "opencode.json"}
    removable = []
    for rel in untracked:
        normalized = rel.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized in preserved_files or normalized.startswith(preserved_prefixes):
            continue
        removable.append(Path(repo_path) / normalized)

    for path in removable:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove stale rerun artifact %s: %s", path, exc)

    if removable:
        logger.info(
            "Removed %d stale untracked rerun artifact(s) "
            "(preserved .uta_cache, .uta_reports, .uta_summary.md, and opencode.json)",
            len(removable),
        )


def setup_branch(state: AgentState) -> Dict[str, Any]:
    """Create or reuse the configured generation branch."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    branch_name = state.get("branch_name", "unit-code-gen")
    if state.get("preserve_branch"):
        current = _git_run(
            repo_path,
            "branch",
            "--show-current",
            capture_output=True,
            check=False,
        )
        current_branch = _output_text(current.stdout).strip()
        if branch_name and current_branch != branch_name:
            exists = _git_run(
                repo_path,
                "rev-parse",
                "--verify",
                branch_name,
                capture_output=True,
                check=False,
            )
            if exists.returncode != 0:
                logger.info("Production branch %s not found locally; will create from default branch.", branch_name)
            else:
                checkout = _git_run(
                    repo_path,
                    "checkout",
                    branch_name,
                    capture_output=True,
                    check=False,
                )
                if checkout.returncode != 0:
                    error = _output_text(checkout.stderr)
                    raise RuntimeError(
                        f"Could not checkout existing generation branch {branch_name} without resetting local changes. "
                        f"stderr: {error[:300]}"
                    )
                logger.info("Using existing branch/worktree for %s; skipping reset/cleanup", branch_name)
                return _preserved_branch_result(state, repo_path, started)
        else:
            logger.info("Using existing branch/worktree for %s; skipping reset/cleanup", branch_name)
            return _preserved_branch_result(state, repo_path, started)

    logger.info("Fetching latest from remote...")
    _git_run(repo_path, "fetch", "origin", capture_output=True, check=False)
    default_branch = _default_branch(repo_path)
    logger.info("Default branch: %s", default_branch)

    checkout = _git_run(
        repo_path,
        "checkout",
        "-B",
        branch_name,
        default_branch,
        "-f",
        capture_output=True,
        check=False,
    )
    if checkout.returncode != 0:
        error = _output_text(checkout.stderr)
        logger.error("Could not recreate %s from %s in %s: %s", branch_name, default_branch, repo_path, error[:500])
        raise RuntimeError(
            f"git could not recreate branch {branch_name} from {default_branch} in {repo_path}. "
            f"Ensure the repo is a git checkout with a valid default branch. stderr: {error[:300]}"
        )

    _git_run(repo_path, "reset", "--hard", default_branch, capture_output=True, check=False)
    _clean_rerun_artifacts(repo_path)
    logger.info("Recreated %s from %s and cleared stale generated test artifacts", branch_name, default_branch)
    return {
        "run_initial_dirty_paths": sorted(_git_status_paths(repo_path)),
        "phase_timings": _merge_phase_timings(
            state,
            branch_setup_seconds=time.perf_counter() - started,
        ),
    }


def _preserved_branch_result(state: AgentState, repo_path: str, started: float) -> Dict[str, Any]:
    return {
        "current_stage": "setup_branch",
        "run_initial_dirty_paths": sorted(_git_status_paths(repo_path)),
        "phase_timings": _merge_phase_timings(
            state,
            setup_branch_seconds=time.perf_counter() - started,
        ),
    }


def _default_branch(repo_path: str) -> str:
    for candidate in ("origin/master", "origin/main", "master", "main"):
        result = _git_run(
            repo_path,
            "rev-parse",
            "--verify",
            candidate,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return "HEAD"
