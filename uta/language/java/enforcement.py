from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.ci_plugin.enforcement import EnforcementResultStatus, MavenEnforcementRunner, RunCommand
from uta.engine.enforcement import ValidationVerdict


JAVA_ENFORCEMENT_SCHEMA_VERSION = 1
JAVA_ENFORCEMENT_BACKEND = "maven_enforcer"
JAVA_ENFORCEMENT_CORE_VERSION = "1.0.0"
UTA_VERSION = "local"


class JavaEnforcementStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    missing_evidence = "missing_evidence"
    command_error = "command_error"
    timeout = "timeout"
    skipped = "skipped"


def run_java_enforcement(
    *,
    repo_path: Path,
    command: str,
    base_ref: str = "origin/master",
    timeout_seconds: int = 1800,
    run_command: Optional[RunCommand] = None,
) -> Dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    runner = MavenEnforcementRunner(
        command=command,
        timeout_seconds=timeout_seconds,
        run_command=run_command,
        base_ref=base_ref,
    )
    result = runner.run(repo)
    evidence = {
        "schemaVersion": JAVA_ENFORCEMENT_SCHEMA_VERSION,
        "evidenceId": "",
        "language": "java",
        "backend": JAVA_ENFORCEMENT_BACKEND,
        "repo": str(repo),
        "baseRef": base_ref,
        "headRef": "HEAD",
        "headCommit": _git_output(repo, "rev-parse", "HEAD"),
        "status": _status_value(result.status),
        "passed": bool(result.passed),
        "reasonCode": _reason_code(result.status, result.summary),
        "summary": result.summary,
        "command": list(result.command),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "usageGuide": result.usage_guide,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "utaVersion": UTA_VERSION,
        "enforcementCoreVersion": JAVA_ENFORCEMENT_CORE_VERSION,
    }
    return _finalize(evidence)


def validate_java_enforcement_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_head: Optional[str] = None,
) -> ValidationVerdict:
    if int(evidence.get("schemaVersion") or 0) != JAVA_ENFORCEMENT_SCHEMA_VERSION:
        return ValidationVerdict(False, "unknown_schema_version", "Unsupported Java enforcement evidence schema version")
    if evidence.get("language") != "java" or evidence.get("backend") != JAVA_ENFORCEMENT_BACKEND:
        return ValidationVerdict(False, "wrong_backend", "Evidence is not Java Maven enforcement evidence")
    if expected_head and evidence.get("headCommit") != expected_head:
        return ValidationVerdict(False, "stale_head", "Evidence head commit does not match the expected branch head")
    if evidence.get("status") != JavaEnforcementStatus.passed.value or evidence.get("passed") is not True:
        return ValidationVerdict(
            False,
            str(evidence.get("reasonCode") or "failed"),
            str(evidence.get("summary") or "Java enforcement did not pass"),
        )
    return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Java enforcement passed"))


def format_evidence_markers(evidence: Mapping[str, Any]) -> str:
    lines = [
        f"[test-enforcer] java enforcement {evidence.get('status')} reason={evidence.get('reasonCode')} schema={evidence.get('schemaVersion')}",
        f"UTA_JAVA_ENFORCEMENT_EVIDENCE={json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True)}",
    ]
    return "\n".join(lines) + "\n"


def _status_value(status: EnforcementResultStatus) -> str:
    if status == EnforcementResultStatus.passed:
        return JavaEnforcementStatus.passed.value
    if status == EnforcementResultStatus.missing_evidence:
        return JavaEnforcementStatus.missing_evidence.value
    if status == EnforcementResultStatus.timeout:
        return JavaEnforcementStatus.timeout.value
    if status == EnforcementResultStatus.command_error:
        return JavaEnforcementStatus.command_error.value
    if status == EnforcementResultStatus.skipped:
        return JavaEnforcementStatus.skipped.value
    return JavaEnforcementStatus.failed.value


def _reason_code(status: EnforcementResultStatus, summary: str) -> str:
    if status == EnforcementResultStatus.passed:
        if "no changed production Java" in summary:
            return "no_changed_java_targets"
        return "passed"
    if status == EnforcementResultStatus.missing_evidence:
        return "missing_evidence"
    if status == EnforcementResultStatus.timeout:
        return "timeout"
    if status == EnforcementResultStatus.command_error:
        return "command_error"
    if status == EnforcementResultStatus.skipped:
        return "skipped"
    return "failed"


def _finalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = json.dumps({k: v for k, v in payload.items() if k != "evidenceId"}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["evidenceId"] = f"uta-java-enforcement:{digest[:16]}"
    return payload


def _git_output(repo: Path, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""
