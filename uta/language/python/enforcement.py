from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from uta.engine.enforcement import ValidationVerdict
from uta.engine.languages import RawTargetSelection, default_registry
from uta.language.python.verification.runner import PythonRuntimeConfig, PythonVerificationResult, resolve_python_runtime_config, verify_python_target
from uta.engine.targets import TargetRef


PYTHON_ENFORCEMENT_SCHEMA_VERSION = 1
PYTHON_ENFORCEMENT_BACKEND = "python_enforcer"
PYTHON_ENFORCEMENT_CORE_VERSION = "1.0.0"
UTA_VERSION = "local"


class PythonEnforcementStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    missing_evidence = "missing_evidence"
    command_error = "command_error"


VerificationRunner = Callable[..., PythonVerificationResult]


def run_python_enforcement(
    *,
    repo_path: Path,
    target_values: Sequence[str],
    test_paths: Sequence[str],
    base_ref: str = "origin/master",
    coverage_gate: float = 80.0,
    mutation_gate: float = 70.0,
    syntax_version: str = "python3",
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    dev_skills_launcher_version: Optional[str] = None,
    verification_runner: Optional[VerificationRunner] = None,
) -> Dict[str, Any]:
    repo = Path(repo_path).expanduser().resolve()
    base_commit = _git_output(repo, "rev-parse", "--verify", base_ref)
    head_commit = _git_output(repo, "rev-parse", "HEAD")
    targets = _target_refs(repo, target_values, base_ref=base_ref)
    changed_files = _changed_production_python_files(repo, base_ref)
    target_source_paths = [
        target.source_path
        for target in targets
        if target.source_path
    ]
    changed_lines = _changed_lines_by_file(repo, base_ref, sorted(set(changed_files + target_source_paths)))

    common = {
        "schemaVersion": PYTHON_ENFORCEMENT_SCHEMA_VERSION,
        "evidenceId": "",
        "language": "python",
        "backend": PYTHON_ENFORCEMENT_BACKEND,
        "repo": str(repo),
        "baseRef": base_ref,
        "baseCommit": base_commit,
        "headRef": "HEAD",
        "headCommit": head_commit,
        "changedProductionFiles": changed_files,
        "changedLines": changed_lines,
        "targets": [target.as_selection() for target in targets],
        "coverage": None,
        "mutation": None,
        "commands": [],
        "artifacts": {},
        "setup": {},
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "utaVersion": UTA_VERSION,
        "enforcementCoreVersion": PYTHON_ENFORCEMENT_CORE_VERSION,
    }
    if dev_skills_launcher_version:
        common["devSkillsLauncherVersion"] = dev_skills_launcher_version

    if not base_commit:
        return _finalize({**common, "status": PythonEnforcementStatus.command_error.value, "passed": False, "reasonCode": "missing_base_ref", "summary": f"Base ref is not available: {base_ref}"})
    if not head_commit:
        return _finalize({**common, "status": PythonEnforcementStatus.command_error.value, "passed": False, "reasonCode": "missing_head_commit", "summary": "Unable to resolve HEAD"})
    if not targets:
        return _finalize({**common, "status": PythonEnforcementStatus.passed.value, "passed": True, "reasonCode": "no_changed_python_targets", "summary": "Python enforcement passed; no changed production Python files"})
    if not test_paths:
        return _finalize({**common, "status": PythonEnforcementStatus.missing_evidence.value, "passed": False, "reasonCode": "missing_test_paths", "summary": "Python enforcement requires at least one --test-path"})

    config = resolve_python_runtime_config(repo, overrides=runtime_overrides)
    runner = verification_runner or verify_python_target
    target_evidence: List[Dict[str, Any]] = []
    status = PythonEnforcementStatus.passed.value
    passed = True
    reason_code = "passed"
    summary = "Python enforcement passed"

    for target in targets:
        result = runner(
            repo,
            target,
            test_paths=test_paths,
            syntax_version=syntax_version,
            coverage_gate=float(coverage_gate),
            mutation_gate=float(mutation_gate),
            config=config,
            changed_lines=changed_lines,
        )
        target_payload = _target_result_evidence(target, result)
        target_evidence.append(target_payload)
        if result.status != "passed":
            status = PythonEnforcementStatus.failed.value
            passed = False
            reason_code = result.reason_code
            summary = result.message or f"Python enforcement failed: {result.reason_code}"
            break

    coverage = _aggregate_coverage(target_evidence, float(coverage_gate))
    mutation = _aggregate_mutation(target_evidence, float(mutation_gate))
    commands = [command for item in target_evidence for command in item.get("commands", [])]
    artifacts: Dict[str, Any] = {}
    for item in target_evidence:
        for key, value in (item.get("artifacts") or {}).items():
            artifacts.setdefault(key, []).append(value)

    return _finalize(
        {
            **common,
            "status": status,
            "passed": passed,
            "reasonCode": reason_code,
            "summary": summary,
            "targetResults": target_evidence,
            "coverage": coverage,
            "mutation": mutation,
            "commands": commands,
            "artifacts": artifacts,
            "setup": {
                "environmentProfile": config.environment_profile,
                "dependencyFingerprints": dict(config.dependency_fingerprints),
                "cacheKey": config.cache_key,
                "configSources": dict(config.config_sources),
            },
        }
    )


