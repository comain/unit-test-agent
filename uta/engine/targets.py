"""Canonical target identity exports used by engine contracts.

The concrete target model still lives in task storage for compatibility with
the existing database layer, but engine modules import it through this facade
so language workflows share one target abstraction.
"""

from uta.tasks.targets import (
    DEFAULT_JAVA_GRANULARITY,
    DEFAULT_LANGUAGE,
    TargetIdentity,
    TargetRef,
    coerce_target,
    coerce_targets,
    display_target,
    event_payload_for_targets,
    legacy_class_fqn_for_storage,
    result_key,
    target_count,
    target_identity_from_row,
)

__all__ = [
    "DEFAULT_JAVA_GRANULARITY",
    "DEFAULT_LANGUAGE",
    "TargetIdentity",
    "TargetRef",
    "coerce_target",
    "coerce_targets",
    "display_target",
    "event_payload_for_targets",
    "legacy_class_fqn_for_storage",
    "result_key",
    "target_count",
    "target_identity_from_row",
]
