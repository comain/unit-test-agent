"""Shaping what an enforcement run proves into the evidence contract.

Diff scope, filtered PIT targets, Surefire failures, declared tooling status
and selected-test quality all become keys on one evidence mapping. The field
names here are the contract other layers read, so they are deliberately kept
apart from the parsing that produces the raw values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from uta.language.java.enforcement_runner.parsing import (
    _failed_surefire_tests,
    _filtered_classes_from_pitest_patterns,
    _pitest_target_patterns,
)
from uta.language.java.enforcement_runner.planning import (
    _java_fqn_from_path,
    _java_test_paths_for_fqn,
)
from uta.language.java.enforcement_versions import (
    PARENT_ROOT_VERSION,
    TEST_ENFORCER_VERSION,
    PARENT_GENERIC_VERSION,
)
from uta.language.java.maven_project import test_enforcement_tooling_status
from uta.language.java.test_quality import scan_java_test_quality_evidence

#: The floor lives here so the message and the check cannot drift apart.
REQUIRED_TEST_ENFORCER_VERSION = TEST_ENFORCER_VERSION

_ROLLOUT_PATH = (
    f"The normal rollout path is example-parent-generic >= {PARENT_GENERIC_VERSION} or "
    f"example-root >= {PARENT_ROOT_VERSION} for Platform-family projects."
)

MISSING_EVIDENCE_SUMMARY = (
    "Missing UTA test-enforcement plugin/profile: Maven completed but no "
    "coverage/mutation evidence was produced. Required Maven plugin: "
    "resolved test-enforcer >= %s. %s" % (REQUIRED_TEST_ENFORCER_VERSION, _ROLLOUT_PATH)
)


def missing_evidence_summary(tooling: Optional[Any] = None) -> str:
    """Name the declared version, not only the required one.

    A project pinned below the floor used to get a message stating the
    requirement and nothing about what it actually declares, so whoever read
    the report had to go and find it. Upgrading the enforcer makes that the
    common case, which is why the found version belongs in the sentence.
    """
    declared = str(getattr(tooling, "version", "") or "").strip()
    artifact = str(getattr(tooling, "artifact_id", "") or "").strip()
    required = str(getattr(tooling, "required_version", "") or "").strip() or REQUIRED_TEST_ENFORCER_VERSION
    if not declared:
        return MISSING_EVIDENCE_SUMMARY
    subject = artifact or "test-enforcer"
    return (
        "UTA test-enforcement plugin is below the required version: %s declares "
        "%s, but %s or newer is required. %s"
        % (subject, declared, required, _ROLLOUT_PATH)
    )


def _tooling_evidence(tooling: Any) -> Dict[str, Any]:
    """The tooling block every missing-evidence result carries."""
    return {
        "available": bool(getattr(tooling, "available", False)),
        "artifactId": getattr(tooling, "artifact_id", ""),
        "version": getattr(tooling, "version", ""),
        "requiredVersion": getattr(tooling, "required_version", "") or REQUIRED_TEST_ENFORCER_VERSION,
        "reason": getattr(tooling, "reason", ""),
    }


def _with_filtered_target_evidence(evidence: Optional[Dict[str, Any]], output: str) -> Optional[Dict[str, Any]]:
    filtered_patterns = _pitest_target_patterns(output)
    if not evidence or not filtered_patterns:
        return evidence
    filtered_classes = _filtered_classes_from_pitest_patterns(
        filtered_patterns,
        evidence.get("changedClasses") or evidence.get("changed_classes") or [],
    )
    if not filtered_classes:
        return evidence
    updated = dict(evidence)
    updated["filteredTargetPatterns"] = filtered_patterns
    updated["filteredTargetClasses"] = filtered_classes
    return updated


def _with_surefire_failure_evidence(evidence: Optional[Dict[str, Any]], repo_path: Path) -> Optional[Dict[str, Any]]:
    failures = _failed_surefire_tests(repo_path)
    if not failures:
        return evidence
    updated = dict(evidence or {})
    updated["failedSurefireTests"] = failures
    return updated


def _with_failing_test_scope_evidence(evidence, partition):
    """Publish what left the run, and why, even when the run exited zero.

    The case this feature exists for -- a red suite tolerated by
    `-Dmaven.test.failure.ignore=true` -- exits zero, which is exactly when
    `_with_surefire_failure_evidence` attaches nothing. Without this the report
    would show a clean pass and no trace of the tests it was taken over.
    """
    updated = dict(evidence or {})
    updated["excludedFailingTestClasses"] = list(partition.excluded)
    updated["excludedCollidingTestClasses"] = list(partition.collateral)
    updated["retainedFailingTestClasses"] = list(partition.retained)
    updated["exclusionReason"] = partition.reason
    return updated


def _with_selected_test_quality_evidence(repo_path: Path, evidence: Dict[str, Any]) -> Dict[str, Any]:
    target_tests = evidence.get("targetTests")
    if not isinstance(target_tests, list) or not target_tests:
        return evidence
    test_paths: List[str] = []
    for test_fqn in target_tests:
        if not isinstance(test_fqn, str) or not test_fqn.strip():
            continue
        test_paths.extend(_java_test_paths_for_fqn(repo_path, test_fqn.strip()))
    if not test_paths:
        return evidence
    try:
        quality = scan_java_test_quality_evidence(repo_path, test_paths)
    except Exception:
        return evidence
    if not quality:
        return evidence
    updated = dict(evidence)
    updated["testQuality"] = quality
    return updated


def _java_classes_from_production_files(changed_production_java: Sequence[str]) -> List[str]:
    classes: List[str] = []
    for path_text in changed_production_java:
        fqn = _java_fqn_from_path(path_text, "src/main/java/")
        if fqn:
            classes.append(fqn)
    return list(dict.fromkeys(classes))


def _with_test_enforcement_tooling_evidence(
    repo_path: Path,
    cmd: Sequence[str],
    evidence: Optional[Dict[str, Any]],
    *,
    run_maven_command: Callable[..., Any],
) -> Dict[str, Any]:
    updated: Dict[str, Any] = dict(evidence or {})
    if isinstance(updated.get("tooling"), dict):
        return updated
    tooling = test_enforcement_tooling_status(
        repo_path,
        maven_bin=str(cmd[0]) if cmd else "mvn",
        run_maven_command=run_maven_command,
        profile_source_cmd=cmd,
    )
    updated["tooling"] = {
        "available": tooling.available,
        "artifactId": tooling.artifact_id,
        "version": tooling.version,
        "requiredVersion": tooling.required_version,
        "reason": tooling.reason,
    }
    return updated


def _diff_evidence(
    changed_java: Optional[List[str]],
    changed_production_java: Optional[List[str]],
    *,
    target_tests: Optional[Sequence[str]] = None,
    changed_modules: Optional[Sequence[str]] = None,
    target_sources: Optional[Sequence[str]] = None,
    filtered_changed_production_java: Optional[Sequence[str]] = None,
    base_ref: str = "",
) -> Dict[str, Any]:
    if changed_java is None or changed_production_java is None:
        return {}
    return {
        "baseRef": base_ref,
        "changedJavaFiles": list(changed_java),
        "changedProductionFiles": list(changed_production_java),
        "changedClasses": _java_classes_from_production_files(changed_production_java),
        "targetTests": list(target_tests or []),
        "changedModules": list(changed_modules or []),
        "targetSources": list(target_sources or []),
        "filteredChangedProductionFiles": list(filtered_changed_production_java or []),
    }


__all__ = [
    "MISSING_EVIDENCE_SUMMARY",
    "REQUIRED_TEST_ENFORCER_VERSION",
    "missing_evidence_summary",
    "_tooling_evidence",
    "_diff_evidence",
    "_java_classes_from_production_files",
    "_with_failing_test_scope_evidence",
    "_with_filtered_target_evidence",
    "_with_selected_test_quality_evidence",
    "_with_surefire_failure_evidence",
    "_with_test_enforcement_tooling_evidence",
]
