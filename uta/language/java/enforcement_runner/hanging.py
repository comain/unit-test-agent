"""Per-invocation stall state, using the failing-test exclusion machinery."""
from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.language.java.enforcement_runner import parsing, planning
from uta.language.java.enforcement_runner.failing_test_scope import (
    _expand_colliding_simple_name_exclusions,
    _without_module_emptying_exclusions,
    partition_failing_tests,
)
from uta.language.java.enforcement_runner.quarantine import repository_key

STALL_MARKER = "UTA_ENFORCEMENT_STALLED stalled-after="


def stalled(completed) -> bool:
    return STALL_MARKER in (completed.stderr or "")


class HangingRun:
    def __init__(self, runner, repo, changed, production):
        self.runner = runner
        self.repo = repo
        self.changed = changed
        self.production = production
        self.named = []
        self.quarantined = []
        self.retained = []
        self.pre = []
        self.retries = 0
        self.reason = ""
        self.key = repository_key(repo) if runner.quarantine_store else ""

    @property
    def enabled(self):
        return (
            self.runner.stall_detection_enabled
            or self.runner.hanging_test_quarantine_enabled
        )

    def evidence(self, original):
        if not self.enabled:
            return original
        return {
            **original,
            "hangingTestClasses": self.named[:],
            "quarantinedTestClasses": self.quarantined[:],
            "retainedHangingTestClasses": self.retained[:],
            "preQuarantinedTestClasses": self.pre[:],
            "stallSeconds": self.runner.stall_seconds,
            "stallRetries": self.retries,
            "stallReason": self.reason,
        }

    def partition(self, names):
        # A stall can happen before filter-diff prints its obligation list.
        # Always protect every changed production type in this path.
        return partition_failing_tests(
            names,
            repo_path=self.repo,
            changed_java=self.changed,
            changed_production_java=self.production,
            generated_test_classes=self.runner.generated_test_classes,
        )

    def command(self, cmd, names, output=""):
        partition = self.partition(names)
        directories = planning._module_dirs_by_display_name(self.repo)
        inventory = planning._declared_test_classes_by_module(
            self.repo,
            list(directories),
            include_tests_suffix=parsing._surefire_defaults_include_tests_suffix(output),
        )
        if inventory is None:
            return cmd, (), "incomplete-test-inventory"
        # Include non-default tests already observed by Surefire as well.
        for module, classes in parsing._surefire_test_classes_by_module(self.repo).items():
            inventory.setdefault(module, set()).update(classes)
        for name in names:
            paths = planning._java_test_paths_for_fqn(self.repo, name)
            if not paths:
                return cmd, (), "test-source-unavailable"
            for path in paths:
                module = _test_module(path)
                inventory.setdefault(module, set()).add(name)
        for narrow in (
            _expand_colliding_simple_name_exclusions,
            _without_module_emptying_exclusions,
        ):
            partition = narrow(partition, inventory)
        if not partition.all_excluded:
            return cmd, (), partition.reason
        selected, reason = self.runner._excluding_test_classes(
            self.repo, cmd, partition.all_excluded, output, inventory,
        )
        return selected, partition.all_excluded if selected != cmd else (), reason

    def preexclude(self, cmd):
        store = self.runner.quarantine_store
        if not self.runner.hanging_test_quarantine_enabled or store is None:
            return cmd
        names = store.active(self.key, self.runner.quarantine_ttl_days)
        if not names:
            return cmd
        selected, excluded, _ = self.command(cmd, names)
        self.pre = list(excluded)
        return selected

    def detect(self, completed):
        name = parsing.unfinished_surefire_class(completed.stdout or "")
        if name is None:
            self.reason = "no-unfinished-test"
            return None
        repeated = name in self.named or name in self.pre
        if name not in self.named:
            self.named.append(name)
        if name in self.partition([name]).retained:
            self.retained.append(name)
            self.reason = "related-to-change"
        elif repeated:
            self.reason = "repeated-hanging-class"
        elif self.retries >= self.runner.stall_retries:
            self.reason = "stall-retries-exhausted"
        else:
            self.reason = ""
        return name

    def observe(self, name):
        if not self.runner.hanging_test_quarantine_enabled or not self.runner.quarantine_store:
            return
        paths = planning._java_test_paths_for_fqn(self.repo, name)
        if paths:
            self.runner.quarantine_store.observe(self.key, name, _test_module(paths[0]))

    def failure(self, cmd, completed, evidence):
        seconds = self.runner.stall_seconds
        if self.reason == "related-to-change":
            summary = (
                f"UTA test-enforcement stopped because test class {self.retained[-1]} "
                f"did not terminate within {seconds}s; it is related to this change, "
                "so it was not excluded"
            )
        elif not self.named:
            summary = f"UTA test-enforcement stopped after {seconds}s without output"
        else:
            summary = (
                f"UTA test-enforcement stopped: {self.reason}; hanging test classes: "
                + ", ".join(self.named)
            )
        if self.quarantined or self.pre:
            summary += "; quarantined: " + ", ".join(self.pre + self.quarantined)
        details = {**self.evidence(evidence), "repairEligible": False}
        if completed.returncode < 0 or completed.returncode in (137, 143):
            details["terminationSignal"] = (
                -completed.returncode
                if completed.returncode < 0
                else completed.returncode - 128
            )
        return QualityGateResult(
            status=QualityGateStatus.command_error,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            summary=summary,
            evidence=details,
        )

    def annotate(self, result):
        if not self.enabled:
            return result
        result.evidence = self.evidence(result.evidence or {})
        if self.quarantined:
            result.summary += (
                f'; test class {", ".join(self.quarantined)} was excluded because it '
                f"did not terminate within {self.runner.stall_seconds}s; lines only it "
                "reached no longer count as covered"
            )
        if self.pre:
            result.summary += "; previously hanging test classes pre-excluded: " + ", ".join(
                self.pre
            )
        return result


def _test_module(test_path: str) -> str:
    return str(test_path).split("src/test/java/", 1)[0].rstrip("/")
