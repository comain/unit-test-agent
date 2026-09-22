"""Re-export of the shared mutation partitioning algebra.

The partition logic is pure and both enforcement lanes need it, so it lives in
`uta_enforce_core` -- the package the standalone binding is distributed with --
rather than inside the product. This module keeps the import path the legacy
lane and its tests already use pointing at that one implementation, so there is
never a second copy to drift.
"""

from __future__ import annotations

from uta_enforce_core.mutation_batching import (  # noqa: F401
    CLASS_NAME_SEPARATOR,
    BatchPlan,
    MutationUnit,
    PartitionResult,
    aggregate_batch_candidate_plans,
    estimated_generated_bytes,
    mutation_units_from_source,
    partition_units_into_batches,
)

__all__ = [
    "CLASS_NAME_SEPARATOR",
    "BatchPlan",
    "MutationUnit",
    "PartitionResult",
    "aggregate_batch_candidate_plans",
    "estimated_generated_bytes",
    "mutation_units_from_source",
    "partition_units_into_batches",
]
