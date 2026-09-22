"""Language-neutral lightweight enforcement contracts, registry, and dispatch."""

from __future__ import annotations

from .commands import (
    CommandExecutionError,
    CommandRequest,
    CommandResult,
    CommandRunner,
    SafeProcessRunner,
)
from .contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementLanguageBinding,
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    EnforcementTarget,
    MutationSelectionPolicy,
    ProgressEvent,
    QualityGates,
    RuntimeSelection,
    TargetEnforcementResult,
)
from .dispatch import UnsupportedCapabilityError, enforce
from .evidence import BACKEND, EVIDENCE_PREFIX, SCHEMA_VERSION, finalize, format_evidence_markers
from .registry import (
    DuplicateBindingError,
    EnforcementRegistry,
    EnforcementRegistryError,
    UnknownLanguageError,
)
from .validation import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_TARGETS,
    MAX_TEST_PATHS_PER_TARGET,
    MAX_TOTAL_TIMEOUT_SECONDS,
    EnforcementValidationError,
    validate_request,
    validate_result,
)

__version__ = "lightweight-1.0.2"

__all__ = [
    "CommandRunner",
    "BACKEND",
    "CommandExecutionError",
    "CommandRequest",
    "CommandResult",
    "CommandRunner",
    "DuplicateBindingError",
    "EVIDENCE_PREFIX",
    "EnforcementCapabilities",
    "EnforcementInvocationContext",
    "EnforcementLanguageBinding",
    "EnforcementRegistry",
    "EnforcementRegistryError",
    "EnforcementRequest",
    "EnforcementResult",
    "EnforcementStatus",
    "EnforcementTarget",
    "EnforcementValidationError",
    "MAX_COMMAND_TIMEOUT_SECONDS",
    "MAX_TARGETS",
    "MAX_TEST_PATHS_PER_TARGET",
    "MAX_TOTAL_TIMEOUT_SECONDS",
    "MutationSelectionPolicy",
    "ProgressEvent",
    "QualityGates",
    "RuntimeSelection",
    "SCHEMA_VERSION",
    "SafeProcessRunner",
    "TargetEnforcementResult",
    "UnknownLanguageError",
    "UnsupportedCapabilityError",
    "annotations",
    "enforce",
    "finalize",
    "format_evidence_markers",
    "validate_request",
    "validate_result",
]
