from __future__ import annotations

import ast
import importlib
import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from difflib import unified_diff
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from uta.shared.config import settings
from uta.enforcement.mutation_repair import (
    MutationRepairContext,
    MutationRepairGroup,
    MutationRepairRoundState,
    plan_mutation_repair,
    should_split_mutation_repair,
    write_mutation_repair_context,
)
from uta.enforcement.mutation_roi import effort_band, family_effort, mutation_roi_score
from uta.language.python.verification.models import (
    CommandEvidence,
    MutationSummary,
    PythonVerificationResult,
)


RunCommand = Callable[..., subprocess.CompletedProcess]

# mutmut emits a diff rather than a named mutator, so the mutation kind is
# inferred from what the diff changed. Families mirror the Java PIT taxonomy in
# uta/language/java/maven/pitest.py so both backends can populate one shared,
# ROI-rankable mutation-repair model.
_COMPARISON_RE = re.compile(r"<=|>=|==|!=|<|>")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_ARITHMETIC_RE = re.compile(r"\*\*|//|[+\-*/%]")


def _diff_old_new(diff_text: str) -> Tuple[str, str]:
    old: List[str] = []
    new: List[str] = []
    for raw in (diff_text or "").splitlines():
        if raw.startswith(("---", "+++", "@@")):
            continue
        if raw.startswith("-"):
            old.append(raw[1:])
        elif raw.startswith("+"):
            new.append(raw[1:])
    return "\n".join(old), "\n".join(new)


def classify_mutmut_family(diff_text: str) -> str:
    """Infer the mutation family from a mutmut ``show`` diff.

    Returns one of the shared families: ``boundary``, ``conditional``,
    ``negation``, ``return_value``, ``math``, ``side_effect``, ``other``.
    """
    old, new = _diff_old_new(diff_text)
    if not old and not new:
        return "other"
    old_words = set(_WORD_RE.findall(old))
    new_words = set(_WORD_RE.findall(new))
    changed_words = old_words ^ new_words

    comparison = set(_COMPARISON_RE.findall(old)) ^ set(_COMPARISON_RE.findall(new))
    if comparison:
        if ("<" in comparison and "<=" in comparison) or (">" in comparison and ">=" in comparison):
            return "boundary"
        return "conditional"
    if {"and", "or"} & changed_words:
        return "conditional"
    if "not" in changed_words and ({"in", "is"} & (old_words | new_words)):
        return "conditional"
    if {"True", "False"} & changed_words or "not" in changed_words:
        return "negation"
    if {"break", "continue"} & changed_words:
        return "side_effect"
    # Operator/literal changes win over return_value: a math/comparison change on
    # a `return` line is still that family (matches the Java mutator-first taxonomy).
    if set(_ARITHMETIC_RE.findall(old)) ^ set(_ARITHMETIC_RE.findall(new)):
        return "math"
    if re.findall(r"\d+", old) != re.findall(r"\d+", new):
        return "math"
    if old.lstrip().startswith("return") or new.lstrip().startswith("return"):
        return "return_value"
    return "other"


def python_mutation_killability(family: str) -> Tuple[int, str]:
    """Killability heuristic for a mutation family (mirrors Java _killability_score)."""
    if family in {"boundary", "conditional", "return_value"}:
        return (3, "high")
    if family in {"math", "negation"}:
        return (1, "medium")
    if family == "side_effect":
        return (1, "low")
    return (1, "low")


