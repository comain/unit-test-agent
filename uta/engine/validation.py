from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set, Tuple


@dataclass(frozen=True)
class PlanCallable:
    """Normalized callable metadata used by plan validators.

    Language extractors convert parser-specific functions or methods into this
    shape so breadth and feasibility checks can stay language-neutral.
    """

    name: str
    qualified_name: str = ""
    kind: str = "method"
    visibility_rank: int = 0
    start_line: int = 0
    end_line: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_roi_method(self) -> Dict[str, Any]:
        body_lines = max(1, int(self.end_line or self.start_line or 1) - int(self.start_line or 1) + 1)
        return {
            "name": self.name,
            "fqn": self.qualified_name or self.name,
            "visibility_rank": self.visibility_rank,
            "missed_lines": body_lines,
            "planning_reach_lines": body_lines,
            "planning_score": float(body_lines),
            "effort_score": 1,
        }


@dataclass(frozen=True)
class PlanContext:
    """Normalized set of callables available for validating a generated plan."""

    callables: List[PlanCallable]
    language: str = "unknown"

    @property
    def public_method_names(self) -> Set[str]:
        return {
            item.name
            for item in self.callables
            if item.name and int(item.visibility_rank) == 0
        }


class PlanContextExtractor(Protocol):
    """Converts language-specific context payloads into PlanContext."""

    language: str

    def can_extract(self, context: Any) -> bool:
        ...

    def extract(self, context: Any) -> PlanContext:
        ...


class PlanContextExtractorRegistry:
    """Registry that finds the right plan context extractor for a payload."""

    def __init__(self, extractors: Iterable[PlanContextExtractor] = ()):
        self._extractors: Dict[str, PlanContextExtractor] = {}
        for extractor in extractors:
            self.register(extractor)

    def register(self, extractor: PlanContextExtractor) -> None:
        language = str(extractor.language or "").strip().lower()
        if not language:
            raise ValueError("Plan context extractor language is required")
        if language in self._extractors:
            raise ValueError(f"Plan context extractor already registered: {language}")
        self._extractors[language] = extractor

    def extractor_for(self, language: str) -> PlanContextExtractor:
        normalized = str(language or "").strip().lower()
        try:
            return self._extractors[normalized]
        except KeyError:
            raise ValueError(f"Unsupported plan context language: {language}") from None

    def extract(self, context: Any, *, language: Optional[str] = None) -> PlanContext:
        if language:
            return self.extractor_for(language).extract(context)
        context_language = _context_language(context)
        if context_language:
            return self.extractor_for(context_language).extract(context)
        for extractor in self._extractors.values():
            if extractor.can_extract(context):
                return extractor.extract(context)
        raise ValueError("Unable to infer plan context language; provide a context extractor or language")

    @property
    def languages(self) -> Tuple[str, ...]:
        return tuple(sorted(self._extractors))


def default_plan_context_registry() -> PlanContextExtractorRegistry:
    from uta.language.java.validation import JavaMarkdownPlanContextExtractor
    from uta.language.python.validation import PythonPayloadPlanContextExtractor

    return PlanContextExtractorRegistry(
        (
            PythonPayloadPlanContextExtractor(),
            JavaMarkdownPlanContextExtractor(),
        )
    )


def coerce_plan_context(
    context: Any,
    extractor: Optional[PlanContextExtractor] = None,
    *,
    language: Optional[str] = None,
    registry: Optional[PlanContextExtractorRegistry] = None,
) -> PlanContext:
    if isinstance(context, PlanContext):
        return context
    if extractor:
        return extractor.extract(context)
    return (registry or default_plan_context_registry()).extract(context, language=language)


def roi_methods_from_callables(callables: Iterable[PlanCallable]) -> List[Dict[str, Any]]:
    return [item.as_roi_method() for item in callables]


class BreadthVerdict(str, Enum):
    """Outcome of checking whether a plan covers enough known callables."""

    PASS = "PASS"
    UNDER = "UNDER"
    OVER = "OVER"


@dataclass(frozen=True)
class BreadthResult:
    """Detailed result from the plan breadth validator."""

    verdict: BreadthVerdict
    planned_methods: int
    known_methods: int
    coverage_ratio: float
    missing_methods: List[str]
    extra_methods: List[str]
    message: str


class FeasibilityVerdict(str, Enum):
    """Outcome of checking whether a plan can plausibly satisfy the gate."""

    PASS = "PASS"
    UNDER = "UNDER"


@dataclass(frozen=True)
class FeasibilityResult:
    """Detailed result from the plan feasibility validator."""

    verdict: FeasibilityVerdict
    planned_methods: int
    candidate_methods: int
    score_ratio: float
    direct_ratio: float
    top_methods_considered: int
    top_methods_covered: int
    missing_anchor_methods: List[str]
    low_yield_methods: List[str]
    message: str