def validate_python_enforcement_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_head: Optional[str] = None,
) -> ValidationVerdict:
    if int(evidence.get("schemaVersion") or 0) != PYTHON_ENFORCEMENT_SCHEMA_VERSION:
        return ValidationVerdict(False, "unknown_schema_version", "Unsupported Python enforcement evidence schema version")
    if evidence.get("language") != "python" or evidence.get("backend") != PYTHON_ENFORCEMENT_BACKEND:
        return ValidationVerdict(False, "wrong_backend", "Evidence is not Python enforcement evidence")
    if expected_head and evidence.get("headCommit") != expected_head:
        return ValidationVerdict(False, "stale_head", "Evidence head commit does not match the expected branch head")
    if evidence.get("status") != PythonEnforcementStatus.passed.value or evidence.get("passed") is not True:
        return ValidationVerdict(False, str(evidence.get("reasonCode") or "failed"), str(evidence.get("summary") or "Python enforcement did not pass"))
    coverage = evidence.get("coverage") or {}
    mutation = evidence.get("mutation") or {}
    if evidence.get("reasonCode") != "no_changed_python_targets":
        if coverage.get("passed") is not True:
            return ValidationVerdict(False, "coverage_gate_failed", "Python evidence did not include passing coverage")
        if coverage.get("no_executable_changed_lines") is True:
            return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Python enforcement passed"))
        if mutation.get("passed") is not True:
            return ValidationVerdict(False, "mutation_gate_failed", "Python evidence did not include passing mutation")
        if (
            mutation.get("scope") == "changed_lines"
            and _has_changed_line_payload(evidence, mutation)
            and int(mutation.get("changedLineMutantsGenerated") or 0) <= 0
        ):
            return ValidationVerdict(
                False,
                "missing_mutation_evidence",
                "Python changed-line mutation evidence did not include generated changed-line mutants",
            )
    return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Python enforcement passed"))


