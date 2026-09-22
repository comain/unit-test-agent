"""Shared RDC delivery publisher, DTOs, and Git policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_core.git import (
    BotIdentity,
    GitScopedPublisher,
    GitPublishRequest,
    NoPublicationChanges,
    PushedPublication,
    ReusedPublication,
    PushConflictError,
    PushPolicyError,
)

from uta.shared.config import settings
from uta.shared.git import git


@dataclass(frozen=True)
class RdcDeliveryContext:
    """RDC audit metadata and product-approved paths for one repair delivery."""

    branch_name: str
    repo_task_id: Optional[int] = None
    rdc_task_id: str = ""
    rdc_record_id: str = ""
    jira_key: str = ""
    class_fqns: Optional[List[str]] = None
    commit_paths: Optional[List[str]] = None


@dataclass(frozen=True)
class RdcPublishResult:
    commit_sha: str
    remote_ref: str
    pushed_at: str
    changed_paths: Tuple[str, ...] = ()
    reused: bool = False


def _commit_message(context: RdcDeliveryContext) -> str:
    jira = context.jira_key or context.rdc_task_id or "rdc"
    class_fqns = ", ".join(context.class_fqns or [])
    trailers = [
        "Generated-by: unit-test-agent",
        f"RDC-Task-Id: {context.rdc_task_id or '-'}",
        f"RDC-Record-Id: {context.rdc_record_id or '-'}",
        f"Jira: {context.jira_key or '-'}",
        f"UTA-Repo-Task-Id: {context.repo_task_id if context.repo_task_id is not None else '-'}",
        f"UTA-Classes: {class_fqns or '-'}",
    ]
    return f"uta: repair unit tests for {jira}\n\n" + "\n".join(trailers)


def _normalize_path(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/")


def _cleanup_roots(paths: Iterable[str]) -> List[str]:
    roots: List[str] = []
    for path in paths:
        normalized = _normalize_path(path)
        parts = normalized.split("/")
        root = normalized
        for marker in (
            ".sisyphus",
            ".uta_cache",
            ".uta_reports",
            ".tmp_train_server_test",
            "target",
            "build",
            ".gradle",
            "mutants",
        ):
            if marker in parts:
                root = "/".join(parts[: parts.index(marker) + 1])
                break
        roots.append(root)
    return list(dict.fromkeys(roots))


def _contains_segment_sequence(parts: List[str], expected: Tuple[str, ...]) -> bool:
    width = len(expected)
    return any(tuple(parts[index : index + width]) == expected for index in range(len(parts) - width + 1))


def _is_package_local_python_test_path(parts: List[str]) -> bool:
    if not parts or not any(part in {"test", "tests"} for part in parts[:-1]):
        return False
    filename = parts[-1]
    return filename == "conftest.py" or (
        filename.endswith(".py") and (filename.startswith("test_") or filename.endswith("_test.py"))
    )


def is_allowed_rdc_test_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if normalized == "pom.xml" or normalized.endswith("/pom.xml"):
        return True
    if normalized == "pyproject.toml":
        return True
    parts = normalized.split("/")
    if normalized.startswith("."):
        return False
    if _contains_segment_sequence(parts, ("src", "test")):
        return True
    if parts and parts[0] in {"tests", "test"}:
        return True
    return _is_package_local_python_test_path(parts)


def is_ignored_rdc_runtime_artifact(path: str) -> bool:
    normalized = _normalize_path(path)
    parts = normalized.split("/")
    return any(
        part
        in {
            ".sisyphus",
            ".uta_cache",
            ".uta_reports",
            ".tmp_train_server_test",
            "target",
            "build",
            ".gradle",
            "mutants",
        }
        for part in parts
    ) or normalized in {".coverage", ".uta_summary.md", "opencode.json"}


class RdcRepairPublisher:
    """Apply RDC's test-only policy through agent-core's Git publisher."""

    def __init__(
        self,
        repo_path: Path | str,
        *,
        user_name: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.identity = BotIdentity(
            name=user_name or settings.ci_git_user_name,
            email=user_email or settings.ci_git_user_email,
        )
        self.publisher = GitScopedPublisher(self.repo_path, runner=git())

    def publish(self, context: RdcDeliveryContext) -> RdcPublishResult:
        changed_paths = self.publisher.changed_paths()
        ignored_paths = [path for path in changed_paths if is_ignored_rdc_runtime_artifact(path)]
        committable_candidates = [path for path in changed_paths if path not in set(ignored_paths)]
        requested_paths_configured = context.commit_paths is not None
        requested_paths = {_normalize_path(path) for path in context.commit_paths or [] if path}
        if requested_paths_configured:
            committable_paths = [
                path for path in committable_candidates if _normalize_path(path) in requested_paths
            ]
            cleanup_paths = [
                path for path in committable_candidates if _normalize_path(path) not in requested_paths
            ]
        else:
            committable_paths = committable_candidates
            cleanup_paths = []
        disallowed = [path for path in committable_paths if not is_allowed_rdc_test_path(path)]
        if disallowed:
            raise PushPolicyError(
                "RDC repair delivery refused non-test or UTA artifact diffs: "
                + ", ".join(disallowed)
            )
        if not committable_paths:
            raise PushPolicyError("RDC repair delivery found no test changes to commit")

        result = self.publisher.publish(
            GitPublishRequest(
                branch=context.branch_name,
                paths=committable_paths,
                cleanup_paths=_cleanup_roots([*ignored_paths, *cleanup_paths]),
                message=_commit_message(context),
                identity=self.identity,
                allow_path=is_allowed_rdc_test_path,
                publication_id=_publication_id(context),
            )
        )
        if isinstance(result, NoPublicationChanges):
            raise PushPolicyError("RDC repair delivery found no test changes to commit")
        if isinstance(result, PushedPublication):
            return RdcPublishResult(
                commit_sha=result.commit_sha,
                remote_ref=result.remote_sha,
                pushed_at=result.committed_at,
                changed_paths=tuple(result.changed_paths),
            )
        if isinstance(result, ReusedPublication):
            return RdcPublishResult(
                commit_sha=result.remote_sha,
                remote_ref=result.remote_sha,
                pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                reused=True,
            )
        raise TypeError(f"unexpected Git publication outcome: {type(result).__name__}")


def _publication_id(context: RdcDeliveryContext) -> str:
    identity = "\0".join(
        (
            context.branch_name,
            context.rdc_task_id,
            context.rdc_record_id,
            context.jira_key,
            str(context.repo_task_id or ""),
            *sorted(context.class_fqns or ()),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def repo_relative_path(repo: str | Path, path_value: Any) -> Optional[str]:
    if not path_value:
        return None
    repo_path = Path(repo).expanduser().resolve()
    path = Path(str(path_value)).expanduser()
    try:
        if path.is_absolute():
            path = path.resolve().relative_to(repo_path)
    except ValueError:
        return None
    normalized = path.as_posix().lstrip("/")
    return normalized or None


def result_targets_and_paths(
    repo: str | Path,
    results: Dict[str, Any],
    *,
    statuses: Optional[Iterable[str]] = None,
    include_python_conftest: bool = False,
) -> Tuple[List[str], List[str]]:
    allowed_statuses = {str(status).upper() for status in statuses or []}
    target_ids: List[str] = []
    commit_paths: List[str] = []
    for target_id, result in (results or {}).items():
        status = str((result or {}).get("status") or "").upper()
        if allowed_statuses and status not in allowed_statuses:
            continue
        target_ids.append(str(target_id))
        rel_path = repo_relative_path(repo, (result or {}).get("test_file_path"))
        if rel_path:
            commit_paths.append(rel_path)
    conftest = Path(repo) / "tests" / "uta_generated" / "conftest.py"
    if include_python_conftest and target_ids and conftest.exists():
        commit_paths.append("tests/uta_generated/conftest.py")
    return target_ids, sorted(dict.fromkeys(commit_paths))


def passed_result_targets_and_paths(repo: str | Path, results: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    return result_targets_and_paths(
        repo,
        results,
        statuses=["SUCCESS", "PASSED", "PASS"],
        include_python_conftest=True,
    )


def commit_rdc_repair_results(
    *,
    repo: str | Path,
    manager: Any,
    task_id: int | str,
    branch_name: str,
    results: Dict[str, Any],
    target_ids: Optional[Iterable[str]] = None,
    commit_paths: Optional[List[str]] = None,
    rdc_context: Optional[Dict[str, Any]] = None,
    delivery_context: Optional[RdcDeliveryContext] = None,
    module: Optional[str] = None,
    phase_token_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    selected_targets = list(target_ids or [])
    selected_paths = commit_paths
    if not selected_targets:
        selected_targets, selected_paths = passed_result_targets_and_paths(repo, results)
    if not selected_targets:
        return False

    task_key = int(task_id) if str(task_id).isdigit() else str(task_id)

    if not branch_name:
        if hasattr(manager, "record_push_failed"):
            manager.record_push_failed(
                task_key,
                branch_name="",
                reason="RDC repair delivery skipped: repo task has no branch_name",
                class_fqns=selected_targets,
            )
        return False

    if delivery_context:
        context = RdcDeliveryContext(
            branch_name=delivery_context.branch_name or branch_name,
            repo_task_id=int(task_id) if str(task_id).isdigit() else None,
            rdc_task_id=delivery_context.rdc_task_id,
            rdc_record_id=delivery_context.rdc_record_id,
            jira_key=delivery_context.jira_key,
            class_fqns=selected_targets,
            commit_paths=selected_paths,
        )
    else:
        return False

    push_result = RdcRepairPublisher(repo).publish(context)
    if hasattr(manager, "record_push_verified"):
        manager.record_push_verified(
            task_key,
            branch_name=context.branch_name,
            local_head=push_result.commit_sha,
            remote_head=push_result.remote_ref,
        )
    if hasattr(manager, "record_commit"):
        manager.record_commit(
            task_key,
            class_fqns=selected_targets,
            commit_sha=push_result.commit_sha,
            pushed_at=push_result.pushed_at,
            remote_ref=push_result.remote_ref,
        )
    selected_results = {target_id: results[target_id] for target_id in selected_targets if target_id in results}
    if selected_results and hasattr(manager, "sync_results"):
        manager.sync_results(
            task_key,
            selected_results,
            module=module,
            phase_token_usage=phase_token_usage,
            elapsed_seconds=None,
        )
    return True


def record_existing_repair_commit(
    *,
    manager: Any,
    task_id: int | str,
    target_ids: Iterable[str],
    results: Dict[str, Any],
    module: Optional[str] = None,
    phase_token_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    import time

    task_key = int(task_id) if str(task_id).isdigit() else str(task_id)
    task = None
    if hasattr(manager, "get_task"):
        task = manager.get_task(task_key)
    elif hasattr(manager, "get_repo_task"):
        task = manager.get_repo_task(task_key)
    latest_commit = task.get("latest_commit") if task else None
    remote_ref = task.get("remote_ref") if task else None
    selected_targets = list(target_ids)
    if hasattr(manager, "record_commit"):
        manager.record_commit(
            task_key,
            class_fqns=selected_targets,
            commit_sha=latest_commit,
            pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            remote_ref=remote_ref,
        )
    selected_results = {target_id: results[target_id] for target_id in selected_targets if target_id in results}
    if selected_results and hasattr(manager, "sync_results"):
        manager.sync_results(
            task_key,
            selected_results,
            module=module,
            phase_token_usage=phase_token_usage,
            elapsed_seconds=None,
        )
    return bool(latest_commit)


__all__ = [
    "PushConflictError",
    "PushPolicyError",
    "RdcDeliveryContext",
    "RdcRepairPublisher",
    "commit_rdc_repair_results",
    "is_allowed_rdc_test_path",
    "is_ignored_rdc_runtime_artifact",
    "passed_result_targets_and_paths",
    "record_existing_repair_commit",
    "repo_relative_path",
    "result_targets_and_paths",
]
