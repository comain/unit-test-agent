from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Optional

from uta.shared.ci_models import CiTaskRecord

from agent_core.git import GitWorkspace

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def assemble_base_context(
    record: CiTaskRecord,
    *,
    issue: Optional[Dict[str, Any]] = None,
    user_context: Optional[str] = None,
    commit_messages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the protocol-neutral repair context skeleton.

    The ``issue`` section is supplied by a protocol-specific
    :class:`~uta.app.protocols.base.CiContextProvider`. Everything else (pipeline metadata, enforcement
    evidence, git commit messages, user context, source priority) is generic.
    """
    issue = issue or {}
    issue_description = issue.get("description")

    missing_reasons = []
    if not issue_description:
        missing_reasons.append("issue_description_unavailable")
    if not commit_messages:
        missing_reasons.append("git_commit_messages_unavailable")

    return {
        "pipeline": {
            "appName": record.request.app_name,
            "gitUrl": record.request.git_url,
            "branch": record.request.branch,
            "commitId": record.request.commit_id,
            "jiraId": record.request.jira_id,
            "operator": record.request.operator,
            "taskId": record.request.task_id,
            "recordId": record.request.record_id,
            "parentId": record.request.parent_id,
            "taskTemplateId": record.request.task_template_id,
            "pipelineId": record.request.pipeline_id,
            "sprintId": record.request.sprint_id,
            "appType": record.request.app_type,
            "stage": record.request.stage,
        },
        "enforcement": record.enforcement_result or {},
        "git": {
            "commitMessages": commit_messages or [],
        },
        "issue": {
            "id": issue.get("id"),
            "description": issue_description,
            "source": issue.get("source"),
            "kind": issue.get("kind"),
        },
        "user": {
            "context": user_context,
        },
        "sources": [
            {"name": "trigger_payload", "available": True, "priority": 1},
            {"name": "enforcement_evidence", "available": bool(record.enforcement_result), "priority": 2},
            {"name": "git_commit_messages", "available": bool(commit_messages), "priority": 3},
            {
                "name": "issue_description",
                "available": bool(issue_description),
                "priority": 4,
                "source": issue.get("source"),
            },
            {"name": "user_context", "available": bool(user_context), "priority": 5},
        ],
        "missingReasons": missing_reasons,
    }


def render_context_markdown(context: Dict[str, Any]) -> str:
    pipeline = context.get("pipeline") or {}
    enforcement = context.get("enforcement") or {}
    git = context.get("git") or {}
    issue = context.get("issue") if isinstance(context.get("issue"), dict) else {}
    user = context.get("user") or {}
    sources = context.get("sources") or []

    lines = [
        "# CI Context",
        "",
        "## Priority order",
    ]
    for source in sorted(sources, key=lambda item: int(item.get("priority") or 100)):
        lines.append(
            f"- P{source.get('priority')}: {source.get('name')} "
            f"({'available' if source.get('available') else 'missing'})"
        )

    lines.extend(
        [
            "",
            "## Missing context",
        ]
    )
    lines.extend([f"- {reason}" for reason in context.get("missingReasons") or []] or ["- none"])
    lines.extend(
        [
            "",
            "## Pipeline",
            f"- App: {pipeline.get('appName') or ''}",
            f"- Branch: {pipeline.get('branch') or ''}",
            f"- Git URL: {pipeline.get('gitUrl') or ''}",
            f"- Commit: {pipeline.get('commitId') or ''}",
            f"- Issue: {pipeline.get('jiraId') or issue.get('id') or ''}",
            f"- Operator: {pipeline.get('operator') or ''}",
            f"- Task: {pipeline.get('taskId') or ''}",
            f"- Record: {pipeline.get('recordId') or ''}",
            f"- Template: {pipeline.get('taskTemplateId') or ''}",
            "",
            "## Enforcement",
            f"- Status: {enforcement.get('status') or ''}",
            f"- Summary: {enforcement.get('summary') or ''}",
            f"- Command: {' '.join(enforcement.get('command') or [])}",
        ]
    )
    evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
    failed_surefire_tests = (
        evidence.get("failedSurefireTests") if isinstance(evidence.get("failedSurefireTests"), list) else []
    )
    if failed_surefire_tests:
        lines.extend(["", "### Failed Surefire tests"])
        for item in failed_surefire_tests[:10]:
            if not isinstance(item, dict):
                continue
            class_name = str(item.get("className") or "").strip()
            test_name = str(item.get("testName") or "").strip()
            summary = str(item.get("summary") or "").strip()
            message = str(item.get("message") or "").strip()
            source_location = str(item.get("sourceLocation") or "").strip()
            report_path = str(item.get("reportPath") or "").strip()
            label = test_name or class_name or "unknown"
            lines.append(f"- `{label}`")
            if summary:
                lines.append(f"  - Summary: {summary}")
            if message:
                lines.append(f"  - Failure: {message}")
            if source_location:
                lines.append(f"  - Source: {source_location}")
            if report_path:
                lines.append(f"  - Report: {report_path}")
    excluded_failing = (
        evidence.get("excludedFailingTestClasses")
        if isinstance(evidence.get("excludedFailingTestClasses"), list)
        else []
    )
    retained_failing = (
        evidence.get("retainedFailingTestClasses")
        if isinstance(evidence.get("retainedFailingTestClasses"), list)
        else []
    )
    if excluded_failing or retained_failing:
        lines.extend(["", "### Failing tests by relation to the change"])
        if excluded_failing:
            lines.append(
                "- Excluded as unrelated (removed from both gates): "
                + ", ".join(f"`{item}`" for item in excluded_failing[:20])
            )
        if retained_failing:
            lines.append(
                "- Related to this change (still counted): "
                + ", ".join(f"`{item}`" for item in retained_failing[:20])
            )
    hanging = evidence.get("hangingTestClasses")
    quarantined = evidence.get("quarantinedTestClasses")
    retained_hanging = evidence.get("retainedHangingTestClasses")
    pre_quarantined = evidence.get("preQuarantinedTestClasses")
    if any(isinstance(value, list) and value for value in (
        hanging, quarantined, retained_hanging, pre_quarantined
    )):
        lines.extend(["", "### Hanging test quarantine"])
        for label, values in (
            ("Detected", hanging),
            ("Quarantined after a stall", quarantined),
            ("Retained because related", retained_hanging),
            ("Pre-quarantined", pre_quarantined),
        ):
            if isinstance(values, list) and values:
                lines.append(f"- {label}: " + ", ".join(f"`{item}`" for item in values[:20]))
        if evidence.get("stallReason"):
            lines.append(f"- Stall reason: `{evidence['stallReason']}`")
    target_tests = evidence.get("targetTests") if isinstance(evidence.get("targetTests"), list) else []
    filtered_targets = (
        evidence.get("filteredTargetClasses") if isinstance(evidence.get("filteredTargetClasses"), list) else []
    )
    if filtered_targets or target_tests:
        lines.extend(["", "### Target evidence"])
        if filtered_targets:
            lines.append("- Filtered production targets:")
            lines.extend(f"  - `{item}`" for item in filtered_targets[:20])
        if target_tests:
            lines.append("- Existing target tests:")
            lines.extend(f"  - `{item}`" for item in target_tests[:20])
    lines.extend(["", "## Git commit messages"])
    messages = git.get("commitMessages") or []
    lines.extend([f"- {message}" for message in messages] or ["- unavailable"])
    lines.extend(
        [
            "",
            "## Issue description",
            issue.get("description") or "unavailable",
            "",
            "## User supplied context",
            user.get("context") or "unavailable",
            "",
        ]
    )
    return "\n".join(lines)


class RepairContextExporter:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        if ".uta_cache" in self.runtime_root.parts:
            raise ValueError("repair context runtime root must be outside .uta_cache")

    def export(self, repair_task_id: str, context: Dict[str, Any]) -> Path:
        target = self.runtime_root / "ci_context" / _safe_name(repair_task_id) / "ci_context.md"
        if ".uta_cache" in target.parts:
            raise ValueError("repair context must be written outside .uta_cache")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_context_markdown(context), encoding="utf-8")
        return target


def collect_git_commit_messages(
    repo_path: Path,
    base_ref: str,
    *,
    head_ref: str = "HEAD",
    workspace: Optional["GitWorkspace"] = None,
    run_command: Optional[RunCommand] = None,
) -> List[str]:
    args = ("log", "--format=%B%x1e", f"{base_ref}..{head_ref}")
    # Bounded either way. A range against a ref that does not resolve can make
    # git walk far more history than the caller expects.
    if workspace is not None:
        # The checkout's own workspace, when the caller has one. It carries the
        # credentials, timeout and cancellation this deployment configured for
        # that tree, which a bare runner built here would not know about.
        return _split_messages(workspace.output(Path(repo_path), *args))
    if run_command is not None:
        # The injected form still receives a full command, so existing test
        # doubles keep working unchanged.
        completed = run_command(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    else:
        from uta.shared.git import git

        completed = git(timeout=60).run(
            repo_path, *args,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    if completed.returncode != 0:
        return []
    return _split_messages(completed.stdout or "")


def _split_messages(text: str) -> List[str]:
    return [item.strip() for item in (text or "").split("\x1e") if item.strip()]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value)).strip("-") or "task"
