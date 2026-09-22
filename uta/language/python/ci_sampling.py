"""The CI cap profile, expressed as an enforcement sampling policy.

Legacy applies CI mutation sampling inside its own generation-policy builder,
reading four caps straight off UTA settings. The binding cannot do that -- it
must not import UTA -- so the caps travel as a `MutationSelectionPolicy`, the
seam the contract already defines and `UtaPythonEnforcementProxy` already
accepts.

The point of the profile is cost, not correctness: a CI run bounds how much
mutation work a large diff can ask for. Skipping it does not produce a wrong
verdict, it produces an unbounded bill, which is why the canonical lane
ignoring the flag went unnoticed for so long.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Sequence

from uta.shared.config import settings


@dataclass(frozen=True)
class CiCapSamplingPolicy:
    """Keep the highest-value opportunities, up to the CI caps.

    Ranking mirrors `uta_py_enforce.mutation_policy._hard_cap`: operator
    priority first, then a digest of the opportunity id, then selection rank.
    The digest is what keeps the choice stable across runs without making it
    alphabetical -- two runs over the same diff sample the same mutants, so a
    CI failure reproduces.
    """

    max_changed_lines: int
    max_opportunities: int
    max_selected: int
    # Per-target shares of the report-wide `max_selected` budget, keyed by
    # source path. `select_mutants` runs once per target, so without this the
    # cap applied N times over for an N-target report. Empty means no
    # allocation was computed and the raw caps apply, which is the right
    # behaviour for a single-target call.
    allocations: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(
        cls, *, allocations: Mapping[str, int] | None = None
    ) -> "CiCapSamplingPolicy":
        return cls(
            max_changed_lines=int(
                getattr(settings, "python_mutation_generation_ci_max_changed_lines", 0) or 0
            ),
            max_opportunities=int(
                getattr(settings, "python_mutation_generation_ci_max_opportunities", 0) or 0
            ),
            max_selected=int(
                getattr(settings, "python_mutation_generation_ci_max_selected", 0) or 0
            ),
            allocations=dict(allocations or {}),
        )

    def select_mutants(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        target_context: Mapping[str, Any] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        items = [item for item in candidates if isinstance(item, Mapping)]
        if not items:
            return list(items)

        # A negative limit means "no cap configured"; zero means "the report
        # budget is spent, run nothing here". Collapsing the two would turn an
        # exhausted budget back into an unbounded run.
        limit = self._effective_limit(items, target_context=target_context)
        if limit < 0 or len(items) <= limit:
            return list(items)

        ranked = sorted(items, key=self._rank)
        kept = ranked[:limit]
        # Restore the original order: the policy file's line lists are read as
        # a plan, and reordering them would make two identical runs look like
        # different plans.
        keep_ids = {id(item) for item in kept}
        return [item for item in items if id(item) in keep_ids]

    def _effective_limit(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        target_context: Mapping[str, Any] | None = None,
    ) -> int:
        source_path = str((target_context or {}).get("sourcePath") or "")
        allocated = self.allocations.get(source_path) if self.allocations else None
        max_selected = int(allocated) if allocated is not None else self.max_selected
        limits = [
            value
            for value in (max_selected, self.max_opportunities)
            if value > 0
        ]
        # An allocation of zero means the report budget was spent on other
        # targets. That is a real answer -- run nothing here -- not an absent
        # limit, so it must not fall through to the unbounded branch below.
        if allocated is not None and int(allocated) <= 0:
            return 0
        distinct_lines = {int(item.get("line") or 0) for item in items}
        if self.max_changed_lines > 0 and len(distinct_lines) > self.max_changed_lines:
            limits.append(self.max_changed_lines)
        return min(limits) if limits else -1

    @staticmethod
    def _rank(item: Mapping[str, Any]) -> tuple:
        return (
            -int(item.get("operatorPriority") or 0),
            hashlib.sha256(
                str(item.get("opportunityId") or "").encode("utf-8")
            ).hexdigest(),
            tuple(item.get("selectionRank") or ()),
        )


__all__ = ["CiCapSamplingPolicy"]
