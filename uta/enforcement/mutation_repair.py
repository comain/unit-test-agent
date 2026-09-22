from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional, Sequence


@dataclass(frozen=True)
class MutationRepairGroup:
    """Language-neutral survivor group small enough for one focused repair turn.

    ``family``/``killability`` describe the dominant mutation kind in the group so
    the repair prompt can prioritize (e.g. boundary/conditional mutations are
    high-killability and worth targeting first). Both backends populate them: Java
    from the PIT mutator, Python from the mutmut diff classifier.

    The remaining fields are a superset that lets Java render its full guidance
    through this shared model without regressing: ``mutator`` is the PIT mutator
    name (Python has none — mutmut only emits diffs, so it stays empty);
    ``effort_score``/``effort_band``/``roi`` carry kill-per-effort ranking when a
    backend computes it (``roi is None`` means "not ranked"); ``deprioritized`` and
    ``likely_equivalent`` carry skip/target guidance. Defaults are inert, so a
    backend that sets none of them renders exactly the minimal family line.
    """

    symbol: str
    count: int
    lines: Sequence[int] = field(default_factory=tuple)
    survivors: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    representative_diffs: Sequence[Dict[str, str]] = field(default_factory=tuple)
    family: str = ""
    killability: str = ""
    mutator: str = ""
    effort_score: str = ""
    effort_band: str = ""
    roi: Optional[float] = None
    deprioritized: bool = False
    likely_equivalent: bool = False


@dataclass(frozen=True)
class MutationRepairContext:
    """Compact mutation context consumed by repair prompts instead of raw tool logs."""

    target_id: str
    source_path: str
    test_paths: Sequence[str]
    reproduce_command: str
    survivor_count: int
    groups: Sequence[MutationRepairGroup]
    artifact_path: str = ""
    group_artifact_paths: Dict[str, str] = field(default_factory=dict)
    language: str = ""
    failure_breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MutationRepairRoundState:
    """Explicit workflow state for one target's mutation-repair turn.

    The planner intentionally consumes state supplied by the workflow instead of
    counting historical events. Resumes and retries can duplicate stage events;
    the attempt index and latest verifier survivor counts are the stable inputs.
    """

    target_id: str
    attempt_index: int = 1
    previous_survivor_count: Optional[int] = None
    latest_survivor_count: Optional[int] = None
    previous_selected_group_ids: Sequence[str] = field(default_factory=tuple)
    edited_test_paths: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class MutationRepairPlanEvidence:
    """Debug evidence explaining why the planner chose this repair scope."""

    previous_survivor_count: Optional[int] = None
    latest_survivor_count: Optional[int] = None
    selected_group_ids: Sequence[str] = field(default_factory=tuple)
    previous_selected_group_ids: Sequence[str] = field(default_factory=tuple)
    edited_test_paths: Sequence[str] = field(default_factory=tuple)
    no_progress: bool = False


@dataclass(frozen=True)
class MutationRepairPlan:
    """Language-neutral repair plan consumed by language-specific prompts."""

    target_id: str
    language: str
    round_index: int
    round_kind: Literal["full_roi", "focused_roi"]
    selected_groups: Sequence[MutationRepairGroup]
    context_artifact_path: str
    group_artifact_paths: Sequence[str] = field(default_factory=list)
    prompt_flags: Dict[str, Any] = field(default_factory=dict)
    evidence: MutationRepairPlanEvidence = field(default_factory=MutationRepairPlanEvidence)


def should_split_mutation_repair(survivor_count: int, *, threshold: int = 50) -> bool:
    """Decide whether repair should be split by language-specific symbol groups."""

    return int(survivor_count or 0) > int(threshold)


def plan_mutation_repair(
    context: MutationRepairContext,
    state: MutationRepairRoundState,
    *,
    focused_group_count: int = 1,
) -> MutationRepairPlan:
    """Create a language-neutral mutation repair plan for one target attempt.

    Round 1 is always the primary full-ROI repair round: the LLM sees every
    survivor group in deterministic ROI order and chooses the most valuable
    multi-mutant edit. Later rounds are cleanup fallbacks over the latest
    verifier survivors, narrowed to the top focused groups.
    """

    attempt = max(1, int(state.attempt_index or 1))
    ordered = _ordered_mutation_groups(context.groups)
    full_round = attempt <= 1
    selected = ordered if full_round else ordered[: max(1, int(focused_group_count or 1))]
    selected_ids = tuple(group.symbol for group in selected)
    group_paths = [
        path
        for path in (context.group_artifact_paths.get(group.symbol, "") for group in selected)
        if path
    ]
    evidence = MutationRepairPlanEvidence(
        previous_survivor_count=state.previous_survivor_count,
        latest_survivor_count=state.latest_survivor_count,
        selected_group_ids=selected_ids,
        previous_selected_group_ids=tuple(state.previous_selected_group_ids),
        edited_test_paths=tuple(state.edited_test_paths),
        no_progress=_mutation_repair_no_progress(state),
    )
    round_kind: Literal["full_roi", "focused_roi"] = "full_roi" if full_round else "focused_roi"
    return MutationRepairPlan(
        target_id=context.target_id,
        language=context.language or _language_from_target_id(context.target_id),
        round_index=attempt,
        round_kind=round_kind,
        selected_groups=tuple(selected),
        context_artifact_path=context.artifact_path,
        group_artifact_paths=group_paths,
        prompt_flags={
            "mutation_repair_roi_guided_full": full_round,
            "mutation_repair_split": not full_round and bool(group_paths),
            "mutation_repair_round_kind": round_kind,
        },
        evidence=evidence,
    )