def format_evidence_markers(evidence: Mapping[str, Any]) -> str:
    lines = [
        f"[test-enforcer] python enforcement {evidence.get('status')} reason={evidence.get('reasonCode')} schema={evidence.get('schemaVersion')}",
    ]
    coverage = evidence.get("coverage") or {}
    mutation = evidence.get("mutation") or {}
    if coverage:
        state = "passed" if coverage.get("passed") else "failed"
        lines.append(
            "[test-enforcer] python diff line coverage "
            f"{float(coverage.get('rate') or 0.0):.2f}% {state} "
            f"({int(coverage.get('covered') or 0)}/{int(coverage.get('total') or 0)})"
        )
    if mutation:
        state = "passed" if mutation.get("passed") else "failed"
        if mutation.get("scope") == "changed_lines":
            lines.append(
                "[test-enforcer] python diff mutation score "
                f"{float(mutation.get('rate') or 0.0):.2f}% {state} "
                f"({int(mutation.get('survived') or 0)} diff survivors, "
                f"{int(mutation.get('changedLineMutantsGenerated') or 0)} diff mutants, "
                f"{int(mutation.get('generated') or 0)} file mutants)"
            )
        else:
            lines.append(
                "[test-enforcer] python diff mutation score "
                f"{float(mutation.get('rate') or 0.0):.2f}% {state} "
                f"({int(mutation.get('killed') or 0)}/{int(mutation.get('generated') or 0)} detected)"
            )
    lines.append(f"UTA_PYTHON_ENFORCEMENT_EVIDENCE={json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"


def _target_refs(repo: Path, target_values: Sequence[str], *, base_ref: str) -> List[TargetRef]:
    adapter = default_registry().adapter_for("python")
    values = list(target_values or []) or _changed_production_python_files(repo, base_ref)
    return [adapter.normalize_target(RawTargetSelection(target=value)) for value in values]


def _changed_production_python_files(repo: Path, base_ref: str) -> List[str]:
    output = _git_output(repo, "diff", "--name-only", f"{base_ref}...HEAD")
    if not output:
        return []
    return [path for path in output.splitlines() if _is_production_python_path(path)]


def _changed_lines_by_file(repo: Path, base_ref: str, paths: Sequence[str]) -> Dict[str, List[int]]:
    changed: Dict[str, List[int]] = {}
    for path in paths:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized:
            continue
        output = _git_output(repo, "diff", "--unified=0", f"{base_ref}...HEAD", "--", normalized)
        lines = _parse_added_diff_lines(output)
        changed[normalized] = sorted(lines)
    return changed


def _parse_added_diff_lines(diff_text: str) -> set[int]:
    lines: set[int] = set()
    current_line: Optional[int] = None
    for raw_line in str(diff_text or "").splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", raw_line)
        if hunk:
            current_line = int(hunk.group(1))
            continue
        if current_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.add(current_line)
            current_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            current_line += 1
    return lines


def _is_production_python_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    parts = normalized.split("/")
    if not normalized.endswith(".py"):
        return False
    if parts[0] in {"tests", "test"}:
        return False
    if any(part in {".venv", "venv", ".tox", ".nox", "site-packages", "__pycache__"} for part in parts):
        return False
    name = Path(normalized).name
    return name != "__init__.py" and not name.startswith("test_")


def _target_result_evidence(target: TargetRef, result: PythonVerificationResult) -> Dict[str, Any]:
    mutation_artifacts = result.mutation.artifacts if result.mutation else {}
    return {
        "target": target.as_selection(),
        "status": result.status,
        "reasonCode": result.reason_code,
        "testsPass": result.tests_pass,
        "message": result.message,
        "coverage": result.coverage.as_dict() if result.coverage else None,
        "mutation": result.mutation.as_dict() if result.mutation else None,
        "commands": [command.as_dict() for command in result.commands],
        "artifacts": dict(mutation_artifacts),
        "setup": {
            "setupStatus": result.setup_status,
            "environmentProfile": result.environment_profile,
            "dependencyFingerprints": dict(result.dependency_fingerprints),
            "cacheKey": result.cache_key,
        },
    }


def _aggregate_coverage(target_evidence: Sequence[Mapping[str, Any]], gate: float) -> Optional[Dict[str, Any]]:
    summaries = [item.get("coverage") for item in target_evidence if item.get("coverage")]
    if not summaries:
        return None
    covered = sum(int(summary.get("covered") or 0) for summary in summaries)
    total = sum(int(summary.get("total") or 0) for summary in summaries)
    scope = _aggregate_scope(summaries)
    changed_lines = _merge_changed_lines(summaries)
    all_passed = all(summary.get("passed") is True for summary in summaries)
    if scope == "changed_lines" and changed_lines and total == 0 and all_passed:
        rate = 100.0
    else:
        rate = 100.0 if total == 0 else round((covered / total) * 100.0, 4)
    no_executable_changed_lines = all(summary.get("no_executable_changed_lines") is True for summary in summaries)
    payload: Dict[str, Any] = {
        "covered": covered,
        "total": total,
        "rate": rate,
        "gate": gate,
        "passed": all_passed and rate >= gate,
        "no_executable_changed_lines": no_executable_changed_lines,
    }
    if scope:
        payload["scope"] = scope
    if changed_lines:
        payload["changed_lines"] = changed_lines
    return payload


def _aggregate_mutation(target_evidence: Sequence[Mapping[str, Any]], gate: float) -> Optional[Dict[str, Any]]:
    summaries = [item.get("mutation") for item in target_evidence if item.get("mutation")]
    if not summaries:
        return None
    generated = sum(int(summary.get("generated") or 0) for summary in summaries)
    killed = sum(int(summary.get("killed") or 0) for summary in summaries)
    survived = sum(int(summary.get("survived") or 0) for summary in summaries)
    no_coverage = sum(int(summary.get("no_coverage") or 0) for summary in summaries)
    changed_line_generated = sum(int(summary.get("changedLineMutantsGenerated") or 0) for summary in summaries)
    changed_line_killed = sum(int(summary.get("changedLineMutantsKilled") or 0) for summary in summaries)
    scope = _aggregate_scope(summaries)
    changed_lines = _merge_changed_lines(summaries)
    diff_survivors = [
        survivor
        for summary in summaries
        for survivor in (summary.get("diff_survivors") or [])
        if isinstance(survivor, dict)
    ]
    if scope == "changed_lines":
        has_changed_line_evidence = changed_line_generated > 0 or not changed_lines
        rate = 100.0 if survived == 0 and has_changed_line_evidence else 0.0
        passed = has_changed_line_evidence and rate >= gate
    else:
        denominator = killed + survived
        rate = 100.0 if denominator == 0 else round((killed / denominator) * 100.0, 4)
        passed = rate >= gate
    payload: Dict[str, Any] = {
        "generated": generated,
        "killed": killed,
        "survived": survived,
        "noCoverage": no_coverage,
        "changedLineMutantsGenerated": changed_line_generated,
        "changedLineMutantsKilled": changed_line_killed,
        "rate": rate,
        "gate": gate,
        "passed": passed,
    }
    if scope:
        payload["scope"] = scope
    if changed_lines:
        payload["changed_lines"] = changed_lines
    if diff_survivors:
        payload["diff_survivors"] = diff_survivors
    return payload


def _aggregate_scope(summaries: Sequence[Mapping[str, Any]]) -> Optional[str]:
    scopes = {str(summary.get("scope") or "") for summary in summaries}
    scopes.discard("")
    return next(iter(scopes)) if len(scopes) == 1 else None


def _merge_changed_lines(summaries: Sequence[Mapping[str, Any]]) -> Dict[str, List[int]]:
    merged: Dict[str, set[int]] = {}
    for summary in summaries:
        changed_lines = summary.get("changed_lines") or {}
        if not isinstance(changed_lines, Mapping):
            continue
        for path, lines in changed_lines.items():
            normalized = str(path or "").replace("\\", "/")
            if not normalized:
                continue
            merged.setdefault(normalized, set()).update(int(line) for line in lines or [] if int(line) > 0)
    return {path: sorted(lines) for path, lines in sorted(merged.items())}


def _has_changed_line_payload(evidence: Mapping[str, Any], mutation: Mapping[str, Any]) -> bool:
    for payload in (evidence.get("changedLines"), mutation.get("changed_lines")):
        if not isinstance(payload, Mapping):
            continue
        for lines in payload.values():
            if lines:
                return True
    return False


def _finalize(evidence: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(evidence)
    payload["evidenceId"] = ""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    payload["evidenceId"] = f"uta-python-enforcement:{digest[:16]}"
    return payload


def _git_output(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()
