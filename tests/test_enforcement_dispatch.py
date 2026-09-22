"""Tests for stateless enforcement dispatch and capability validation."""

from __future__ import annotations

from pathlib import Path
import pytest

from uta_enforce_core.contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    QualityGates,
)
from uta_enforce_core.dispatch import UnsupportedCapabilityError, enforce
from uta_enforce_core.registry import EnforcementRegistry, UnknownLanguageError


class MockCommandRunner:
    def run(self, args, *, cwd, env=None, timeout_seconds=None, is_cancelled=None):
        raise NotImplementedError()


class MockBinding:
    def __init__(
        self,
        language: str = "python",
        supports_coverage: bool = True,
        supports_mutation: bool = True,
        accepts_sampling_policy: bool = False,
    ):
        self.language = language
        self._supports_cov = supports_coverage
        self._supports_mut = supports_mutation
        self._accepts_sampling = accepts_sampling_policy
        self.call_count = 0
        self.last_request = None
        self.last_context = None

    def capabilities(self) -> EnforcementCapabilities:
        return EnforcementCapabilities(
            language=self.language,
            supports_coverage=self._supports_cov,
            supports_mutation=self._supports_mut,
            accepts_sampling_policy=self._accepts_sampling,
        )

    def enforce(self, request: EnforcementRequest, context: EnforcementInvocationContext) -> EnforcementResult:
        self.call_count += 1
        self.last_request = request
        self.last_context = context
        return EnforcementResult(
            status=EnforcementStatus.PASSED,
            evidence={"schemaVersion": 1, "backend": "python_enforcer", "status": "passed"},
        )


class MockSamplingPolicy:
    def select_mutants(self, candidates, *, target_context=None):
        return candidates


def test_enforce_dispatch_invokes_binding_exactly_once(tmp_path: Path):
    binding = MockBinding("python")
    registry = EnforcementRegistry([binding])
    context = EnforcementInvocationContext(run_command=MockCommandRunner())

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(),
    )

    res = enforce(request, registry=registry, context=context)
    assert res.status == EnforcementStatus.PASSED
    assert binding.call_count == 1
    assert binding.last_request is request
    assert binding.last_context is context


def test_enforce_dispatch_unknown_language(tmp_path: Path):
    binding = MockBinding("python")
    registry = EnforcementRegistry([binding])
    context = EnforcementInvocationContext(run_command=MockCommandRunner())

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="ruby",
        targets=(),
    )

    with pytest.raises(UnknownLanguageError):
        enforce(request, registry=registry, context=context)


def test_enforce_dispatch_sampling_policy_capability_check(tmp_path: Path):
    binding = MockBinding("python", accepts_sampling_policy=False)
    registry = EnforcementRegistry([binding])
    context = EnforcementInvocationContext(
        run_command=MockCommandRunner(),
        sampling_policy=MockSamplingPolicy(),
    )

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(),
    )

    with pytest.raises(UnsupportedCapabilityError, match="does not accept an injected mutation sampling policy"):
        enforce(request, registry=registry, context=context)


def test_enforce_dispatch_mutation_gate_capability_check(tmp_path: Path):
    binding = MockBinding("python", supports_mutation=False)
    registry = EnforcementRegistry([binding])
    context = EnforcementInvocationContext(run_command=MockCommandRunner())

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(),
        quality_gates=QualityGates(diff_mutation_min=0.8),
    )

    with pytest.raises(UnsupportedCapabilityError, match="does not support mutation quality gates"):
        enforce(request, registry=registry, context=context)