_PLANNED_CODE_SPAN_RE = re.compile(r"`([^`]+)`", re.MULTILINE)
_PLANNED_METHOD_CALL_RE = re.compile(r"\b([a-z][A-Za-z0-9_]+)\s*\(")
_PLANNED_SIMPLE_NAME_RE = re.compile(r"^[a-z][A-Za-z0-9_]+$")
_SECTION_RE = re.compile(
    r"METHODS REQUIRED FOR GATE\s*(.*?)(?=^\s*\d+\.\s+[A-Z][A-Z ]+\s*$|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_SKIP_KNOWN = frozenset({"main", "equals", "hashCode", "toString", "clone"})


def _context_language(context: Any) -> Optional[str]:
    if isinstance(context, dict):
        language = context.get("language")
        return str(language).strip().lower() if language else None
    language = getattr(context, "language", None)
    return str(language).strip().lower() if language else None


def extract_planned_methods_from_plan(plan_text: str) -> Set[str]:
    """Extract method/callable names the plan intends to cover."""
    names: Set[str] = set()
    for match in _PLANNED_CODE_SPAN_RE.finditer(plan_text or ""):
        content = match.group(1).strip()
        if not content:
            continue
        call_matches = list(_PLANNED_METHOD_CALL_RE.finditer(content))
        if call_matches:
            for call_match in call_matches:
                name = call_match.group(1)
                if name not in _SKIP_KNOWN:
                    names.add(name)
            continue
        if _PLANNED_SIMPLE_NAME_RE.fullmatch(content) and content not in _SKIP_KNOWN:
            names.add(content)
    return names


def validate_plan_breadth(
    plan_text: str,
    context: Any,
    *,
    context_extractor: Optional[PlanContextExtractor] = None,
    language: Optional[str] = None,
    registry: Optional[PlanContextExtractorRegistry] = None,
    min_coverage_ratio: float = 0.6,
    max_over_ratio: float = 1.5,
) -> BreadthResult:
    """Validate that ``plan_text`` covers public callables listed by context."""
    known = coerce_plan_context(
        context,
        context_extractor,
        language=language,
        registry=registry,
    ).public_method_names
    planned = extract_planned_methods_from_plan(plan_text)

    if not known:
        return BreadthResult(
            verdict=BreadthVerdict.PASS,
            planned_methods=len(planned),
            known_methods=0,
            coverage_ratio=1.0,
            missing_methods=[],
            extra_methods=[],
            message="No public methods found in context; breadth gate skipped.",
        )

    covered = known & planned
    ratio = len(covered) / len(known)
    missing = sorted(known - planned)
    extra = sorted(planned - known)

    if len(planned) > len(known) * max_over_ratio:
        verdict = BreadthVerdict.OVER
        message = (
            f"Plan lists {len(planned)} methods but only {len(known)} are known "
            f"(ratio {len(planned)/len(known):.1f}x). "
            f"Extra entries may inflate the generation prompt."
        )
    elif ratio < min_coverage_ratio:
        verdict = BreadthVerdict.UNDER
        message = (
            f"Plan covers {len(covered)}/{len(known)} methods ({ratio:.0%}), "
            f"below the {min_coverage_ratio:.0%} breadth gate. "
            f"Missing: {', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}."
        )
    else:
        verdict = BreadthVerdict.PASS
        message = f"Plan covers {len(covered)}/{len(known)} methods ({ratio:.0%}). Breadth gate passed."

    return BreadthResult(
        verdict=verdict,
        planned_methods=len(planned),
        known_methods=len(known),
        coverage_ratio=ratio,
        missing_methods=missing,
        extra_methods=extra,
        message=message,
    )


def _extract_gate_methods(plan_text: str) -> Set[str]:
    match = _SECTION_RE.search(plan_text or "")
    if not match:
        return extract_planned_methods_from_plan(plan_text or "")
    section = match.group(1).strip()
    extracted = extract_planned_methods_from_plan(section)
    if extracted:
        return extracted
    return extract_planned_methods_from_plan(plan_text or "")


def validate_plan_feasibility(
    plan_text: str,
    roi_methods: Optional[List[Dict[str, Any]]] = None,
    *,
    coverage_gate: int,
    callables: Optional[Iterable[PlanCallable]] = None,
    plan_context: Optional[PlanContext] = None,
) -> FeasibilityResult:
    gate_methods = _extract_gate_methods(plan_text)
    if roi_methods is None:
        source_callables = list(callables or (plan_context.callables if plan_context else []))
        roi_methods = roi_methods_from_callables(source_callables)
    public_candidates = [
        method for method in (roi_methods or [])
        if int(method.get("visibility_rank", 2)) == 0 and int(method.get("missed_lines", 0) or 0) > 0
    ]
    if not public_candidates:
        return FeasibilityResult(
            verdict=FeasibilityVerdict.PASS,
            planned_methods=len(gate_methods),
            candidate_methods=0,
            score_ratio=1.0,
            direct_ratio=1.0,
            top_methods_considered=0,
            top_methods_covered=0,
            missing_anchor_methods=[],
            low_yield_methods=[],
            message="No positive-gain public ROI methods available; feasibility gate skipped.",
        )

    methods_by_name = {str(method.get("name", "")): method for method in public_candidates}
    ranked = sorted(
        public_candidates,
        key=lambda method: (
            -float(method.get("planning_score", 0.0) or 0.0),
            -int(method.get("planning_reach_lines", 0) or 0),
            -int(method.get("missed_lines", 0) or 0),
            int(method.get("effort_score", 0) or 0),
            str(method.get("name", "")),
        ),
    )
    top_pool_size = min(len(ranked), max(6, int(math.ceil(len(ranked) * 0.35))))
    top_pool = ranked[:top_pool_size]

    total_score = sum(float(method.get("planning_score", 0.0) or 0.0) for method in public_candidates)
    total_direct = sum(int(method.get("missed_lines", 0) or 0) for method in public_candidates)
    planned_roi = [methods_by_name[name] for name in gate_methods if name in methods_by_name]
    planned_score = sum(float(method.get("planning_score", 0.0) or 0.0) for method in planned_roi)
    planned_direct = sum(int(method.get("missed_lines", 0) or 0) for method in planned_roi)

    score_ratio = 1.0 if total_score <= 0 else planned_score / total_score
    direct_ratio = 1.0 if total_direct <= 0 else planned_direct / total_direct
    top_names = [str(method.get("name", "")) for method in top_pool]
    top_hits = [name for name in top_names if name in gate_methods]
    missing_anchors = [name for name in top_names if name not in gate_methods]

    positive_scores = sorted(float(method.get("planning_score", 0.0) or 0.0) for method in public_candidates)
    median_score = positive_scores[len(positive_scores) // 2] if positive_scores else 0.0
    low_yield = sorted(
        name for name in gate_methods
        if name in methods_by_name and float(methods_by_name[name].get("planning_score", 0.0) or 0.0) < median_score
    )

    min_score_ratio = 0.58 if coverage_gate >= 75 else 0.45
    min_direct_ratio = 0.50 if coverage_gate >= 75 else 0.38
    min_top_hits = min(top_pool_size, 4 if coverage_gate >= 75 else 3)
    too_many_low_yield = (
        bool(gate_methods)
        and len(low_yield) > max(2, len(gate_methods) // 2)
        and (score_ratio < max(min_score_ratio + 0.10, 0.75) or len(top_hits) < min_top_hits)
    )

    under_reasons: List[str] = []
    if score_ratio < min_score_ratio:
        under_reasons.append(
            f"planning-score reach {score_ratio:.0%} is below the {min_score_ratio:.0%} feasibility floor"
        )
    if direct_ratio < min_direct_ratio:
        under_reasons.append(
            f"direct uncovered-line reach {direct_ratio:.0%} is below the {min_direct_ratio:.0%} feasibility floor"
        )
    if len(top_hits) < min_top_hits:
        anchors = ", ".join(missing_anchors[:5])
        under_reasons.append(
            f"only {len(top_hits)}/{top_pool_size} top public anchor methods are covered"
            + (f"; missing {anchors}" if anchors else "")
        )
    if too_many_low_yield:
        under_reasons.append(
            "gate method list still contains too many lower-yield methods relative to the public ROI ordering"
        )

    if under_reasons:
        return FeasibilityResult(
            verdict=FeasibilityVerdict.UNDER,
            planned_methods=len(gate_methods),
            candidate_methods=len(public_candidates),
            score_ratio=score_ratio,
            direct_ratio=direct_ratio,
            top_methods_considered=top_pool_size,
            top_methods_covered=len(top_hits),
            missing_anchor_methods=missing_anchors,
            low_yield_methods=low_yield,
            message="; ".join(under_reasons),
        )

    return FeasibilityResult(
        verdict=FeasibilityVerdict.PASS,
        planned_methods=len(gate_methods),
        candidate_methods=len(public_candidates),
        score_ratio=score_ratio,
        direct_ratio=direct_ratio,
        top_methods_considered=top_pool_size,
        top_methods_covered=len(top_hits),
        missing_anchor_methods=missing_anchors,
        low_yield_methods=low_yield,
        message=(
            f"Gate methods cover {score_ratio:.0%} of planning score, "
            f"{direct_ratio:.0%} of direct uncovered lines, and {len(top_hits)}/{top_pool_size} anchor methods."
        ),
    )