def plan_mutation_repair_round(
    context: MutationRepairContext,
    *,
    attempt_index: int,
    previous_survivor_count: Optional[int] = None,
    previous_selected_group_ids: Sequence[str] = (),
    edited_test_paths: Sequence[str] = (),
    focused_group_count: int = 1,
) -> MutationRepairPlan:
    """Plan one repair round from what the previous round left behind.

    Both languages measure survivors their own way -- PIT XML and mutmut -- but
    the bookkeeping around a round is the same question in both: what did the
    last round start from, which groups did it target, and did it move
    anything. Assembling `MutationRepairRoundState` here keeps that answer in
    one place; a phase that built it inline passed only the attempt index, and
    `no_progress` was then false forever.
    """
    return plan_mutation_repair(
        context,
        MutationRepairRoundState(
            target_id=context.target_id,
            attempt_index=max(1, int(attempt_index or 1)),
            previous_survivor_count=(
                int(previous_survivor_count)
                if isinstance(previous_survivor_count, (int, float))
                and not isinstance(previous_survivor_count, bool)
                else None
            ),
            latest_survivor_count=int(context.survivor_count),
            previous_selected_group_ids=tuple(
                str(symbol) for symbol in previous_selected_group_ids or ()
            ),
            edited_test_paths=tuple(str(path) for path in edited_test_paths or ()),
        ),
        focused_group_count=max(1, int(focused_group_count or 1)),
    )


def _ordered_mutation_groups(groups: Sequence[MutationRepairGroup]) -> List[MutationRepairGroup]:
    return sorted(
        list(groups or []),
        key=lambda group: (
            -(group.roi if group.roi is not None else -1.0),
            -int(group.count or 0),
            str(group.symbol or ""),
            int(group.lines[0]) if group.lines else 0,
        ),
    )


def _mutation_repair_no_progress(state: MutationRepairRoundState) -> bool:
    if not state.edited_test_paths:
        return False
    if state.previous_survivor_count is None or state.latest_survivor_count is None:
        return False
    return int(state.latest_survivor_count) >= int(state.previous_survivor_count)


def _language_from_target_id(target_id: str) -> str:
    value = str(target_id or "")
    if value.startswith(("pyfile:", "pysymbol:")):
        return "python"
    if value.startswith(("java:", "class:")):
        return "java"
    return "unknown"


def select_mutation_repair_group(
    groups: Sequence[MutationRepairGroup],
    attempt: int,
) -> Optional[MutationRepairGroup]:
    """Pick a stable group for this repair attempt."""

    if not groups:
        return None
    index = max(0, int(attempt or 1) - 1) % len(groups)
    return groups[index]


def select_mutation_repair_groups(
    groups: Sequence[MutationRepairGroup],
    attempt: int,
    *,
    per_round: int = 1,
) -> List[MutationRepairGroup]:
    """Pick a stable window of groups for one focused repair round."""

    ordered = list(groups or [])
    if not ordered:
        return []
    size = max(1, min(int(per_round or 1), len(ordered)))
    start = (max(1, int(attempt or 1)) - 1) * size
    return [ordered[(start + offset) % len(ordered)] for offset in range(size)]


