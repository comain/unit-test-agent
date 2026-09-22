"""The UTA Maven test-enforcement runner (Java backend quality gate).

``MavenEnforcementRunner`` is the orchestrator and stays the whole public
surface: plan the command from the diff, run it, classify what came back. The
four layers it orchestrates are separate modules -- ``planning`` builds the
argument vector, ``execution`` launches it, ``parsing`` reads the output, and
``evidence``/``classification`` turn that into the enforcement contract.

The private helpers are still reachable as attributes on the class because
callers and tests bind to them there; the aliases at the bottom of the class
keep that contract while the implementations live with their layer.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from uta.enforcement.enforcement import (
    QualityGateResult,
    QualityGateStatus,
    RunCommand,
    run_bounded_command,
)
from uta.language.java.enforcement_runner import evidence as _evidence
from uta.language.java.enforcement_runner import execution as _execution
from uta.language.java.enforcement_runner import parsing as _parsing
from uta.language.java.enforcement_runner import planning as _planning
from uta.language.java.enforcement_runner.classification import (
    _classify_completed,
    _mutation_score_below_ci_gate,
)
from uta.language.java.enforcement_runner.hanging import HangingRun, stalled
from uta.language.java.enforcement_runner.failing_test_scope import (
    _expand_colliding_simple_name_exclusions,
    _without_exclusions,
    _without_module_emptying_exclusions,
    partition_failing_tests,
)
from uta.language.java.enforcement_runner.artifacts import _clear_module_test_artifacts
from uta.language.java.enforcement_runner.evidence import (
    MISSING_EVIDENCE_SUMMARY,
    _tooling_evidence,
    missing_evidence_summary,
)
from uta.language.java.enforcement_runner.parsing import (
    _filtered_classes_from_pitest_patterns,
    _has_test_enforcer_marker,
    _pitest_target_patterns,
)
from uta.language.java.enforcement_runner.quarantine import HangingTestQuarantineStore
from uta.language.java.enforcement_runner.planning import (
    JAVA_CI_COVERAGE_GATE_PROPERTY,
    JAVA_CI_MUTATION_GATE_PROPERTY,
    _java_fqn_from_path,
)
from uta.language.java.maven_project import (
    test_enforcement_tooling_status,
    with_default_profile_args,
)
from uta.language.java.maven_compat.launcher import PitCompatibilityError, validate_completion


def _write_surefire_positive_inventory(repo_path: Path, test_classes: Sequence[str]) -> Path:
    """Persist the deterministic test inventory outside Maven's cleaned targets."""
    inventory_file = repo_path / ".uta_cache/maven/surefire-positive-tests.txt"
    inventory_file.parent.mkdir(parents=True, exist_ok=True)
    patterns = [f"**/{name.rsplit('.', 1)[-1]}.java" for name in test_classes]
    inventory_file.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    return inventory_file.resolve()


