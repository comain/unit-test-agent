"""Which verification a repair invalidates, and how the attempt advances.

A repair turn edits the workspace on purpose. The verification phases that ran
before it are therefore stale, and re-running them is *new* work rather than a
replay of the old work.

The reconciler decides that by identity, and identity includes the phase's
logical attempt. Leave the attempt where it is and the re-run lands on the
previous COMPLETED operation, whose recorded workspace fingerprint no longer
matches -- because the repair just changed it -- and the reconciler correctly
calls that unsafe and stops the unit. Which is what happened on beta: two java
generation runs died immediately after `fix_coverage` with `fail_unsafe`, on a
workspace nothing had corrupted.

So the repair has to say what it invalidated.
"""

from __future__ import annotations

from typing import Dict, Mapping

#: Verification phases whose results a repair turn makes stale. Deliberately
#: the whole set rather than a per-repair subset: a phase the graph does not
#: revisit never consults its attempt, so an unused increment costs nothing,
#: while a missing one costs the unit. None of these carry a
#: `max_attempts_by_phase` limit, so advancing them cannot exhaust a budget.
INVALIDATED_BY_REPAIR = (
    "verify_compile",
    "verify_tests",
    "measure_coverage",
    "measure_mutation",
    # The delegated gate runs its own verification, and it is a verification
    # like any other: beta task 45 advanced `verify_tests` correctly and then
    # died on this one, which was not listed. `test_repair_invalidates_
    # verification` now derives the expected set from the cycle definition so
    # a new verification phase cannot be added without appearing here.
    "delegated_quality_gate_verify",
)


def advance_verification_attempts(attempts: Mapping[str, int]) -> Dict[str, int]:
    """Return `attempts` with every repair-invalidated phase moved on one."""
    advanced = dict(attempts)
    for phase in INVALIDATED_BY_REPAIR:
        advanced[phase] = int(advanced.get(phase, 0)) + 1
    return advanced


__all__ = ["INVALIDATED_BY_REPAIR", "advance_verification_attempts"]
