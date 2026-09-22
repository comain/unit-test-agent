from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.enforcement.enforcement import QualityGateStatus, RunCommand
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.enforcement.enforcement import (
    ValidationVerdict,
    evidence_marker_header,
    evidence_marker_payload,
    finalize_evidence,
    git_output,
    validate_evidence_envelope,
)


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
    maven_central_mirror_url: str = "",
    preserve_explicit_target_scope: bool = False,
) -> Dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    runner = MavenEnforcementRunner(
        command=command,
        timeout_seconds=timeout_seconds,
        run_command=run_command,
        base_ref=base_ref,
        maven_central_mirror_url=maven_central_mirror_url,
        preserve_explicit_target_scope=preserve_explicit_target_scope,
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
        "headCommit": git_output(repo, "rev-parse", "HEAD"),
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
    return finalize_evidence(evidence, evidence_id_prefix="uta-java-enforcement")


def validate_java_enforcement_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_head: Optional[str] = None,
) -> ValidationVerdict:
    verdict = validate_evidence_envelope(
        evidence,
        language="java",
        backend=JAVA_ENFORCEMENT_BACKEND,
        schema_version=JAVA_ENFORCEMENT_SCHEMA_VERSION,
        expected_head=expected_head,
    )
    if verdict is not None:
        return verdict
    return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Java enforcement passed"))


def format_evidence_markers(evidence: Mapping[str, Any]) -> str:
    lines = [
        evidence_marker_header("java", evidence),
        evidence_marker_payload("java", evidence),
    ]
    return "\n".join(lines) + "\n"


def _status_value(status: QualityGateStatus) -> str:
    if status == QualityGateStatus.passed:
        return JavaEnforcementStatus.passed.value
    if status == QualityGateStatus.missing_evidence:
        return JavaEnforcementStatus.missing_evidence.value
    if status == QualityGateStatus.timeout:
        return JavaEnforcementStatus.timeout.value
    if status == QualityGateStatus.command_error:
        return JavaEnforcementStatus.command_error.value
    if status == QualityGateStatus.skipped:
        return JavaEnforcementStatus.skipped.value
    return JavaEnforcementStatus.failed.value


def _reason_code(status: QualityGateStatus, summary: str) -> str:
    if status == QualityGateStatus.passed:
        if "no changed production Java" in summary:
            return "no_changed_java_targets"
        return "passed"
    if status == QualityGateStatus.missing_evidence:
        return "missing_evidence"
    if status == QualityGateStatus.timeout:
        return "timeout"
    if status == QualityGateStatus.command_error:
        return "command_error"
    if status == QualityGateStatus.skipped:
        return "skipped"
    return "failed"

