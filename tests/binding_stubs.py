"""Enforcement bindings that report a scripted result.

There is one Python enforcement lane: the request goes to a binding, the
binding reports what it ran, and `uta.language.python.enforcement` projects
that into the envelope. So there is one seam worth stubbing --
`enforcement_binding` -- and these are the stubs for it.

They mirror `uta_py_enforce` key for key rather than paraphrasing it. The
projection and the aggregates read those names, so a stub that invents its own
tests the stub. Two bugs of exactly that kind were found and fixed here while
these were being written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Sequence

import pytest


class PassingBinding:
    """The same outcome, in the shape the canonical lane reads.

    Mirrors what `uta_py_enforce` reports: per-target coverage, mutation and
    the commands it issued, and nothing about the repository -- the envelope
    facts are the product's, which is exactly the split under test.
    """

    language = "python"

    def __init__(self, changed_lines: Dict[str, Sequence[int]] | None = None):
        self.requests: list[Any] = []
        #: The policy each invocation was given, `None` when sampling is off.
        #: This is where CI cost control is now observable: the caps travel as
        #: a policy on the context rather than as a flag down a runner call.
        self.sampling_policies: list[Any] = []
        self._changed_lines = dict(changed_lines or {})

    def capabilities(self):  # pragma: no cover - not read on this path
        raise NotImplementedError

    def enforce(self, request, context):
        from uta_enforce_core.contracts import (
            EnforcementResult,
            EnforcementStatus,
            TargetEnforcementResult,
        )

        self.requests.append(request)
        self.sampling_policies.append(context.sampling_policy)
        targets = []
        results = []
        for target in request.targets:
            changed = list(self._changed_lines.get(target.source_path, [1]))
            payload = {
                "status": "passed",
                "reasonCode": "passed",
                "message": "",
                "testsPass": True,
                "coverage": {
                    "covered": len(changed), "total": len(changed), "rate": 100.0,
                    "gate": request.quality_gates.diff_coverage_min * 100.0,
                    "passed": True, "scope": "changed_lines",
                    # snake_case, exactly as `uta_py_enforce.coverage`
                    # emits it. A stub that renames a key the projection
                    # reads tests the stub, not the lane.
                    "changed_lines": {target.source_path: changed},
                },
                "mutation": {
                    "runtime_lane": "lightweight",
                    "generated": 1, "killed": 1, "survived": 0,
                    "changedLineMutantsGenerated": 1,
                    "changedLineMutantsKilled": 1,
                    "changedLineMutantsScored": 1,
                    "rate": 100.0,
                    "gate": request.quality_gates.diff_mutation_min * 100.0,
                    "passed": True, "scope": "changed_lines",
                    "changed_lines": {target.source_path: changed},
                },
                "selectedTest": (target.test_paths or ("",))[0],
                "commands": [],
                "artifacts": {},
            }
            targets.append(payload)
            results.append(
                TargetEnforcementResult(
                    target=target, status=EnforcementStatus.PASSED, evidence=payload
                )
            )
        return EnforcementResult(
            status=EnforcementStatus.PASSED,
            evidence={"status": "passed", "targets": targets, "commands": []},
            target_results=tuple(results),
        )


@pytest.fixture
def binding_stub():
    """A binding that passes every target it is given.

    The changed lines are supplied rather than discovered: an
    `EnforcementRequest` does not carry them -- the real binding computes them
    from the repo -- so a stub has to be told what the fixture changed.
    `_init_repo` changes line 2 of jobs/forecast.py.
    """
    return {
        "enforcement_binding": PassingBinding(changed_lines={"jobs/forecast.py": [2]})
    }


class ExplodingBinding:
    """A binding that must never be asked to enforce anything."""

    language = "python"

    def __init__(self, message: str):
        self._message = message

    def capabilities(self):  # pragma: no cover - not read on this path
        raise NotImplementedError

    def enforce(self, request, context):
        raise AssertionError(self._message)


@pytest.fixture
def binding_refuses_to_verify():
    """A binding that fails loudly if it is asked to enforce anything.

    For pre-flight properties -- a comment-only diff, no changed targets --
    where the finding is that nothing ran. Enforcement that quietly ran pytest
    here would still produce a passing envelope, so asserting the envelope
    alone cannot catch it.
    """
    return {"enforcement_binding": ExplodingBinding("verification must not be invoked")}


@dataclass(frozen=True)
class TargetOutcome:
    """What a lane should report for one target, said once for both lanes."""

    passed: bool
    covered: int = 1
    total: int = 1
    changed_line: int = 2
    reason_code: str = ""
    message: str = ""
    #: Per-target mutation outcome. Defaults keep the historical stub result
    #: (one generated mutant, none killed) so existing callers are unchanged.
    mutants_generated: int = 1
    mutants_killed: int = 0

    def resolved_reason(self) -> str:
        if self.reason_code:
            return self.reason_code
        return "passed" if self.passed else "coverage_gate_failed"

    def resolved_message(self) -> str:
        if self.message:
            return self.message
        return "passed" if self.passed else "coverage failed"


def _coverage_payload(source_path: str, outcome: TargetOutcome, gate: float) -> Dict[str, Any]:
    """Mirrors `uta_py_enforce.coverage.coverage_summary` key for key.

    Deliberately not a paraphrase: the projection and the aggregates read these
    names, so a stub that invents its own tests the stub.
    """
    rate = 0.0 if outcome.total == 0 else round((outcome.covered / outcome.total) * 100.0, 4)
    return {
        "covered": outcome.covered,
        "total": outcome.total,
        "rate": rate,
        "gate": gate,
        # Derived, not copied from `outcome.passed`: the real binding decides
        # coverage on rate vs gate, and a stub that conflates the two cannot
        # express "coverage passed, mutation did not" -- the case the CI
        # sampling gate turns on.
        "passed": rate >= gate,
        "testsPass": True,
        "xml_path": ".uta_cache/python/coverage/coverage.xml",
        "scope": "changed_lines",
        "changed_lines": {source_path: [outcome.changed_line]},
        "no_executable_changed_lines": False,
    }


def _mutation_payload(source_path: str, outcome: TargetOutcome, gate: float) -> Dict[str, Any]:
    generated = int(outcome.mutants_generated)
    killed = int(outcome.mutants_killed)
    rate = 0.0 if generated == 0 else round((killed / generated) * 100.0, 4)
    return {
        "runtime_lane": "mutmut-modern",
        "generated": generated,
        "killed": killed,
        "survived": generated - killed,
        "noCoverage": 0,
        "rate": rate,
        "gate": gate,
        "passed": rate >= gate,
        "scope": "changed_lines",
        "changed_lines": {source_path: [outcome.changed_line]},
        "changedLineMutantsGenerated": generated,
        "changedLineMutantsKilled": killed,
        "changedLineMutantsScored": generated,
    }


class OutcomeBinding:
    """A binding that reports a caller-supplied outcome per target."""

    language = "python"

    def __init__(self, outcomes: Dict[str, TargetOutcome], calls: list):
        self._outcomes = outcomes
        self._calls = calls

    def capabilities(self):  # pragma: no cover - not read on this path
        raise NotImplementedError

    def enforce(self, request, context):
        from uta_enforce_core.contracts import (
            EnforcementResult,
            EnforcementStatus,
            TargetEnforcementResult,
        )

        cov_gate = request.quality_gates.diff_coverage_min * 100.0
        mut_gate = request.quality_gates.diff_mutation_min * 100.0
        payloads = []
        results = []
        for target in request.targets:
            self._calls.append(target.source_path)
            outcome = self._outcomes[target.source_path]
            payload = {
                "status": "passed" if outcome.passed else "failed",
                "reasonCode": outcome.resolved_reason(),
                "message": outcome.resolved_message(),
                "testsPass": True,
                "coverage": _coverage_payload(target.source_path, outcome, cov_gate),
                "mutation": _mutation_payload(target.source_path, outcome, mut_gate),
                "testsPass": True,
                "selectedTest": (target.test_paths or ("",))[0],
                "commands": [],
                "artifacts": {},
            }
            payloads.append(payload)
            results.append(
                TargetEnforcementResult(
                    target=target,
                    status=(
                        EnforcementStatus.PASSED if outcome.passed
                        else EnforcementStatus.FAILED
                    ),
                    evidence=payload,
                )
            )
        all_passed = all(item.passed for item in self._outcomes.values())
        return EnforcementResult(
            status=EnforcementStatus.PASSED if all_passed else EnforcementStatus.FAILED,
            evidence={
                "status": "passed" if all_passed else "failed",
                "targets": payloads,
                "commands": [],
            },
            target_results=tuple(results),
        )


def stub_for(outcomes: Dict[str, TargetOutcome]) -> tuple:
    """Return `(kwargs, calls)` for a binding driven by one outcome map.

    `calls` records the source paths enforcement actually asked to verify, in
    order -- the thing a "no short-circuit" assertion needs, and the one fact
    the envelope would not reveal, since an aggregate built from one module
    still looks plausible.
    """
    calls: list = []
    return {"enforcement_binding": OutcomeBinding(outcomes, calls)}, calls


class SelectingBinding:
    """Real test selection, faked verification.

    The binding does two things: it decides which test verifies a target, then
    it runs pytest and mutmut against it. Only the second is expensive, and
    only the second needs faking. A stub that skipped selection too would make
    every `selectedTest` assertion an assertion about the stub -- which is
    exactly what happened when these tests stubbed `verify_python_target` and
    the lane beneath them changed.

    So this calls the same `strict_test_candidates` production calls, records
    what it chose, and reports a pass for it.
    """

    language = "python"

    def __init__(self, passing: bool = True):
        self.selected: list[list[str]] = []
        self.requests: list[Any] = []
        self.sampling_policies: list[Any] = []
        self._passing = passing

    def capabilities(self):  # pragma: no cover - not read on this path
        raise NotImplementedError

    def enforce(self, request, context):
        from uta_py_enforce.test_selection import strict_test_candidates
        from uta_enforce_core.contracts import (
            EnforcementResult,
            EnforcementStatus,
            TargetEnforcementResult,
        )

        self.requests.append(request)
        self.sampling_policies.append(context.sampling_policy)
        payloads = []
        results = []
        all_passed = True
        for target in request.targets:
            candidates = [
                str(path)
                for path in strict_test_candidates(
                    request.repo_path,
                    target.source_path,
                    configured=list(target.test_paths or ()),
                )
            ]
            self.selected.append(candidates)
            if not candidates:
                all_passed = False
                payload = {
                    "status": "failed",
                    "reasonCode": "missing_test_file",
                    "message": f"No unit test file found targeting {target.source_path}",
                    "testsPass": False,
                    "coverage": _coverage_payload(
                        target.source_path,
                        TargetOutcome(passed=False, covered=0, total=1),
                        request.quality_gates.diff_coverage_min * 100.0,
                    ),
                    "mutation": _mutation_payload(
                        target.source_path,
                        TargetOutcome(passed=False),
                        request.quality_gates.diff_mutation_min * 100.0,
                    ),
                    "selectedTest": "",
                    "candidateTestPaths": [],
                    "commands": [],
                    "artifacts": {},
                }
            else:
                outcome = TargetOutcome(passed=self._passing)
                all_passed = all_passed and self._passing
                payload = {
                    "status": "passed" if self._passing else "failed",
                    "reasonCode": outcome.resolved_reason(),
                    "message": "" if self._passing else outcome.resolved_message(),
                    "testsPass": True,
                    "coverage": _coverage_payload(
                        target.source_path,
                        outcome,
                        request.quality_gates.diff_coverage_min * 100.0,
                    ),
                    "mutation": _mutation_payload(
                        target.source_path,
                        outcome,
                        request.quality_gates.diff_mutation_min * 100.0,
                    ),
                    "selectedTest": candidates[0],
                    "candidateTestPaths": candidates,
                    "commands": [],
                    "artifacts": {},
                }
            payloads.append(payload)
            results.append(
                TargetEnforcementResult(
                    target=target,
                    status=(
                        EnforcementStatus.PASSED
                        if payload["status"] == "passed"
                        else EnforcementStatus.FAILED
                    ),
                    evidence=payload,
                )
            )
        return EnforcementResult(
            status=EnforcementStatus.PASSED if all_passed else EnforcementStatus.FAILED,
            evidence={
                "status": "passed" if all_passed else "failed",
                "targets": payloads,
                "commands": [],
            },
            target_results=tuple(results),
        )


def patch_binding(monkeypatch, binding) -> None:
    """Install `binding` for callers that cannot pass `enforcement_binding`.

    The CLI builds the request itself, so a test driving it through
    `CliRunner` has no kwarg to reach. `_run_canonical_python_enforcement`
    imports the proxy inside the function, so patching it at the source module
    is what the running code will see.
    """
    monkeypatch.setattr(
        "uta.enforcement.bindings.python_proxy.UtaPythonEnforcementProxy",
        lambda *args, **kwargs: binding,
    )
