"""Thin UTA proxy to the canonical distributed Python enforcement binding."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from uta_py_enforce.api import create_python_enforcement_binding
from uta_enforce_core.contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementLanguageBinding,
    EnforcementRequest,
    EnforcementResult,
    MutationSelectionPolicy,
)


class UtaPythonEnforcementProxy:
    """Thin UTA proxy delegating to canonical Python enforcement binding with injected policy."""

    language = "python"

    def __init__(
        self,
        delegate: EnforcementLanguageBinding | None = None,
        *,
        sampling_policy_factory: Optional[Callable[[], MutationSelectionPolicy]] = None,
    ) -> None:
        self._delegate = delegate if delegate is not None else create_python_enforcement_binding()
        self._sampling_policy_factory = sampling_policy_factory

    def capabilities(self) -> EnforcementCapabilities:
        base_caps = self._delegate.capabilities()
        if self._sampling_policy_factory is not None:
            return replace(base_caps, accepts_sampling_policy=True)
        return base_caps

    def enforce(
        self,
        request: EnforcementRequest,
        context: EnforcementInvocationContext,
    ) -> EnforcementResult:
        effective_context = context
        if context.sampling_policy is None and self._sampling_policy_factory is not None:
            sampling_policy = self._sampling_policy_factory()
            effective_context = replace(context, sampling_policy=sampling_policy)

        return self._delegate.enforce(request, effective_context)
