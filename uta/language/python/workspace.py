"""Python workspace path rules for the engine LLM guard."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping

from uta.shared.workspace_policy import _git_run

logger = logging.getLogger("uta")

# Suffixes a Python repair may legitimately write under throwaway .tmp_* roots.
_TEMP_ARTIFACT_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".log", ".txt", ".yaml", ".yml"}


def looks_like_python_test_path(path: str) -> bool:
    parts = Path(path).parts
    if not parts or Path(path).suffix != ".py":
        return False
    name = Path(path).name
    if not (name.startswith("test_") or name.endswith("_test.py")):
        return False
    return any(part in {"tests", "test"} or part.endswith("_test") for part in parts[:-1])


def is_python_runtime_temp_artifact(path: str) -> bool:
    if not path.startswith(".tmp_"):
        return False
    return Path(path).suffix.lower() in _TEMP_ARTIFACT_SUFFIXES


def cleanup_python_verifier_residue(repo_path: str) -> None:
    repo = Path(repo_path)
    for relative in ("mutants", ".mutmut-cache", ".pytest_cache", ".coverage"):
        path = repo / relative
        if path.is_dir():
            shutil.rmtree(str(path), ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                logger.debug("Failed to remove Python verifier artifact %s", path, exc_info=True)
    for path in repo.glob(".coverage.*"):
        try:
            path.unlink()
        except OSError:
            logger.debug("Failed to remove Python coverage artifact %s", path, exc_info=True)
    restore_python_verifier_overlays(repo_path)


def restore_python_verifier_overlays(repo_path: str) -> None:
    """Revert UTA verifier overlays left in pytest/mutmut config files."""
    _restore_python_verifier_overlay(repo_path, "setup.cfg", ("UTA_MUTMUT_TARGET_REL=", "UTA_MUTMUT_CANONICAL_MODULE=", "tests/uta_generated/"))
    _restore_python_verifier_overlay(repo_path, "pyproject.toml", ("pytest_add_cli_args_test_selection", "tests/uta_generated/"))


def _restore_python_verifier_overlay(repo_path: str, rel_path: str, markers: tuple[str, ...]) -> None:
    path = Path(repo_path) / rel_path
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not any(marker in content for marker in markers):
        return
    diff = _git_run(repo_path, "diff", "--", rel_path, capture_output=True, text=True)
    if diff.returncode == 0 and not diff.stdout:
        return
    if diff.returncode not in {0, 1}:
        return
    if "[mutmut]" not in content and "[tool.mutmut]" not in content:
        return
    _git_run(repo_path, "checkout", "--", rel_path, capture_output=True, text=True)


class PythonWorkspacePolicy:
    language = "python"

    def allowed_llm_path(self, path: str, state: Dict[str, Any], batch: List[str]) -> bool:
        if self.is_repair_config_path(path):
            return True
        if path == ".coverage" or path.startswith(".coverage."):
            return True
        if path.startswith((".mutmut-cache/", ".pytest_cache/")):
            return True
        if path == "mutants" or path.startswith("mutants/"):
            return True
        if is_python_runtime_temp_artifact(path):
            return True
        if self.is_test_artifact_path(path):
            return True
        if path.startswith("tests/uta_generated/") and path.endswith(".py"):
            return True
        return False

    def is_test_artifact_path(self, path: str) -> bool:
        normalized = path.lstrip("/")
        if not normalized or normalized.startswith("."):
            return False
        return looks_like_python_test_path(normalized)

    def is_repair_config_path(self, path: str) -> bool:
        return path == "pyproject.toml"

    def related_test_artifact_paths(self, state: Dict[str, Any], target_id: str) -> List[str]:
        return []

    def cleanup_runtime_residue(self, repo_path: str) -> None:
        cleanup_python_verifier_residue(repo_path)

    def cleanup_llm_generated_residue(self, repo_path: str, before: Mapping[str, str]) -> None:
        """Discard a lock file created incidentally by an LLM-invoked uv command.

        Existing or staged lock files remain visible to the unsafe-diff guard. Only
        an untracked ``uv.lock`` that did not exist in the pre-phase snapshot is
        verifier/runtime residue rather than an authored repository change.
        """

        if "uv.lock" in before:
            return
        lock_file = Path(repo_path) / "uv.lock"
        if not lock_file.is_file():
            return
        tracked = _git_run(
            repo_path,
            "ls-files",
            "--error-unmatch",
            "--",
            "uv.lock",
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0:
            return
        try:
            lock_file.unlink()
        except OSError:
            logger.debug("Failed to remove generated uv.lock", exc_info=True)
