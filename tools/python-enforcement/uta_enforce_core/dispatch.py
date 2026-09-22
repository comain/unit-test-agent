"""Stateless enforcement dispatch entry point."""

from __future__ import annotations

from .contracts import (
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementResult,
)
from .registry import EnforcementRegistry
from .validation import EnforcementValidationError, validate_request, validate_result


class UnsupportedCapabilityError(EnforcementValidationError):
    """Raised when an enforcement request requires capabilities not supported by the binding."""


def enforce(
    request: EnforcementRequest,
    *,
    registry: EnforcementRegistry,
    context: EnforcementInvocationContext,
) -> EnforcementResult:
    """Stateless enforcement dispatch.

    Validates request parameters, resolves language binding from the immutable registry,
    verifies capability conformance, invokes the binding exactly once, and validates
    the resulting evidence and status.
    """
    if not isinstance(registry, EnforcementRegistry):
        raise TypeError(f"registry must be an EnforcementRegistry, got {type(registry).__name__}")
    if not isinstance(context, EnforcementInvocationContext):
        raise TypeError(f"context must be an EnforcementInvocationContext, got {type(context).__name__}")

    # Step 1: Validate request fail-closed
    validate_request(request)

    # Step 2: Lookup binding in registry
    binding = registry.get(request.language)

    # Step 3: Check capabilities
    capabilities = binding.capabilities() if hasattr(binding, "capabilities") else None
    if capabilities is not None:
        if context.sampling_policy is not None and not getattr(capabilities, "accepts_sampling_policy", False):
            raise UnsupportedCapabilityError(
                f"Language binding '{binding.language}' does not accept an injected mutation sampling policy"
            )
        if request.quality_gates and request.quality_gates.diff_mutation_min is not None:
            if not getattr(capabilities, "supports_mutation", True):
                raise UnsupportedCapabilityError(
                    f"Language binding '{binding.language}' does not support mutation quality gates"
                )
        if request.quality_gates and request.quality_gates.diff_coverage_min is not None:
            if not getattr(capabilities, "supports_coverage", True):
                raise UnsupportedCapabilityError(
                    f"Language binding '{binding.language}' does not support coverage quality gates"
                )

    # Step 4: Invoke binding exactly once
    result = binding.enforce(request, context)

    # Step 5: Validate returned result
    validate_result(result)

    return result
