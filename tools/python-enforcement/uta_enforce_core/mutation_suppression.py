from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


LOW_VALUE_LOGGING = "low_value_logging"
METRICS_ONLY = "metrics_only"
IMPORT_WIRING = "import_wiring"
PURE_CONFIG_CONSTANT = "pure_config_constant"
GENERATED_FRAMEWORK_GLUE = "generated_framework_glue"
MUTATION_TOOL_UNSUPPORTED = "mutation_tool_unsupported"
ASYNC_SCHEDULER_LOOP = "async_scheduler_loop"


@dataclass(frozen=True)
class SuppressionReason:
    """Language-neutral reason for excluding a low-value mutation opportunity."""

    code: str
    description: str


SUPPRESSION_REASONS: Tuple[SuppressionReason, ...] = (
    SuppressionReason(LOW_VALUE_LOGGING, "Logging-only side effect with no business decision."),
    SuppressionReason(METRICS_ONLY, "Metrics-only side effect already covered by behavior gates."),
    SuppressionReason(IMPORT_WIRING, "Import or compatibility wiring without target behavior."),
    SuppressionReason(PURE_CONFIG_CONSTANT, "Configuration constant with no executable branch behavior."),
    SuppressionReason(GENERATED_FRAMEWORK_GLUE, "Generated or framework glue outside unit-test intent."),
    SuppressionReason(MUTATION_TOOL_UNSUPPORTED, "Construct that the active mutation tool cannot stably address."),
    SuppressionReason(ASYNC_SCHEDULER_LOOP, "Async scheduler loop control that commonly turns mutation tests into timeouts."),
)


def suppression_reason_codes() -> Tuple[str, ...]:
    """Return stable suppression reason codes owned by the engine layer."""

    return tuple(reason.code for reason in SUPPRESSION_REASONS)


def is_known_suppression_reason(code: str) -> bool:
    """Return whether ``code`` is part of the shared suppression taxonomy."""

    return str(code or "") in suppression_reason_codes()
