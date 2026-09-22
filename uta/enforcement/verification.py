from __future__ import annotations

from typing import Protocol


class VerificationResult(Protocol):
    """Minimum result shape returned by language verification runners."""

    status: str
    reason_code: str

    def as_result_fields(self) -> dict:
        ...


def verification_task_status(status: str, reason_code: str) -> str:
    """Map a verification (status, reason_code) onto a CLASS_TASK_STATUS.

    Single source of truth shared by every language's verification runner so the
    reason -> task-status mapping cannot drift. Only the mutation gate gets its
    own terminal status; every other failure (test, coverage, missing evidence)
    is a plain ``FAIL`` (matching both production paths and the valid status set
    in ``uta.tasks.models.CLASS_TASK_STATUSES`` — there is no ``COVERAGE_FAIL``).
    """
    if status == "passed":
        return "PASS"
    if reason_code in {"mutation_gate_failed", "mutation_command_failed", "missing_mutation_report"}:
        return "MUTATION_FAIL"
    return "FAIL"


__all__ = [
    "VerificationResult",
    "verification_task_status",
]