def write_mutation_repair_context(
    artifact_dir: Path,
    context: MutationRepairContext,
    *,
    split_threshold: int = 50,
) -> MutationRepairContext:
    """Persist full and per-group mutation repair context artifacts."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    full_path = artifact_dir / "mutation-repair-context.md"
    full_path.write_text(format_mutation_repair_context(context), encoding="utf-8")
    json_path = artifact_dir / "mutation-repair-context.json"
    json_path.write_text(_context_json(context), encoding="utf-8")

    group_paths: Dict[str, str] = {}
    if should_split_mutation_repair(context.survivor_count, threshold=split_threshold):
        for group in context.groups:
            group_path = artifact_dir / f"mutation-repair-{_slug(group.symbol)}.md"
            group_path.write_text(
                format_mutation_repair_context(context, selected_group=group.symbol),
                encoding="utf-8",
            )
            group_paths[group.symbol] = group_path.as_posix()

    return MutationRepairContext(
        target_id=context.target_id,
        source_path=context.source_path,
        test_paths=tuple(context.test_paths),
        reproduce_command=context.reproduce_command,
        survivor_count=context.survivor_count,
        groups=tuple(context.groups),
        artifact_path=full_path.as_posix(),
        group_artifact_paths=group_paths,
        language=context.language,
        failure_breakdown=dict(context.failure_breakdown),
    )


def format_mutation_repair_context(
    context: MutationRepairContext,
    *,
    selected_group: Optional[str] = None,
) -> str:
    """Render deterministic markdown without mutmut/PIT progress spam."""

    failure_label = _format_failure_breakdown(context)
    lines: List[str] = [
        "# Mutation Repair Context",
        "",
        f"- Target: `{context.target_id}`",
        f"- Source: `{context.source_path}`",
        f"- Test path(s): `{', '.join(context.test_paths)}`",
        f"- Failed mutation candidates: {context.survivor_count}{failure_label}",
        "",
        "## Verification",
        "The orchestrator will run the authoritative mutation verifier after the active test file is edited.",
        "Do not run mutation commands from this context artifact.",
        "",
        "## Survivor Groups",
        "",
    ]
    if any(group.roi is not None for group in context.groups):
        lines.extend(["_Ranked by kill-per-effort ROI — tackle the top entry first._", ""])
    for group in context.groups:
        if selected_group and group.symbol != selected_group:
            continue
        line_list = ", ".join(str(line) for line in group.lines[:12]) or "unknown"
        lines.extend(
            [
                f"### `{group.symbol}`",
                f"- survivors: {group.count}",
                f"- lines: {line_list}",
            ]
        )
        if group.family:
            lines.append(_format_family_line(group))
        if group.survivors:
            lines.append("- representative survivors:")
            for survivor in group.survivors[:6]:
                mutant_id = str(survivor.get("id") or survivor.get("mutant") or "").strip()
                id_suffix = f" `{mutant_id}`" if mutant_id else ""
                description = str(survivor.get("description") or survivor.get("mutation_type") or "").strip()
                lines.append(f"  - line {survivor.get('line', '?')}:{id_suffix} {description}".rstrip())
        if group.representative_diffs:
            lines.append("- representative diffs:")
            for diff in group.representative_diffs:
                output = str(diff.get("output") or "").strip()
                if output:
                    lines.append("```diff")
                    lines.append(output)
                    lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_failure_breakdown(context: MutationRepairContext) -> str:
    breakdown = {key: int(value or 0) for key, value in dict(context.failure_breakdown or {}).items()}
    if not breakdown:
        return f" (survived={context.survivor_count})"
    parts = [
        f"{key}={value}"
        for key, value in sorted(breakdown.items())
        if value
    ]
    return f" ({', '.join(parts)})" if parts else ""


def _format_family_line(group: MutationRepairGroup) -> str:
    """Render a group's mutation-family line.

    With only ``family``/``killability`` set this is the minimal Python line; the
    extra clauses (mutator, ROI, priority note) appear only when a backend
    populates the superset fields, so Python's output is unchanged until Java is
    switched onto this renderer (J1 step 3).
    """

    parts = [f"- mutation family: `{group.family}`"]
    if group.mutator:
        parts.append(f" via `{group.mutator}`")
    if group.killability:
        parts.append(f", killability {group.killability}")
    if group.roi is not None:
        parts.append(
            f", effort {group.effort_score or '?'} ({group.effort_band or '?'}), roi {group.roi}"
        )
    if group.likely_equivalent:
        parts.append(", likely_equivalent — skip, not worth a test")
    elif group.deprioritized:
        parts.append(", deprioritize unless there is an easy seam")
    elif group.mutator or group.roi is not None:
        parts.append(", target early")
    return "".join(parts)


def _context_json(context: MutationRepairContext) -> str:
    payload = {
        "target_id": context.target_id,
        "language": context.language,
        "source_path": context.source_path,
        "test_paths": list(context.test_paths),
        "survivor_count": context.survivor_count,
        "failure_breakdown": dict(context.failure_breakdown),
        "groups": [
            {
                "symbol": group.symbol,
                "count": group.count,
                "lines": list(group.lines),
                "family": group.family,
                "killability": group.killability,
                "mutator": group.mutator,
                "effort_score": group.effort_score,
                "effort_band": group.effort_band,
                "roi": group.roi,
                "deprioritized": group.deprioritized,
                "likely_equivalent": group.likely_equivalent,
                "survivors": list(group.survivors),
                "representative_diffs": list(group.representative_diffs),
            }
            for group in context.groups
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(value or "unknown")).strip("_").lower() or "unknown"