def _dominant_family(diffs: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
    """Pick the highest-killability family in a group, breaking ties by frequency."""
    families = [str(diff.get("family") or "") for diff in diffs if diff.get("family")]
    if not families:
        return "", ""
    counts: Dict[str, int] = {}
    for family in families:
        counts[family] = counts.get(family, 0) + 1
    dominant = max(counts, key=lambda fam: (python_mutation_killability(fam)[0], counts[fam]))
    return dominant, python_mutation_killability(dominant)[1]


def _rank_groups_by_roi(
    groups: Sequence[MutationRepairGroup],
    method_efforts: Sequence[Mapping[str, Any]],
) -> List[MutationRepairGroup]:
    """Attach kill-per-effort ROI to each group and sort highest-ROI first.

    Mirrors Java ``score_families``/``roi_sort_key`` using the shared
    ``uta.enforcement.mutation_roi`` math; the per-symbol base effort comes from the
    Python scorer (``effort_score``), defaulting to 2 for unscored symbols.
    """
    lookup: Dict[str, Mapping[str, Any]] = {}
    for eff in method_efforts or []:
        name = eff.get("name") or str(eff.get("fqn") or "").split(".")[-1]
        if not name:
            continue
        existing = lookup.get(name)
        if existing is None or int(eff.get("effort_score", 0) or 0) > int(existing.get("effort_score", 0) or 0):
            lookup[name] = eff
    ranked: List[MutationRepairGroup] = []
    for group in groups:
        killability_score = python_mutation_killability(group.family)[0]
        base = int((lookup.get(group.symbol) or {}).get("effort_score", 2) or 2)
        effort = family_effort(base, group.family or "other")
        roi = mutation_roi_score(group.count, killability_score, effort)
        ranked.append(replace(group, roi=roi, effort_score=str(effort), effort_band=effort_band(effort)))
    ranked.sort(
        key=lambda g: (
            -(g.roi or 0.0),
            -python_mutation_killability(g.family)[0],
            g.symbol,
            g.lines[0] if g.lines else 0,
        )
    )
    return ranked


def build_python_mutation_repair_context(
    *,
    repo: Path,
    target_id: str,
    source_path: str,
    test_paths: Sequence[str],
    verification: PythonVerificationResult,
    mutmut_bin: Optional[str] = None,
    limit_per_symbol: Optional[int] = None,
    repair_attempt: int = 1,
    runner: Optional[RunCommand] = None,
    artifact_dir: Optional[Path] = None,
    method_efforts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> MutationRepairContext:
    """Build Python mutmut survivor context before the repair LLM is called.

    When ``method_efforts`` is supplied (per-callable effort from
    ``PythonTargetScorer``), groups are scored by kill-per-effort ROI via the
    shared ``uta.enforcement.mutation_roi`` math and re-ordered highest-ROI first —
    the Python counterpart of Java's ``score_families``. Without it, groups keep
    their symbol order and carry no ROI fields (prompt unchanged)."""

    mutation = verification.mutation
    survivors = list((mutation.diff_survivors if mutation and mutation.diff_survivors else mutation.survivors) if mutation else [])
    synthetic_failures = _timeout_or_suspicious_candidates_from_plan(mutation, source_path=source_path)
    if synthetic_failures:
        existing_ids = {str(item.get("id") or "") for item in survivors}
        survivors.extend(item for item in synthetic_failures if str(item.get("id") or "") not in existing_ids)
    failure_breakdown = _mutation_failure_breakdown(mutation, fallback_survivor_count=len(survivors))
    repair_survivor_count = sum(failure_breakdown.values()) if failure_breakdown else len(survivors)
    command_runner = runner or _subprocess_run
    mutmut = mutmut_bin or _mutmut_bin_from_commands(verification.commands) or "mutmut"
    grouped = _group_survivors_by_symbol(Path(repo) / source_path, survivors)
    batches = _batch_survivor_groups(
        grouped,
        max_batch_size=max(1, int(settings.python_mutation_repair_batch_max_survivors or 60)),
    )
    group_limit = int(limit_per_symbol if limit_per_symbol is not None else settings.python_mutant_diff_limit_per_symbol)
    exact_diff_limit = max(1, int(settings.python_mutation_repair_exact_diff_limit or 60))
    show_timeout = max(1, int(settings.python_mutant_show_timeout_seconds or 10))
    show_budget = _ShowBudget(max(1, int(settings.python_mutant_show_total_timeout_seconds or show_timeout)))
    internal_show_cache: Dict[str, Tuple[Any, Any]] = {}
    split = should_split_mutation_repair(
        repair_survivor_count,
        threshold=int(settings.python_mutation_repair_split_threshold),
    )
    batch_count = max(1, len(batches))
    symbol_visit_attempt = _symbol_visit_attempt(repair_attempt, batch_count) if split else max(1, int(repair_attempt or 1))
    candidates = [
        MutationRepairGroup(
            symbol=symbol,
            count=len(items),
            lines=tuple(sorted({int(item.get("line") or 0) for item in items if item.get("line")})),
            survivors=tuple(
                _select_representative_survivors(
                    items,
                    limit=group_limit,
                    visit_attempt=symbol_visit_attempt,
                )
            ),
        )
        for symbol, items in batches
    ]
    if method_efforts is not None:
        candidates = _rank_groups_by_roi(candidates, method_efforts)
    draft_plan = plan_mutation_repair(
        MutationRepairContext(
            target_id=target_id,
            source_path=source_path,
            test_paths=tuple(test_paths),
            reproduce_command=_reproduce_command(verification.commands, fallback_source=source_path),
            survivor_count=repair_survivor_count,
            groups=tuple(candidates),
            language="python",
            failure_breakdown=failure_breakdown,
        ),
        MutationRepairRoundState(target_id=target_id, attempt_index=repair_attempt),
        focused_group_count=max(1, int(settings.mutation_repair_groups_per_round or 1)),
    )
    selected_symbols = {group.symbol for group in draft_plan.selected_groups}
    batch_items = {symbol: items for symbol, items in batches}
    groups: List[MutationRepairGroup] = []
    for candidate in candidates:
        symbol = candidate.symbol
        items = batch_items.get(symbol, [])
        selected_for_repair = not split or symbol in selected_symbols
        representatives = (
            list(items[: min(len(items), exact_diff_limit)])
            if selected_for_repair
            else _select_representative_survivors(
                items,
                limit=group_limit,
                visit_attempt=symbol_visit_attempt,
            )
        )
        diffs: List[Dict[str, str]] = []
        if selected_for_repair:
            for survivor in representatives:
                diff = _mutmut_show_diff(
                    Path(repo),
                    survivor,
                    mutmut_bin=mutmut,
                    source_path=source_path,
                    internal_show_cache=internal_show_cache,
                    runner=command_runner,
                    timeout_seconds=show_timeout,
                    budget=show_budget,
                )
                if not diff:
                    continue
                family = classify_mutmut_family(diff.get("output", ""))
                killability_score, killability = python_mutation_killability(family)
                diff["family"] = family
                diff["killability"] = killability
                diff["killability_score"] = str(killability_score)
                diffs.append(diff)
        group_family, group_killability = _dominant_family(diffs)
        groups.append(
            MutationRepairGroup(
                symbol=symbol,
                count=len(items),
                lines=candidate.lines,
                survivors=tuple(representatives),
                representative_diffs=tuple(diffs),
                family=group_family,
                killability=group_killability,
                effort_score=candidate.effort_score,
                effort_band=candidate.effort_band,
                roi=candidate.roi,
                deprioritized=candidate.deprioritized,
                likely_equivalent=candidate.likely_equivalent,
            )
        )
    context = MutationRepairContext(
        target_id=target_id,
        source_path=source_path,
        test_paths=tuple(test_paths),
        reproduce_command=_reproduce_command(verification.commands, fallback_source=source_path),
        survivor_count=repair_survivor_count,
        groups=tuple(groups),
        language="python",
        failure_breakdown=failure_breakdown,
    )
    return write_mutation_repair_context(
        artifact_dir or Path(repo) / ".uta_cache" / "python" / "mutation_repair",
        context,
        split_threshold=int(settings.python_mutation_repair_split_threshold),
    )


def _mutation_failure_breakdown(
    mutation: Optional[MutationSummary],
    *,
    fallback_survivor_count: int,
) -> Dict[str, int]:
    if mutation is None:
        return {"survived": int(fallback_survivor_count or 0)} if fallback_survivor_count else {}
    breakdown = {
        "survived": int(mutation.survived or 0),
        "timeout": int(mutation.timeout or 0),
        "suspicious": int(mutation.suspicious or 0),
    }
    if not any(breakdown.values()) and fallback_survivor_count:
        breakdown["survived"] = int(fallback_survivor_count)
    return {key: value for key, value in breakdown.items() if value}


def _timeout_or_suspicious_candidates_from_plan(
    mutation: Optional[MutationSummary],
    *,
    source_path: str,
) -> List[Dict[str, Any]]:
    if mutation is None:
        return []
    needed_statuses = [
        *("timeout" for _ in range(max(0, int(mutation.timeout or 0)))),
        *("suspicious" for _ in range(max(0, int(mutation.suspicious or 0)))),
    ]
    if not needed_statuses:
        return []
    plan = mutation.candidate_plan or {}
    selected = plan.get("activeSelected") or plan.get("reportFullSelected") or []
    if not isinstance(selected, list):
        return []
    selected_with_status = [
        item
        for item in selected
        if isinstance(item, Mapping) and str(item.get("executionStatus") or "").strip()
    ]
    exact_failures = [
        (str(item.get("executionStatus") or "").strip(), item)
        for item in selected_with_status
        if str(item.get("executionStatus") or "").strip() in {"timeout", "suspicious"}
    ]
    candidates = exact_failures if exact_failures else list(zip(needed_statuses, selected))
    failures: List[Dict[str, Any]] = []
    for status, item in candidates:
        if not isinstance(item, Mapping):
            continue
        opportunity = item.get("opportunity") if isinstance(item.get("opportunity"), Mapping) else {}
        mutant_id = str(item.get("toolCandidateKey") or "").strip()
        line = int(opportunity.get("line") or 0) if isinstance(opportunity, Mapping) else 0
        operator = str(opportunity.get("operatorName") or "").strip() if isinstance(opportunity, Mapping) else ""
        inferred = not bool(exact_failures)
        failure = {
                "file": str(opportunity.get("sourcePath") or source_path) if isinstance(opportunity, Mapping) else source_path,
                "line": line,
                "description": (
                    f"{status} mutation candidate"
                    + (f" ({operator})" if operator else "")
                    + ("; candidate inferred from deterministic candidate-plan order" if inferred else "")
                ),
                "id": mutant_id,
                "status": status,
                "operator": operator,
            }
        show_output = str(item.get("mutmutShowOutput") or item.get("mutmut_show_output") or "").strip()
        show_command = str(item.get("mutmutShowCommand") or item.get("mutmut_show_command") or "").strip()
        if show_output:
            failure["mutmut_show_output"] = show_output
        if show_command:
            failure["mutmut_show_command"] = show_command
        failures.append(failure)
        if len(failures) >= len(needed_statuses):
            break
    return failures


def load_python_survivors(repo: Path, survivors_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load Python mutmut survivors from a verifier artifact path."""

    path = Path(survivors_path) if survivors_path else Path(repo) / ".uta_cache" / "python" / "mutation" / "survivors.json"
    if not path.is_absolute():
        path = Path(repo) / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _group_survivors_by_symbol(source_file: Path, survivors: Sequence[Mapping[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    ranges = _python_symbol_ranges(source_file)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for survivor in survivors:
        item = dict(survivor)
        line = int(item.get("line") or 0)
        symbol = _symbol_for_line(ranges, line) if line else _symbol_from_mutmut_id(str(item.get("id") or ""))
        item.setdefault("symbol", symbol)
        grouped.setdefault(symbol, []).append(item)
    return sorted(
        (
            (
                symbol,
                sorted(items, key=_survivor_sort_key),
            )
            for symbol, items in grouped.items()
        ),
        key=lambda pair: (-len(pair[1]), pair[0]),
    )


def _batch_survivor_groups(
    grouped: Sequence[Tuple[str, List[Dict[str, Any]]]],
    *,
    max_batch_size: int,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Split oversized symbol groups into mutation-repair batches."""

    batch_size = max(1, int(max_batch_size or 1))
    batches: List[Tuple[str, List[Dict[str, Any]]]] = []
    for symbol, items in grouped:
        if len(items) <= batch_size:
            batches.append((symbol, list(items)))
            continue
        total = (len(items) + batch_size - 1) // batch_size
        for index, start in enumerate(range(0, len(items), batch_size), start=1):
            chunk = list(items[start : start + batch_size])
            batches.append((f"{symbol}#batch-{index}-of-{total}", chunk))
    return sorted(batches, key=lambda pair: (-len(pair[1]), pair[0]))


def _survivor_sort_key(item: Mapping[str, Any]) -> Tuple[int, str, int, str]:
    mutant_id = str(item.get("id") or item.get("mutant") or "")
    return (
        int(item.get("line") or 0),
        _symbol_from_mutmut_id(mutant_id),
        _mutant_number(mutant_id),
        str(item.get("description") or ""),
    )


def _mutant_number(mutant_id: str) -> int:
    if "__mutmut_" not in mutant_id:
        return 0
    suffix = mutant_id.rsplit("__mutmut_", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return 0


def _python_symbol_ranges(source_file: Path) -> List[Tuple[int, int, str]]:
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    ranges: List[Tuple[int, int, str]] = []

    def visit(nodes: Iterable[ast.AST], owners: Sequence[str] = ()) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*owners, node.name))
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = int(getattr(node, "lineno", 0) or 0)
                end = int(getattr(node, "end_lineno", start) or start)
                symbol = ".".join((*owners, node.name))
                ranges.append((start, end, symbol))
                visit(node.body, (*owners, node.name))

    visit(tree.body)
    return sorted(ranges, key=lambda item: (item[0], -item[1], item[2]))


@dataclass
class _ShowBudget:
    total_seconds: int
    started_at: float = 0.0

    def __post_init__(self) -> None:
        self.started_at = time.monotonic()

    def remaining(self) -> float:
        return max(0.0, float(self.total_seconds) - (time.monotonic() - self.started_at))


def _symbol_for_line(ranges: Sequence[Tuple[int, int, str]], line: int) -> str:
    candidates = [item for item in ranges if item[0] <= line <= item[1]]
    if not candidates:
        return "(module)"
    return sorted(candidates, key=lambda item: (item[1] - item[0], item[2]))[0][2]


def _symbol_from_mutmut_id(mutant_id: str) -> str:
    marker = ".x"
    if marker not in mutant_id or "__mutmut_" not in mutant_id:
        return "(module)"
    encoded = mutant_id.split(marker, 1)[1].rsplit("__mutmut_", 1)[0]
    if encoded.startswith("ǁ"):
        parts = [part for part in encoded.split("ǁ") if part]
        return ".".join(parts) if parts else "(module)"
    if encoded.startswith("_"):
        return encoded[1:] or "(module)"
    return encoded or "(module)"


def _symbol_visit_attempt(repair_attempt: int, symbol_count: int) -> int:
    return ((max(1, int(repair_attempt or 1)) - 1) // max(1, int(symbol_count or 1))) + 1


def _select_representative_survivors(
    survivors: Sequence[Dict[str, Any]],
    *,
    limit: int,
    visit_attempt: int,
) -> List[Dict[str, Any]]:
    if limit <= 0 or not survivors:
        return []
    capped = min(int(limit), len(survivors))
    start = ((max(1, int(visit_attempt or 1)) - 1) * capped) % len(survivors)
    ordered = list(survivors[start:]) + list(survivors[:start])
    return ordered[:capped]


def _mutmut_show_diff(
    repo: Path,
    survivor: Mapping[str, Any],
    *,
    mutmut_bin: str,
    source_path: str,
    internal_show_cache: Dict[str, Tuple[Any, Any]],
    runner: RunCommand,
    timeout_seconds: int,
    budget: _ShowBudget,
) -> Optional[Dict[str, str]]:
    mutant_id = str(survivor.get("id") or survivor.get("mutant") or "").strip()
    if not mutant_id:
        return None
    stored_output = _compact_diff(str(survivor.get("mutmut_show_output") or survivor.get("show_output") or ""))
    if stored_output:
        command = str(survivor.get("mutmut_show_command") or "").strip()
        if not command:
            command = " ".join(shlex.quote(part) for part in [mutmut_bin, "show", mutant_id])
        return {"command": command, "output": stored_output}
    internal_diff = _mutmut_internal_show_diff(
        repo,
        survivor,
        source_path=source_path,
        module_cache=internal_show_cache,
    )
    if internal_diff:
        return internal_diff
    command = [mutmut_bin, "show", mutant_id]
    rendered_command = " ".join(shlex.quote(part) for part in command)
    remaining = budget.remaining()
    if remaining <= 0:
        return _mutmut_show_unavailable(rendered_command, "skipped because the mutmut show total budget was exhausted")
    effective_timeout = max(1, min(int(timeout_seconds), int(remaining)))
    try:
        result = runner(command, cwd=repo, timeout=effective_timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return _mutmut_show_unavailable(rendered_command, f"timed out after {effective_timeout}s")
    output = _decode(result.stdout) + _decode(result.stderr)
    if int(getattr(result, "returncode", 0) or 0) != 0:
        reason = _summarize_mutmut_show_failure(output)
        return _mutmut_show_unavailable(rendered_command, reason)
    output = _compact_diff(output)
    if not output:
        return None
    return {"command": rendered_command, "output": output}


def _mutmut_internal_show_diff(
    repo: Path,
    survivor: Mapping[str, Any],
    *,
    source_path: str,
    module_cache: Dict[str, Tuple[Any, Any]],
) -> Optional[Dict[str, str]]:
    mutant_id = str(survivor.get("id") or survivor.get("mutant") or "").strip()
    if not mutant_id:
        return None
    path = _mutmut_source_path(survivor, fallback_source_path=source_path)
    if not path:
        return None
    try:
        mutmut_main, module = _mutmut_internal_module(repo, path, module_cache)
        orig_code = mutmut_main.cst.Module([mutmut_main.read_original_function(module, mutant_id)]).code.strip()
        mutant_code = mutmut_main.cst.Module([mutmut_main.read_mutant_function(module, mutant_id)]).code.strip()
    except Exception:
        return None
    description = str(survivor.get("description") or "survived").strip() or "survived"
    diff_lines = unified_diff(
        orig_code.split("\n"),
        mutant_code.split("\n"),
        fromfile=path,
        tofile=path,
        lineterm="",
    )
    output = _compact_diff("\n".join([f"# {mutant_id}: {description}", *diff_lines]))
    if not output:
        return None
    return {
        "command": f"mutmut internal show {shlex.quote(mutant_id)} --path {shlex.quote(path)}",
        "output": output,
    }


def _mutmut_internal_module(repo: Path, source_path: str, module_cache: Dict[str, Tuple[Any, Any]]) -> Tuple[Any, Any]:
    cached = module_cache.get(source_path)
    if cached:
        return cached
    mutants_source = Path(repo) / "mutants" / source_path
    # The subprocess path below this one is bounded by a timeout and a budget;
    # this in-process parse is not, and a mutated module is a full copy of the
    # source with every mutant inlined. Parsing a very large one is the
    # 100%-CPU tokenizer hang seen on node2, so refuse it here and let the
    # budgeted `mutmut show` fall back instead of blocking the phase.
    size = mutants_source.stat().st_size
    limit = max(1, int(settings.python_mutation_generation_max_source_bytes or 1))
    if size > limit:
        raise ValueError(
            f"mutated module {source_path} is {size} bytes, above the "
            f"{limit}-byte in-process parse limit"
        )
    source = mutants_source.read_text(encoding="utf-8")
    mutmut_main = importlib.import_module("mutmut.__main__")
    module = mutmut_main.cst.parse_module(source)
    module_cache[source_path] = (mutmut_main, module)
    return mutmut_main, module


def _mutmut_source_path(survivor: Mapping[str, Any], *, fallback_source_path: str) -> str:
    file_path = str(survivor.get("file") or "").strip()
    if file_path:
        return file_path
    return str(fallback_source_path or "").strip()


def _summarize_mutmut_show_failure(output: str) -> str:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if not lines:
        return "mutmut show failed without output"
    for line in reversed(lines):
        if "Error" in line or "Exception" in line or "Could not find mutant" in line:
            return line[:240]
    return lines[-1][:240]


def _mutmut_show_unavailable(command: str, reason: str) -> Dict[str, str]:
    return {
        "command": command,
        "output": f"[mutmut show unavailable: {reason}]",
    }


def _mutmut_bin_from_commands(commands: Sequence[CommandEvidence]) -> Optional[str]:
    for command in reversed(list(commands or [])):
        if command.name == "mutmut_run" and command.command:
            return str(command.command[0])
    return None


def _reproduce_command(commands: Sequence[CommandEvidence], *, fallback_source: str) -> str:
    for command in reversed(list(commands or [])):
        if command.name == "mutmut_run":
            return " ".join(shlex.quote(str(part)) for part in command.command)
    return f"mutmut run --paths-to-mutate {shlex.quote(fallback_source)}"


def _subprocess_run(cmd: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[int] = None, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=dict(env) if env else None,
        capture_output=True,
        check=False,
    )


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _compact_diff(output: str, *, max_lines: int = 80) -> str:
    lines = [line for line in str(output or "").splitlines() if not _is_mutmut_progress_line(line)]
    return "\n".join(lines[:max_lines]).strip()


def _is_mutmut_progress_line(line: str) -> bool:
    stripped = line.strip()
    return bool("/" in stripped and any(token in stripped for token in ("🎉", "🫥", "🙁", "⏰", "🤔")))
