from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord
from uta.shared.opencode_snapshot import opencode_config_snapshot
from uta.shared.config import settings
from uta.enforcement.ci import BaseCiLanguageHandler
from uta.language.java.enforcement_runner.evidence import _java_classes_from_production_files
from uta.language.java.compile import classify_compile_errors
from uta.language.java.runtime import RepositoryJavaRuntimeResolver
from uta.tasks.manager import TaskManager


class JavaCiLanguageHandler(BaseCiLanguageHandler):
    language = "java"
    quality_gate_backend = "maven_enforcer"

    def __init__(self, runner, runtime_resolver: Optional[RepositoryJavaRuntimeResolver] = None) -> None:
        super().__init__(runner)
        self.runtime_resolver = runtime_resolver

    def runner_for(self, record: CiTaskRecord):
        if self.runner is None or self.runtime_resolver is None:
            return self.runner
        return self.runner.with_java_home(self.runtime_resolver.resolve(record.request.app_name))

    def create_repair_task(
        self,
        *,
        task_manager: TaskManager,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        priority: int,
        base_ref: str,
        coverage_gate: float,
        mutation_gate: float,
        rdc_context: Dict[str, Any],
        rdc_context_path: Optional[str],
    ) -> int:
        class_fqns = self._repair_class_fqns(record, request, repo_path=repo_path, base_ref=base_ref)
        if not class_fqns:
            raise ValueError(
                "Java CI incremental repair requires explicit or Maven-filtered target classes; "
                "no safe repair target classes were found in enforcement evidence"
            )
        # The OpenCode selection travels on every repair task; the resolved
        # java_home is layered on top rather than replacing it, which is what
        # left Java repair tasks unpinned.
        config_snapshot = opencode_config_snapshot()
        if self.runtime_resolver is not None:
            config_snapshot["java_home"] = self.runtime_resolver.resolve(record.request.app_name)
        return task_manager.create_task(
            repo_path=str(repo_path),
            class_fqns=class_fqns,
            select_all=False,
            priority=priority,
            branch_name=record.request.branch,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            config_snapshot=config_snapshot,
            quality_mode="ci_incremental",
            quality_gate_backend=self.quality_gate_backend,
            quality_gate_command=self._quality_gate_command(record),
            rdc_context=rdc_context,
            rdc_context_path=rdc_context_path,
        )

    def scoring_survivors(self, *, record: CiTaskRecord, result, repo_path: Path):
        from uta.language.java.equivalence import java_scoring_survivors

        payload = result.model_dump(mode="json")
        return java_scoring_survivors(payload, repo_path)

    def gate_failure_flags(self, *, record: CiTaskRecord, result):
        from uta.language.java.equivalence import java_gate_failure_flags

        payload = result.model_dump(mode="json")
        return java_gate_failure_flags(payload)

    def repair_target_ids(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
    ) -> tuple[str, ...]:
        return tuple(self._repair_class_fqns(record, request, repo_path=repo_path, base_ref=base_ref))

    def _quality_gate_command(self, record: CiTaskRecord) -> str:
        command = super()._quality_gate_command(record)
        if not command:
            return command
        if _is_filter_diff_preflight_command(command):
            command = settings.ci_enforcement_command
        return shlex.join(
            part
            for part in shlex.split(command)
            if not (
                part.startswith("-DtargetTests=")
                or part.startswith("-Dtest=")
                or part.startswith("-Dsurefire.failIfNoSpecifiedTests=")
                # The CI run's PIT compatibility credentials are good for that
                # invocation and no other: the nonce names an evidence directory
                # inside the workspace, which the repair refresh deletes before
                # the repair ever runs. Inheriting them hands every repair task a
                # dead path, and prepare_command reissues real ones per run.
                or part.startswith("-Duta.pit.compat.")
            )
        )

    @staticmethod
    def _repair_class_fqns(
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        *,
        repo_path: Path,
        base_ref: str,
    ) -> list[str]:
        classes = [
            target_id.split(":", 1)[1]
            for target_id in request.target_ids
            if target_id.startswith("class:") and target_id.split(":", 1)[1]
        ]
        enforcement = record.enforcement_result or {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        baseline_failed_classes = _pit_baseline_failure_target_classes(enforcement, evidence)
        if baseline_failed_classes:
            classes.extend(baseline_failed_classes)
            return list(dict.fromkeys(classes))
        # Invariant: repair scope must cover every selected test that the original
        # CI enforcement command compiled or ran; otherwise a scoped repair can
        # pass while the final rerun fails on an omitted selected test.
        classes.extend(_compile_failure_target_classes(enforcement, evidence))
        classes.extend(_string_list_values(enforcement, ("failedClasses", "failed_classes")))
        classes.extend(_string_list_values(evidence, ("failedClasses", "failed_classes")))
        classes.extend(
            _string_list_values(
                evidence,
                ("filteredTargetClasses", "filtered_target_classes", "effectiveChangedClasses", "effective_changed_classes"),
            )
        )
        classes.extend(
            _string_list_values(
                enforcement,
                ("filteredTargetClasses", "filtered_target_classes", "effectiveChangedClasses", "effective_changed_classes"),
            )
        )
        if not classes:
            classes.extend(_filtered_classes_from_pitest_output(enforcement, evidence))
        if not classes:
            # Every source above needs enforcement to have reached the tests:
            # a PIT baseline failure, a compile failure, failed classes, the
            # Maven-filtered target list, PIT output. When a changed class has
            # no test at all, enforcement stops before naming any target and
            # all of them come back empty -- so the one case a repair most
            # exists for was the one case repair refused to start, with
            # "no safe repair target classes were found".
            #
            # The changed production classes are the honest scope here. They
            # are a superset of what needs fixing, so the invariant above --
            # that repair scope covers every selected test enforcement ran --
            # still holds: it ran none.
            classes.extend(_unfiltered_fallback_classes(evidence))
        return list(dict.fromkeys(classes))


def _unfiltered_fallback_classes(evidence: Dict[str, Any]) -> list[str]:
    """The changed classes worth repairing when enforcement named no targets.

    The Maven enforcer already decides which changed sources carry an
    obligation: it drops pure wrappers, beans and other non-executable
    sources, and UTA publishes what survives as
    `filteredChangedProductionFiles`. `changedClasses` is the *unfiltered*
    list, so falling back to it sent repair at classes the enforcer had
    deliberately excluded -- and the fix session then wrote tests for beans.

    An empty filtered list is an answer, not a missing one: it means nothing
    changed that needs covering, so there is no repair target. Only an absent
    key -- evidence from a plugin too old to publish it -- falls back to the
    unfiltered classes.
    """
    for key in ("filteredChangedProductionFiles", "filtered_changed_production_files"):
        if key in evidence:
            files = _string_list_values(evidence, (key,))
            return _java_classes_from_production_files(files)
    return _string_list_values(evidence, ("changedClasses", "changed_classes"))


def _is_filter_diff_preflight_command(command: str) -> bool:
    parts = shlex.split(command)
    has_initialize = "initialize" in parts
    has_verify_or_later = any(goal in parts for goal in ("test", "verify", "install", "deploy"))
    skips_pitest = any(part == "-DskipPitest" or part.startswith("-DskipPitest=true") for part in parts)
    return has_initialize and skips_pitest and not has_verify_or_later


def _pit_baseline_failure_target_classes(enforcement: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
    output = "\n".join(
        str(enforcement.get(key) or "")
        for key in ("summary", "stdout", "stderr")
    )
    if "Mutation testing requires a green suite" not in output and "did not pass without mutation" not in output:
        return []
    failing_tests = _pit_baseline_failing_tests(output)
    failing_tests.extend(_failed_surefire_tests_from_evidence(evidence))
    # A test class the enforcement run excluded as unrelated to the diff is not
    # something a generated test can fix; sending it to repair is the misread
    # this exclusion exists to prevent.
    excluded = set(
        _string_list_values(
            evidence,
            (
                "excludedFailingTestClasses",
                "quarantinedTestClasses",
                "preQuarantinedTestClasses",
            ),
        )
    )
    if excluded:
        failing_tests = [test for test in failing_tests if test not in excluded]
    if not failing_tests:
        return []
    candidates = _string_list_values(
        evidence,
        ("filteredTargetClasses", "filtered_target_classes", "effectiveChangedClasses", "effective_changed_classes"),
    )
    if not candidates:
        candidates = _string_list_values(evidence, ("changedClasses", "changed_classes"))
    return _target_classes_for_failing_tests(failing_tests, candidates)


def _failed_surefire_tests_from_evidence(evidence: Dict[str, Any]) -> list[str]:
    failures = evidence.get("failedSurefireTests") if isinstance(evidence.get("failedSurefireTests"), list) else []
    tests: list[str] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        test_name = str(failure.get("testName") or "").strip()
        class_name = str(failure.get("className") or "").strip()
        if test_name:
            tests.append(test_name.rsplit(".", 1)[0])
        elif class_name:
            tests.append(class_name)
    return list(dict.fromkeys(tests))


def _compile_failure_target_classes(enforcement: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
    output = "\n".join(str(enforcement.get(key) or "") for key in ("summary", "stdout", "stderr"))
    if "COMPILATION ERROR" not in output and "Compilation failure" not in output and "cannot find symbol" not in output:
        return []

    selected_tests = _selected_target_tests(enforcement, evidence)
    if not selected_tests:
        return []

    failing_tests = _compile_failing_selected_tests(output, selected_tests)
    if not failing_tests:
        return []

    candidates = _string_list_values(evidence, ("changedClasses", "changed_classes"))
    candidates.extend(
        _string_list_values(
            evidence,
            ("filteredTargetClasses", "filtered_target_classes", "effectiveChangedClasses", "effective_changed_classes"),
        )
    )
    matched = _target_classes_for_failing_tests(failing_tests, candidates)
    matched.extend(_inferred_target_classes_for_tests(failing_tests))
    return list(dict.fromkeys(matched))


def _selected_target_tests(enforcement: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
    tests = _string_list_values(evidence, ("targetTests", "target_tests"))
    tests.extend(_string_list_values(enforcement, ("targetTests", "target_tests")))
    command = enforcement.get("command") or []
    command_text = command if isinstance(command, str) else shlex.join(str(item) for item in command)
    for part in shlex.split(command_text):
        if part.startswith("-DtargetTests="):
            tests.extend(value.strip() for value in part.split("=", 1)[1].split(",") if value.strip())
    return list(dict.fromkeys(tests))


def _compile_failing_selected_tests(output: str, selected_tests: Sequence[str]) -> list[str]:
    selected_by_simple = {test.rsplit(".", 1)[-1]: test for test in selected_tests}
    selected = set(selected_tests) | set(selected_by_simple)
    failing: list[str] = []
    for error in classify_compile_errors(output):
        test_name = _test_fqn_from_compile_error_path(error.file)
        simple = test_name.rsplit(".", 1)[-1] if test_name else Path(error.file).stem
        selected_name = selected_by_simple.get(simple)
        if test_name in selected:
            failing.append(test_name)
        elif simple in selected:
            failing.append(selected_name or simple)
    return list(dict.fromkeys(failing))


def _test_fqn_from_compile_error_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "src/test/java/"
    if marker in normalized:
        return normalized.split(marker, 1)[1].removesuffix(".java").replace("/", ".")
    return Path(normalized).stem


def _inferred_target_classes_for_tests(failing_tests: Sequence[str]) -> list[str]:
    inferred: list[str] = []
    for test_name in failing_tests:
        package, _, simple = test_name.rpartition(".")
        production_simple = _production_simple_name_for_test(simple)
        if production_simple:
            inferred.append(f"{package}.{production_simple}" if package else production_simple)
    return inferred


def _pit_baseline_failing_tests(output: str) -> list[str]:
    tests: list[str] = []
    for match in re.finditer(r"testClass=([A-Za-z_][\w.$]*(?:Test|Tests|IT))\b", output):
        tests.append(match.group(1).replace("$", "."))
    for line in output.splitlines():
        stripped = re.sub(r"^\s*(?:\[ERROR\]\s*)+", "", line).strip()
        if not stripped:
            continue
        match = re.match(r"([A-Za-z_][\w.$]*(?:Test|Tests|IT))(?:[.#][\w$]+)?(?::\d+)?\b", stripped)
        if match:
            tests.append(match.group(1).replace("$", "."))
    return list(dict.fromkeys(tests))


def _target_classes_for_failing_tests(failing_tests: Sequence[str], candidates: Sequence[str]) -> list[str]:
    candidate_values = [str(item).strip() for item in candidates if str(item or "").strip()]
    matched: list[str] = []
    for test_name in failing_tests:
        test_simple = test_name.rsplit(".", 1)[-1]
        possible_simple = _production_simple_name_for_test(test_simple)
        if not possible_simple:
            continue
        suffix = f".{possible_simple}"
        for candidate in candidate_values:
            candidate_simple = candidate.rsplit(".", 1)[-1]
            if candidate_simple == possible_simple or candidate.endswith(suffix):
                matched.append(candidate)
    return list(dict.fromkeys(matched))


def _production_simple_name_for_test(test_simple: str) -> str:
    for suffix in ("Test", "Tests", "IT"):
        if test_simple.endswith(suffix) and len(test_simple) > len(suffix):
            return test_simple[: -len(suffix)]
    return ""


def _string_list_values(payload: Dict[str, Any], keys: Iterable[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if value)
    return values


def _filtered_classes_from_pitest_output(enforcement: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
    patterns = _pitest_target_patterns(str(enforcement.get("stdout") or ""))
    if not patterns:
        return []
    changed_classes = _string_list_values(evidence, ("changedClasses", "changed_classes"))
    return _filtered_classes_from_patterns(patterns, changed_classes)


def _pitest_target_patterns(output: str) -> list[str]:
    patterns: list[str] = []
    for match in re.finditer(r"\bpitest\.targets=\d+\s+\[([^\]]*)\]", output, flags=re.IGNORECASE):
        for item in match.group(1).split(","):
            value = item.strip()
            if value:
                patterns.append(value)
    return list(dict.fromkeys(patterns))


def _filtered_classes_from_patterns(patterns: Sequence[str], changed_classes: Sequence[str]) -> list[str]:
    matched: list[str] = []
    changed = [str(item) for item in changed_classes if item]
    for pattern in patterns:
        normalized = str(pattern).strip()
        while normalized.endswith("*"):
            normalized = normalized[:-1]
        normalized = normalized.strip(".")
        if not normalized:
            continue
        if "." in normalized:
            matched.append(normalized)
            continue
        suffix = f".{normalized}"
        matched.extend(item for item in changed if item == normalized or item.endswith(suffix))
    return list(dict.fromkeys(matched))
