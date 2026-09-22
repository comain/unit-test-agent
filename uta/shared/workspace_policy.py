"""What a workspace is allowed to look like, and how to inspect it.

Split out of `workspace_guard.py`, which held two unrelated things: these
rules, which need nothing but git and a path, and the budget/stop enforcement
that needs the task queue -- now in `uta/engine/task_guard.py`.

The split is not tidiness. Enforcement runs deterministic gates and has no
task queue, yet importing this module pulled `tasks.manager` and with it
`tasks.db`, `render` and `targets` -- some 3,900 lines of queue machinery
into a code path that never touches a queue. That single edge was the only
thing making the deterministic lane depend on the agent one.

Language-specific path rules live in each backend's ``WorkspacePolicy``
(see ``uta/language/{java,python}/workspace.py``).
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

from agent_core.git.guard import changed_paths, snapshot

from uta.shared.git import git


logger = logging.getLogger("uta")


class TaskStopRequested(RuntimeError):
    pass


class TaskUnsafeDiffError(RuntimeError):
    pass


class TaskBudgetExceeded(RuntimeError):
    pass


class WorkspacePolicy(Protocol):
    """Language-specific workspace path rules used by the LLM guard."""

    language: str

    def allowed_llm_path(self, path: str, state: Dict[str, Any], batch: List[str]) -> bool:
        ...

    def is_test_artifact_path(self, path: str) -> bool:
        ...

    def is_repair_config_path(self, path: str) -> bool:
        ...

    def related_test_artifact_paths(self, state: Dict[str, Any], target_id: str) -> List[str]:
        """Existing test artifacts that a repair of ``target_id`` may update."""
        ...

    def cleanup_runtime_residue(self, repo_path: str) -> None:
        ...

    def cleanup_llm_generated_residue(
        self, repo_path: str, before: Mapping[str, str]
    ) -> None:
        """Remove language-tool residue created during the guarded LLM phase."""
        ...


# Paths UTA itself owns regardless of backend language.
_GENERIC_ALLOWED_PATHS = {"opencode.json", ".uta_summary.md"}
_GENERIC_ALLOWED_PREFIXES = (".sisyphus/", ".uta_cache/", ".uta_reports/")


def is_uta_runtime_residue(path: str) -> bool:
    """True for paths UTA writes about its own run rather than the repository.

    Reports, caches and scratch files. They are already tolerated by the
    unsafe-diff guard; this names the same set so workspace *identity* can
    exclude it too. `.uta_reports/` in particular is rewritten continuously
    while a run is in flight, so counting it would make any fingerprint taken
    before a report refresh disagree with one taken after.
    """
    # Not `lstrip("./")`: that strips *characters*, so ".uta_reports/x" would
    # lose its leading dot and stop matching the prefix it belongs to.
    candidate = str(path or "")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate in _GENERIC_ALLOWED_PATHS:
        return True
    return candidate.startswith(_GENERIC_ALLOWED_PREFIXES)


def guard_language(state: Optional[Dict[str, Any]], batch: Optional[List[str]] = None) -> str:
    language = str((state or {}).get("language") or "").strip().lower()
    if language:
        return language
    if any(str(item).startswith(("pyfile:", "pysymbol:")) for item in batch or []):
        return "python"
    return "java"


def workspace_policy_for(state: Optional[Dict[str, Any]], batch: Optional[List[str]] = None) -> WorkspacePolicy:
    from uta.shared.backends import make_backend

    return make_backend(guard_language(state, batch), "workspace_policy")


def workspace_policies() -> tuple[WorkspacePolicy, ...]:
    from uta.shared.backends import make_all

    return make_all("workspace_policy")


def _git_run(repo_path: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    """Kept for callers and tests that patch it by name.

    New code should not reach for this. It is an unbounded `subprocess.run`,
    which is why the two readers below no longer use it: a `git status` that
    blocks -- large checkout, cold cache, an index lock held elsewhere -- would
    hang the turn this is supposed to be protecting, twice per phase.
    """
    kwargs.setdefault("check", False)
    return git().run(repo_path, *args, **kwargs)


def git_status_paths(repo_path: str) -> set:
    """Every path git reports as changed, from agent-core.

    This used to parse `--porcelain=1 -z` here. Parsing it in one place is not
    a tidiness argument: the local copy had the two fields of a rename the
    wrong way round, so the guard checked a path that no longer existed while
    the path that now did went unexamined -- an agent could move a file out of
    a directory it may write into one it may not, unnoticed. A second
    implementation is a second chance to get that wrong.
    """
    return changed_paths(repo_path)


def git_status_snapshot(repo_path: str) -> Dict[str, str]:
    """Path -> content digest for everything dirty, from agent-core."""
    return snapshot(repo_path)


def task_fqns_for_guard(state: Dict[str, Any], batch: Optional[List[str]] = None) -> List[str]:
    fqns: List[str] = []
    if batch:
        fqns.extend(batch)
    current_batch = state.get("current_batch") or []
    if isinstance(current_batch, list):
        fqns.extend(str(item) for item in current_batch if item)
    current_class = state.get("current_class")
    if current_class:
        fqns.append(str(current_class))
    return list(dict.fromkeys(fqns))


def allowed_llm_path(path: str, state: Dict[str, Any], batch: List[str]) -> bool:
    normalized = path.replace("\\", "/")
    if normalized in _GENERIC_ALLOWED_PATHS or normalized.startswith(_GENERIC_ALLOWED_PREFIXES):
        return True
    return workspace_policy_for(state, batch).allowed_llm_path(normalized, state, batch)


def allowed_preexisting_dirty_path(path: str, state: Dict[str, Any], batch: List[str]) -> bool:
    """Allow bounded UTA-owned setup residue while still blocking source edits."""
    normalized = path.replace("\\", "/")
    if allowed_llm_path(normalized, state, batch):
        return True
    if normalized in set(state.get("deterministic_change_paths") or []):
        return True
    if normalized in {"AGENTS.md", "CLAUDE.md"} or normalized.startswith(".opencode/"):
        return True
    return False


def looks_like_test_artifact_path(path: str) -> bool:
    """Allow test-scope edits; target drift is a workflow issue, not an unsafe production diff."""
    normalized = path.replace("\\", "/").lstrip("/")
    if not normalized or normalized.startswith("."):
        return False
    return any(policy.is_test_artifact_path(normalized) for policy in workspace_policies())


def normalize_commit_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("/")


def extra_authored_test_paths(
    current_dirty: Iterable[str],
    run_initial_dirty: Iterable[str],
    existing_commit_paths: Iterable[str],
) -> List[str]:
    """Test files the repair authored this run that aren't already scheduled to commit.

    A repair frequently edits a shared base/helper test class (e.g. a superclass the
    target test ``extends``) or adds a new test class that is not any target's primary
    ``test_file_path``. Restricting the commit to the per-target paths silently dropped
    those edits, so the pushed branch lost the tests that made the quality gate pass.

    We only return test files that became dirty *during this run* — paths already dirty
    at run start (``run_initial_dirty``) are pre-existing residue from a reused workspace
    and are deliberately excluded so contamination is never committed. Paths already in
    ``existing_commit_paths`` are skipped to keep this purely additive.
    """
    baseline = {normalize_commit_path(path) for path in run_initial_dirty}
    already = {normalize_commit_path(path) for path in existing_commit_paths}
    extra: List[str] = []
    for path in current_dirty:
        normalized = normalize_commit_path(path)
        if not normalized or normalized in baseline or normalized in already:
            continue
        if looks_like_test_artifact_path(normalized):
            extra.append(normalized)
    return sorted(dict.fromkeys(extra))