class MavenEnforcementRunner:

    def with_java_home(self, java_home: str) -> "MavenEnforcementRunner":
        """Return an isolated runner copy bound to one repository's runtime.

        A copy, not a mutation: the shared runner is reused across tasks, and
        rebinding it in place would leak one repository's JDK into the next.
        """
        import copy as _copy

        selected = _copy.copy(self)
        selected.java_home = str(java_home or "").strip()
        return selected

    def __init__(
        self,
        command: str,
        timeout_seconds: int = 1800,
        run_command: Optional[RunCommand] = None,
        base_ref: str = "origin/master",
        java_home: str = "",
        coverage_gate: float = 95.0,
        mutation_gate: float = 100.0,
        maven_central_mirror_url: str = "",
        preserve_explicit_target_scope: bool = False,
        full_run: bool = False,
        exclude_unrelated_failing_tests: bool = False,
        stall_detection_enabled: bool = True,
        stall_seconds: int = 600,
        stall_retries: int = 1,
        hanging_test_quarantine_enabled: bool = True,
        quarantine_store: Optional[HangingTestQuarantineStore] = None,
        quarantine_ttl_days: int = 14,
        generated_test_classes: Sequence[str] = (),
    ) -> None:
        # Two modes, matching what the Maven test-enforcer supports.
        #
        # Full run is the CI gate: the plugin gets the changed modules and
        # decides what carries an obligation, which it does with rules UTA
        # cannot see (accessor-only changes, testability-hint suffixes,
        # entry-wrapper annotations). Module selection still comes from the git
        # diff -- that never needed the filter-diff preflight.
        #
        # Targeted run is for test generation and repair, which work one batch
        # at a time and must stay in that scope.
        self.full_run = bool(full_run)
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.run_command = run_command or subprocess.run
        self.base_ref = base_ref
        self.java_home = java_home.strip()
        self.coverage_gate = float(coverage_gate)
        self.mutation_gate = float(mutation_gate)
        self.maven_central_mirror_url = maven_central_mirror_url.strip()
        self.preserve_explicit_target_scope = bool(preserve_explicit_target_scope)
        # Off unless a caller opts in: with it on, a run whose failures are all
        # unrelated to the diff is executed twice.
        self.exclude_unrelated_failing_tests = bool(exclude_unrelated_failing_tests)
        self.stall_detection_enabled = bool(stall_detection_enabled)
        self.stall_seconds = min(max(1, stall_seconds), timeout_seconds)
        self.stall_retries = min(max(0, stall_retries), 3)
        self.hanging_test_quarantine_enabled = bool(hanging_test_quarantine_enabled)
        self.quarantine_store = quarantine_store
        self.quarantine_ttl_days = quarantine_ttl_days
        self.generated_test_classes = tuple(generated_test_classes)

    def run(self, repo_path: Path) -> QualityGateResult:
        cmd = shlex.split(self.command)
        self._validate_command(cmd)
        changed_java = self._changed_java_files(repo_path)
        changed_production_java = self._changed_production_java_files(changed_java)
        diff_evidence = self._diff_evidence(changed_java, changed_production_java)
        target_tests = None
        changed_modules = []
        if changed_production_java == []:
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=cmd,
                stdout="No changed production Java files under origin/master...HEAD.",
                summary="UTA test-enforcement passed; no changed production Java files",
                evidence=diff_evidence,
            )
        if changed_production_java is not None and self.full_run:
            changed_modules = self._maven_modules_for_changed_files(repo_path, changed_production_java)
            diff_evidence = self._diff_evidence(
                changed_java,
                changed_production_java,
                changed_modules=changed_modules,
            )
            cmd = self._with_changed_modules(cmd, changed_modules)
        elif changed_production_java is not None:
            # Targeted mode: test generation and repair work one batch at a
            # time, and the scope is whatever the caller pinned in the command.
            # Nothing is re-derived here -- the plugin still decides what
            # carries an obligation when the real run happens.
            filtered_production_java = self._scoped_changed_production_java(
                cmd, changed_production_java
            )
            target_tests = self._pitest_target_tests(repo_path, changed_java, filtered_production_java)
            changed_modules = self._maven_modules_for_changed_files(repo_path, filtered_production_java)
            diff_evidence = self._diff_evidence(
                changed_java,
                changed_production_java,
                target_tests=target_tests,
                changed_modules=changed_modules,
                target_sources=filtered_production_java,
                filtered_changed_production_java=filtered_production_java,
            )
            diff_evidence = self._with_selected_test_quality_evidence(repo_path, diff_evidence)
            if target_tests:
                cmd = self._with_target_tests(cmd, target_tests)
            cmd = self._with_target_sources(cmd, filtered_production_java)
            cmd = self._with_changed_modules(cmd, changed_modules)
        cmd = self._with_ci_gate_properties(cmd)
        cmd = with_default_profile_args(cmd, repo_path)
        # Invariant: profile/inherited versions are checked against Maven's active
        # reactor even when tests already exist. "Not found in raw POM" is not
        # evidence of a compatible version (dms-order-core task 2738).
        tooling = test_enforcement_tooling_status(
            repo_path, maven_bin=cmd[0], run_maven_command=self._run_command,
            profile_source_cmd=cmd,
        )
        diff_evidence["tooling"] = _tooling_evidence(tooling)
        if not tooling.available or target_tests == []:
            return QualityGateResult(
                status=QualityGateStatus.missing_evidence,
                passed=False,
                command=cmd,
                summary=missing_evidence_summary(tooling) if not tooling.available else (
                    "UTA test-enforcement cannot run PIT safely because no related "
                    "targetTests were found for changed production Java files"
                ),
                evidence={
                    **diff_evidence,
                    "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                    "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                },
            )
        hanging = HangingRun(
            self, repo_path, changed_java, changed_production_java
        )
        cmd = hanging.preexclude(cmd)
        previous_surefire_reports = (
            _parsing._surefire_report_snapshot(repo_path)
            if self.full_run
            and (self.exclude_unrelated_failing_tests or self.stall_detection_enabled)
            else None
        )
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                remaining = max(0.001, deadline - time.monotonic())
                completed = self._run_command(cmd, repo_path, timeout=remaining)
                if not self.stall_detection_enabled or not stalled(completed):
                    break
                name = hanging.detect(completed)
                if not name or hanging.reason:
                    return hanging.failure(cmd, completed, diff_evidence)
                excluded_cmd, excluded, reason = hanging.command(
                    cmd,
                    hanging.pre + hanging.quarantined + [name],
                    completed.stdout or "",
                )
                if not excluded:
                    hanging.reason = reason or "test-could-not-be-quarantined"
                    return hanging.failure(cmd, completed, diff_evidence)
                hanging.observe(name)
                hanging.quarantined.append(name)
                hanging.retries += 1
                cmd = excluded_cmd
                _clear_module_test_artifacts(repo_path, changed_modules)
        except subprocess.TimeoutExpired as exc:
            return QualityGateResult(
                status=QualityGateStatus.timeout,
                passed=False,
                command=cmd,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                summary="UTA test-enforcement timed out",
                evidence=hanging.evidence(diff_evidence),
            )
        except OSError as exc:
            if isinstance(exc, PitCompatibilityError) and exc.completed is not None:
                return self._pit_compatibility_result(exc, cmd, repo_path, diff_evidence)
            return QualityGateResult(
                status=QualityGateStatus.command_error,
                passed=False,
                command=cmd,
                stderr=str(exc),
                summary=str(exc) if isinstance(exc, PitCompatibilityError) else "UTA test-enforcement command failed to start",
                evidence=hanging.evidence({
                    **diff_evidence,
                    **({"repairEligible": False, "failureKind": "pit_compatibility_failed"}
                       if isinstance(exc, PitCompatibilityError) else {}),
                }),
            )

        if self.full_run:
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode == 0 and not _has_test_enforcer_marker(output):
                # A green build that never ran the plugin is the vacuous pass
                # this whole mode has to rule out. A red build needs no such
                # guard -- it is classified as a failure either way.
                return QualityGateResult(
                    status=QualityGateStatus.missing_evidence,
                    passed=False,
                    command=cmd,
                    returncode=completed.returncode,
                    stdout=completed.stdout or "",
                    stderr=completed.stderr or "",
                    summary=MISSING_EVIDENCE_SUMMARY,
                    evidence={
                        **diff_evidence,
                        "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                        "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                    },
                )
            diff_evidence = self._with_plugin_scope_evidence(
                repo_path, output, changed_java, changed_production_java or [], diff_evidence
            )
            # Only now are the target tests known -- the plugin names them --
            # and the report's test-quality panel reads them, so this has to
            # come after the scope evidence rather than before the run.
            diff_evidence = self._with_selected_test_quality_evidence(repo_path, diff_evidence)
            cmd, completed, diff_evidence = self._rerun_without_unrelated_failures(
                cmd,
                completed,
                repo_path,
                changed_java=changed_java,
                changed_production_java=changed_production_java,
                changed_modules=changed_modules,
                evidence=diff_evidence,
                previous_surefire_reports=previous_surefire_reports,
            )
        # The command Maven actually ran, not the one the caller asked for: only
        # the prepared form carries this invocation's PIT compat credentials.
        # An injected runner returns them as a tuple (uta_enforce_core's
        # CommandResult normalises args), so a list-only check silently left
        # `cmd` as the configured command -- which, for a repair task, is the CI
        # record's stored command complete with a past run's one-time nonce.
        # validate_completion then checked an evidence path belonging to another
        # run and reported this one as unverified.
        executed_args = getattr(completed, "args", None)
        if isinstance(executed_args, (list, tuple)):
            cmd = list(executed_args)
        result = self._classify_completed(cmd, completed, repo_path, evidence=diff_evidence)
        result = hanging.annotate(result)
        if result.passed:
            try:
                compatibility = validate_completion(cmd)
                if compatibility:
                    result.evidence = {**(result.evidence or {}), "pitCompatibility": compatibility}
            except OSError as exc:
                result = result.model_copy(update=_pit_compatibility_update(exc, result))
        return result

    def _pit_compatibility_result(
        self,
        exc: PitCompatibilityError,
        cmd: List[str],
        repo_path: Path,
        diff_evidence: Dict[str, Any],
    ) -> QualityGateResult:
        """Report what this run's gates said, then why it still cannot stand.

        Completion evidence going missing does not erase the coverage and
        mutation numbers Maven already printed. A failure named only by the
        bookkeeping sends readers to the wrong problem -- and when a gate really
        did miss, that gate is the failure worth reporting.
        """
        completed = exc.completed
        executed_args = getattr(completed, "args", None)
        if isinstance(executed_args, (list, tuple)):
            cmd = list(executed_args)
        result = self._classify_completed(cmd, completed, repo_path, evidence=diff_evidence)
        return result.model_copy(update=_pit_compatibility_update(exc, result))

    def _rerun_without_unrelated_failures(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo_path: Path,
        *,
        changed_java: Optional[List[str]],
        changed_production_java: Optional[List[str]],
        changed_modules: Sequence[str],
        evidence: Dict[str, Any],
        previous_surefire_reports: Optional[Dict[str, tuple[int, int]]],
    ) -> tuple[List[str], subprocess.CompletedProcess[str], Dict[str, Any]]:
        """Take the verdict again with the diff's own failures, and only those.

        The first run tolerates a red suite, so JaCoCo has already credited every
        line those failing tests walked through while PIT has already discarded
        them. Re-running with the unrelated failures excluded from both stages is
        what makes the two gates describe the same test set.

        Clearing the module's Jacoco and Surefire output first is not
        housekeeping: the agent appends to `jacoco.exec`, so without it the
        second run reports the first run's coverage and the exclusion silently
        does nothing.
        """
        if not self.exclude_unrelated_failing_tests:
            return cmd, completed, evidence

        results = _parsing._surefire_test_class_results(repo_path, previous_surefire_reports)
        # Relatedness has to mean one thing. The plugin decides which changed
        # sources carry an obligation, and UTA records that as
        # `filteredChangedProductionFiles`; the unfiltered list also holds the
        # sources the plugin exempted, which UTA never generates for, never
        # mutates, and cannot repair. Partitioning on the unfiltered list made a
        # single exempt file -- a shared config helper referenced across the
        # suite -- retain six failing classes and fail the run for code nothing
        # was ever asked to cover.
        obligated_production_java = _obligated_changed_production_files(
            evidence, changed_production_java
        )
        partition = partition_failing_tests(
            [name for name, failed in results.items() if failed],
            repo_path=repo_path,
            changed_java=changed_java,
            changed_production_java=obligated_production_java,
        )
        evidence = _evidence._with_failing_test_scope_evidence(evidence, partition)
        if not partition.excluded:
            return cmd, completed, evidence
        first_run_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        classes_by_module = _parsing._surefire_test_classes_by_module(
            repo_path, previous_surefire_reports
        )
        source_inventoried: List[str] = []
        if _parsing._reactor_left_modules_unbuilt(first_run_output):
            # The positive inventory can only name tests the first run reported,
            # and a fail-fast reactor writes none for the modules after the
            # failure -- which is the usual shape here, since PIT stops the very
            # module whose red suite this exclusion exists for. Name those
            # modules' tests from source; one that cannot be named refuses the
            # rerun rather than silently dropping out of it.
            declared = _planning._declared_test_classes_by_module(
                repo_path,
                _parsing._reactor_skipped_modules(first_run_output),
                include_tests_suffix=_parsing._surefire_defaults_include_tests_suffix(first_run_output),
            )
            if declared is None:
                return cmd, completed, _evidence._with_failing_test_scope_evidence(
                    evidence, _without_exclusions(partition, "incomplete-test-inventory")
                )
            for module, classes in declared.items():
                classes_by_module.setdefault(module, set()).update(classes)
            source_inventoried = sorted(declared)
        for narrow in (_expand_colliding_simple_name_exclusions, _without_module_emptying_exclusions):
            partition = narrow(partition, classes_by_module)
            evidence = _evidence._with_failing_test_scope_evidence(evidence, partition)
            if not partition.all_excluded:
                return cmd, completed, evidence

        # Read from the first run's own output: the coverage block on `evidence`
        # is only populated on the early-return paths, and this number is the
        # signal that says whether the exclusion changed a verdict at all.
        first_run_coverage = _parsing._diff_coverage_rates(first_run_output)
        excluded_cmd, selector_reason = self._excluding_test_classes(
            repo_path, cmd, partition.all_excluded, first_run_output, classes_by_module
        )
        if excluded_cmd == cmd:
            # Nothing could be said to this reactor that expresses the intent.
            return cmd, completed, _evidence._with_failing_test_scope_evidence(
                evidence,
                _without_exclusions(partition, selector_reason or "no-usable-test-selector"),
            )
        _clear_module_test_artifacts(repo_path, changed_modules)
        try:
            rerun = self._run_command(excluded_cmd, repo_path)
        except subprocess.TimeoutExpired:
            raise
        except PitCompatibilityError as exc:
            return excluded_cmd, subprocess.CompletedProcess(excluded_cmd, 2, "", str(exc)), {**evidence, "repairEligible": False, "failureKind": "pit_compatibility_failed"}
        except OSError:
            return cmd, completed, evidence

        rerun_output = (rerun.stdout or "") + "\n" + (rerun.stderr or "")
        if _parsing._has_required_evidence(first_run_output) and not _parsing._has_required_evidence(
            rerun_output
        ):
            # The re-run exists to describe the same run more honestly, never to
            # replace a verdict with silence. Whatever went wrong -- a selector
            # this reactor read differently, a module the first run never reached
            # -- the first run's gates are the better evidence, so keep them.
            return cmd, completed, _evidence._with_failing_test_scope_evidence(
                evidence, _without_exclusions(partition, "rerun-produced-no-evidence")
            )
        evidence = dict(evidence)
        if first_run_coverage:
            evidence["coverageBeforeExclusion"] = min(first_run_coverage)
        if source_inventoried:
            evidence["sourceInventoriedModules"] = source_inventoried
        return excluded_cmd, rerun, evidence

    def _excluding_test_classes(
        self,
        repo_path: Path,
        cmd: List[str],
        excluded: Sequence[str],
        first_run_output: str,
        classes_by_module: Dict[str, Any],
    ) -> tuple[List[str], str]:
        """Select the complete first-pass test inventory minus excluded failures."""
        known: set = set()
        for module_classes in classes_by_module.values():
            known |= set(module_classes)
        kept = sorted(known - set(excluded))
        if not kept:
            return list(cmd), "empty-positive-test-inventory"

        if _parsing._surefire_supports_includes_file(first_run_output):
            try:
                inventory_file = _write_surefire_positive_inventory(repo_path, kept)
            except OSError:
                return list(cmd), "positive-test-inventory-write-failed"
            return _planning._with_selected_test_file(cmd, inventory_file, excluded), ""

        selected = _planning._with_selected_test_classes(cmd, kept, excluded)
        reason = "" if selected != cmd else "positive-test-selector-too-large"
        return selected, reason

    def _with_plugin_scope_evidence(
        self,
        repo_path: Path,
        output: str,
        changed_java: Optional[List[str]],
        changed_production_java: List[str],
        diff_evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record the scope the plugin chose, not the scope UTA guessed.

        Repair reads these keys to decide what to work on, so they have to name
        the plugin's obligation list -- the classes it reported under
        `pitest.targets` -- and the changed files those classes came from.
        """
        by_class = {}
        for path_text in changed_production_java:
            fqn = _java_fqn_from_path(path_text, "src/main/java/")
            if fqn:
                by_class.setdefault(fqn, path_text)
        patterns = _pitest_target_patterns(output)
        filtered_classes = [
            fqn
            for fqn in _filtered_classes_from_pitest_patterns(patterns, list(by_class))
            if fqn in by_class
        ]
        filtered_files = [by_class[fqn] for fqn in filtered_classes]
        evidence = dict(diff_evidence)
        evidence["pitestTargetPatterns"] = patterns
        evidence["filteredTargetClasses"] = filtered_classes
        evidence["filteredChangedProductionFiles"] = filtered_files
        evidence["targetTests"] = self._pitest_target_tests(
            repo_path, changed_java, filtered_files
        )
        return evidence

    # -- bound to this runner's configuration --------------------------------

    def _run_command(
        self,
        cmd: List[str],
        repo_path: Path,
        *,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess[str]:
        from uta.language.java.maven_compat.launcher import prepare_command, reject_compatibility_failure

        cmd = self._with_maven_central_mirror(cmd, repo_path)
        cmd = _execution._with_jacoco_argline_bridge(cmd, repo_path)
        cmd = prepare_command(cmd, repo_path)
        completed = run_bounded_command(
            self.run_command,
            cmd,
            cwd=repo_path,
            env=self._command_env(),
            timeout=self.timeout_seconds if timeout is None else timeout,
            stall_seconds=self.stall_seconds if self.stall_detection_enabled else 0,
        )
        if completed.returncode != 0:
            try:
                reject_compatibility_failure((completed.stdout or "") + "\n" + (completed.stderr or ""))
            except PitCompatibilityError as exc:
                # A compatibility failure on a red build still has gate output
                # above it. Carry the run so the caller reports what the gates
                # said rather than echoing one Maven [ERROR] line as the verdict.
                exc.completed = completed
                raise
        if completed.returncode == 0:
            # Earlier green Maven summaries cannot override missing completion.
            try:
                validate_completion(cmd)
            except OSError as exc:
                error = exc if isinstance(exc, PitCompatibilityError) else PitCompatibilityError(str(exc))
                # The gates already spoke in this run's output. Carry it so the
                # caller reports what they said, not only that PIT bookkeeping
                # came up short.
                error.completed = completed
                raise error from exc
        return completed

    def _with_maven_central_mirror(self, cmd: Sequence[str], repo_path: Path) -> List[str]:
        return _execution._with_maven_central_mirror(
            cmd, repo_path, self.maven_central_mirror_url
        )

    def _command_env(self) -> Optional[Dict[str, str]]:
        return _execution._command_env(self.java_home)

    def _changed_java_files(self, repo_path: Path) -> Optional[List[str]]:
        return _planning._changed_java_files(repo_path, self.base_ref)

    def _scoped_changed_production_java(
        self,
        cmd: Sequence[str],
        changed_production_java: Sequence[str],
    ) -> List[str]:
        """The changed sources this run is about.

        A caller that pinned `-Dtest.enforcement.targetSource(s)=` -- repair,
        working one batch at a time -- gets exactly the sources it named. This
        used to cost a Maven invocation: a filter-diff preflight ran so the
        plugin could echo the value back, which UTA had written itself a moment
        earlier. Everyone else gets the whole changed set, and the plugin
        decides what carries an obligation during the real run.
        """
        explicit = _planning._explicit_target_sources(cmd)
        if explicit:
            scoped = _planning._changed_files_matching(changed_production_java, explicit)
            if scoped:
                return scoped
        return list(changed_production_java)

    def _with_ci_gate_properties(self, cmd: List[str]) -> List[str]:
        return _planning._with_ci_gate_properties(
            cmd, coverage_gate=self.coverage_gate, mutation_gate=self.mutation_gate
        )

    def _coverage_gate_ratio_value(self) -> str:
        return _planning._coverage_gate_ratio_value(self.coverage_gate)

    def _mutation_score_below_ci_gate(self, output: str) -> bool:
        return _mutation_score_below_ci_gate(output, self.mutation_gate)

    def _diff_evidence(
        self,
        changed_java: Optional[List[str]],
        changed_production_java: Optional[List[str]],
        *,
        target_tests: Optional[Sequence[str]] = None,
        changed_modules: Optional[Sequence[str]] = None,
        target_sources: Optional[Sequence[str]] = None,
        filtered_changed_production_java: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        return _evidence._diff_evidence(
            changed_java,
            changed_production_java,
            target_tests=target_tests,
            changed_modules=changed_modules,
            target_sources=target_sources,
            filtered_changed_production_java=filtered_changed_production_java,
            base_ref=self.base_ref,
        )

    def _with_test_enforcement_tooling_evidence(
        self,
        repo_path: Path,
        cmd: Sequence[str],
        evidence: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return _evidence._with_test_enforcement_tooling_evidence(
            repo_path, cmd, evidence, run_maven_command=self._run_command
        )

    def _classify_completed(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo_path: Path,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> QualityGateResult:
        return _classify_completed(
            cmd,
            completed,
            repo_path,
            evidence,
            mutation_gate=self.mutation_gate,
            detect_termination=self.stall_detection_enabled or self.hanging_test_quarantine_enabled,
            tooling_evidence=self._with_test_enforcement_tooling_evidence,
        )

    # -- configuration-free layers, kept reachable on the class --------------

    _changed_production_java_files = staticmethod(_planning._changed_production_java_files)
    _gate_percent_value = staticmethod(_planning._gate_percent_value)
    _java_code_without_comments_or_strings = staticmethod(_planning._java_code_without_comments_or_strings)
    _java_fqn_from_path = staticmethod(_planning._java_fqn_from_path)
    _java_test_paths_for_fqn = staticmethod(_planning._java_test_paths_for_fqn)
    _java_test_targets_type = staticmethod(_planning._java_test_targets_type)
    _maven_module_for_path = staticmethod(_planning._maven_module_for_path)
    _maven_modules_for_changed_files = staticmethod(_planning._maven_modules_for_changed_files)
    _pitest_target_tests = staticmethod(_planning._pitest_target_tests)
    _replace_maven_goals = staticmethod(_planning._replace_maven_goals)
    _surefire_test_selector = staticmethod(_planning._surefire_test_selector)
    _validate_command = staticmethod(_planning._validate_command)
    _with_changed_modules = staticmethod(_planning._with_changed_modules)
    _with_target_sources = staticmethod(_planning._with_target_sources)
    _with_target_tests = staticmethod(_planning._with_target_tests)
    _with_selected_test_classes = staticmethod(_planning._with_selected_test_classes)
    _with_selected_test_file = staticmethod(_planning._with_selected_test_file)
    _without_maven_project_selector = staticmethod(_planning._without_maven_project_selector)
    _without_maven_property = staticmethod(_planning._without_maven_property)
    _without_maven_reactor_dependency_selectors = staticmethod(_planning._without_maven_reactor_dependency_selectors)

    _diff_mutation_scores = staticmethod(_parsing._diff_mutation_scores)
    _failed_surefire_tests = staticmethod(_parsing._failed_surefire_tests)
    _failed_surefire_test_classes = staticmethod(_parsing._failed_surefire_test_classes)
    _filtered_classes_from_pitest_patterns = staticmethod(_parsing._filtered_classes_from_pitest_patterns)
    _has_required_evidence = staticmethod(_parsing._has_required_evidence)
    _has_scoped_pitest_targets = staticmethod(_parsing._has_scoped_pitest_targets)
    _looks_build_broken = staticmethod(_parsing._looks_build_broken)
    _looks_gate_failed = staticmethod(_parsing._looks_gate_failed)
    _looks_no_enforceable_java_lines = staticmethod(_parsing._looks_no_enforceable_java_lines)
    _looks_pitest_baseline_failure = staticmethod(_parsing._looks_pitest_baseline_failure)
    _looks_skipped = staticmethod(_parsing._looks_skipped)
    _parse_surefire_text_failure = staticmethod(_parsing._parse_surefire_text_failure)
    _pitest_target_patterns = staticmethod(_parsing._pitest_target_patterns)

    _java_classes_from_production_files = staticmethod(_evidence._java_classes_from_production_files)
    _with_filtered_target_evidence = staticmethod(_evidence._with_filtered_target_evidence)
    _with_selected_test_quality_evidence = staticmethod(_evidence._with_selected_test_quality_evidence)
    _with_surefire_failure_evidence = staticmethod(_evidence._with_surefire_failure_evidence)


__all__ = [
    "JAVA_CI_COVERAGE_GATE_PROPERTY",
    "JAVA_CI_MUTATION_GATE_PROPERTY",
    "MISSING_EVIDENCE_SUMMARY",
    "MavenEnforcementRunner",
]


def _obligated_changed_production_files(
    evidence: Mapping[str, Any],
    changed_production_java: Optional[Sequence[str]],
) -> Optional[List[str]]:
    """The changed sources the plugin says carry a test obligation.

    An empty filtered list is an answer, not a gap: the plugin exempted every
    changed source, so no failing test is this diff's responsibility. Only a
    missing key falls back to the unfiltered list, which is the pre-plugin
    evidence shape and the local lane.
    """
    filtered = evidence.get("filteredChangedProductionFiles")
    if isinstance(filtered, (list, tuple)):
        return [str(path) for path in filtered]
    return list(changed_production_java) if changed_production_java is not None else None


def _pit_compatibility_update(exc: OSError, result: QualityGateResult) -> Dict[str, Any]:
    """Fold an incomplete-completion failure into an already-classified verdict."""
    evidence = {**(result.evidence or {}), "failureKind": "pit_compatibility_failed"}
    modules = list(getattr(exc, "modules", []) or [])
    if modules:
        evidence["pitCompatibilityModules"] = modules
    # The evidence tree lives in the workspace and is routinely gone before
    # anyone reads the record, so what it held is captured here, not re-derived.
    diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
    if diagnostics:
        evidence["pitCompatibilityDiagnostics"] = diagnostics
    if not result.passed:
        # A real gate miss is the honest headline; the incomplete completion
        # evidence is a note on it, and the targets stay repairable.
        return {
            "summary": "%s (PIT completion evidence incomplete: %s)" % (result.summary, exc),
            "evidence": evidence,
        }
    return {
        "passed": False,
        "status": QualityGateStatus.missing_evidence,
        "summary": (
            "UTA test-enforcement gates passed, but the run cannot be trusted: %s" % exc
        ),
        "evidence": {**evidence, "repairEligible": False},
    }
