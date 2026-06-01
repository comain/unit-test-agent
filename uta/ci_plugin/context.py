from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Optional

from uta.ci_plugin.models import CiTaskRecord

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
    :class:`~uta.ci_plugin.protocols.base.CiContextProvider`. Everything else (pipeline metadata, enforcement
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
            "",
            "## Git commit messages",
        ]
    )
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
    run_command: Optional[RunCommand] = None,
) -> List[str]:
    runner = run_command or subprocess.run
    cmd = ["git", "-C", str(repo_path), "log", "--format=%B%x1e", f"{base_ref}..{head_ref}"]
    completed = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        return []
    return [item.strip() for item in (completed.stdout or "").split("\x1e") if item.strip()]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value)).strip("-") or "task"
