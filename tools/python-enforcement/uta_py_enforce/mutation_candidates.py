from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from uta_enforce_core.mutation_candidates import (
    MUTATION_CANDIDATE_PLANNER_VERSION,
    MUTMUT3_METADATA_SELECTED_EXECUTION,
    MutationCandidate,
    MutationCandidatePlan,
    MutationOpportunity,
    MutationSamplingLayer,
    MutationVerificationContext,
    SuppressedMutationOpportunity,
    make_candidate_id,
    make_candidate_plan_id,
    make_opportunity_id,
)
from uta_enforce_core.mutation_suppression import (
    ASYNC_SCHEDULER_LOOP,
    GENERATED_FRAMEWORK_GLUE,
    IMPORT_WIRING,
    LOW_VALUE_LOGGING,
    METRICS_ONLY,
    MUTATION_TOOL_UNSUPPORTED,
    PURE_CONFIG_CONSTANT,
)


PYTHON_OPERATOR_POLICY_VERSION = "python-mutops-v1"
PYTHON_SUPPRESSION_POLICY_VERSION = "python-arid-v1"


@dataclass(frozen=True)
class PythonMutationOpportunityPlan:
    """Python AST binding output before mutmut candidate generation."""

    eligible: Tuple[MutationOpportunity, ...]
    selected: Tuple[MutationOpportunity, ...]
    omitted_by_one_per_line: Tuple[MutationOpportunity, ...]
    suppressed: Tuple[SuppressedMutationOpportunity, ...]


def collect_python_mutation_opportunities(
    source_file: Path,
    *,
    source_path: str,
    changed_lines: Iterable[int],
    covered_lines: Optional[Iterable[int]] = None,
) -> PythonMutationOpportunityPlan:
    """Collect deterministic pre-generation mutation opportunities for Python."""

    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    _attach_parent_links(tree)
    changed = {int(line) for line in changed_lines or [] if int(line) > 0}
    covered = {int(line) for line in covered_lines or [] if int(line) > 0}
    line_text = _source_lines(source)
    symbol_by_line = _symbol_by_line(tree)
    suppressions = _suppressed_line_reasons(tree)

    eligible: List[MutationOpportunity] = []
    suppressed: List[SuppressedMutationOpportunity] = []
    for line in sorted(changed):
        operators = _operator_hints_for_line(tree, line, line_text.get(line, ""))
        if line in suppressions:
            if not operators:
                operators = (("statement", "other", 20),)
            opportunity = _opportunity(
                source_path=source_path,
                line=line,
                symbol=symbol_by_line.get(line, "<module>"),
                operator_name=operators[0][0],
                family_hint=operators[0][1],
                operator_priority=operators[0][2],
                source_line=line_text.get(line, ""),
                covered=line in covered,
                executable=True,
                selection_reason="suppressed changed line",
            )
            suppressed.append(
                SuppressedMutationOpportunity(
                    opportunity=opportunity,
                    reason_code=suppressions[line],
                    reason=f"Suppressed by Python AST binding: {suppressions[line]}",
                )
            )
            continue
        if not operators:
            continue
        for operator_name, family_hint, priority in operators:
            eligible.append(
                _opportunity(
                    source_path=source_path,
                    line=line,
                    symbol=symbol_by_line.get(line, "<module>"),
                    operator_name=operator_name,
                    family_hint=family_hint,
                    operator_priority=priority,
                    source_line=line_text.get(line, ""),
                    covered=line in covered,
                    executable=True,
                    selection_reason="covered changed line" if line in covered else "changed line",
                )
            )
    selected, omitted = select_one_opportunity_per_line(eligible)
    return PythonMutationOpportunityPlan(
        eligible=tuple(sorted(eligible, key=lambda item: item.selection_rank)),
        selected=tuple(selected),
        omitted_by_one_per_line=tuple(omitted),
        suppressed=tuple(sorted(suppressed, key=lambda item: item.opportunity.selection_rank)),
    )


def build_mutmut3_candidate_plan_from_meta(
    repo: Path,
    *,
    source_path: str,
    target_id: str,
    changed_lines: Mapping[str, Iterable[int]],
    covered_lines: Iterable[int],
    selected_test_paths: Sequence[str],
    mutmut_version: str,
    runtime_fingerprint: str,
    dependency_fingerprint: str,
    config_fingerprint_value: str,
    mutmut_internal_api_fingerprint: str,
    repo_url: str = "",
    base_ref: str = "",
    base_commit: str = "",
    head_commit: str = "",
    generation_policy: Optional[Mapping[str, Any]] = None,
) -> MutationCandidatePlan:
    """Build a Python3 mutmut exact-key candidate plan from generated metadata."""

    normalized_source = _normalize_relpath(source_path)
    changed = {
        int(line)
        for line in (changed_lines or {}).get(normalized_source, ())
        if int(line) > 0
    }
    opportunity_plan = _opportunity_plan_from_generation_policy(generation_policy)
    if opportunity_plan is None:
        opportunity_plan = collect_python_mutation_opportunities(
            Path(repo) / normalized_source,
            source_path=normalized_source,
            changed_lines=changed,
            covered_lines=covered_lines,
        )
    meta = _read_mutmut_meta(Path(repo), normalized_source)
    exit_code_by_key = _exit_code_by_key(meta)
    line_by_key = _line_by_key(meta)
    operator_by_key = _operator_by_key(meta)
    opportunity_id_by_key = _opportunity_id_by_key(meta)
    module_name = _module_name(normalized_source)
    candidates: List[MutationCandidate] = []
    for opportunity in opportunity_plan.selected:
        prefixes = _mutmut_key_prefixes(module_name, opportunity.symbol)
        matching_keys = [
            str(key)
            for key in exit_code_by_key
            if any(str(key).startswith(prefix) for prefix in prefixes)
            and line_by_key.get(str(key)) == int(opportunity.line)
            and (
                not opportunity_id_by_key
                or opportunity_id_by_key.get(str(key)) == opportunity.opportunity_id
            )
            and (
                not operator_by_key
                or operator_by_key.get(str(key)) == opportunity.operator_name
            )
        ]
        for tool_key in sorted(matching_keys)[:1]:
            candidates.append(
                MutationCandidate(
                    opportunity=opportunity,
                    candidate_id=make_candidate_id(
                        opportunity_id=opportunity.opportunity_id,
                        tool_candidate_key=str(tool_key),
                        mutation_tool_api_fingerprint=mutmut_internal_api_fingerprint,
                    ),
                    tool_candidate_key=str(tool_key),
                    score=opportunity.roi_score,
                )
            )
    context = MutationVerificationContext(
        repo_url=repo_url,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        target_id=target_id,
        source_path=normalized_source,
        selected_test_paths=tuple(str(path) for path in selected_test_paths),
        runtime_fingerprint=runtime_fingerprint,
        dependency_fingerprint=dependency_fingerprint,
        mutation_tool_version=str(mutmut_version or "").strip(),
        selected_test_policy_version="strict-python-v1",
        operator_policy_version=PYTHON_OPERATOR_POLICY_VERSION,
        suppression_policy_version=PYTHON_SUPPRESSION_POLICY_VERSION,
        mutation_tool_api_fingerprint=mutmut_internal_api_fingerprint,
        candidate_plan_config_fingerprint=config_fingerprint_value,
        policy_mode="report_full",
        enable_ci_sampling=False,
    )
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    return MutationCandidatePlan(
        language="python",
        target_id=target_id,
        source_path=normalized_source,
        policy_mode="report_full",
        candidate_plan_id=make_candidate_plan_id(
            context=context,
            selected_candidate_ids=candidate_ids,
            filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
        ),
        changed_lines=tuple(sorted(changed)),
        executable_changed_lines=tuple(sorted(changed)),
        covered_changed_lines=tuple(sorted(int(line) for line in covered_lines if int(line) in changed)),
        eligible_opportunities=opportunity_plan.eligible,
        suppressed=opportunity_plan.suppressed,
        report_full_selected=tuple(candidates),
        active_selected=tuple(candidates),
        omitted_by_one_per_line=opportunity_plan.omitted_by_one_per_line,
        suppression_by_reason=_suppression_counts(opportunity_plan.suppressed),
        sampling_layer=MutationSamplingLayer.disabled(),
        filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
        generation_policy_artifact=None,
        generation_policy_fingerprint=PYTHON_OPERATOR_POLICY_VERSION,
        exact_tool_candidate_keys=tuple(candidate.tool_candidate_key for candidate in candidates),
        planner_version=MUTATION_CANDIDATE_PLANNER_VERSION,
        arid_rule_version=PYTHON_SUPPRESSION_POLICY_VERSION,
        test_selection_policy_version="strict-python-v1",
        suppression_policy_version=PYTHON_SUPPRESSION_POLICY_VERSION,
        operator_policy_version=PYTHON_OPERATOR_POLICY_VERSION,
        mutation_tool_api_fingerprint=mutmut_internal_api_fingerprint,
        mutation_tool_config_fingerprint=config_fingerprint_value,
        effective_sampling_config={"enabled": False},
        runtime_fingerprint=runtime_fingerprint,
        dependency_fingerprint=dependency_fingerprint,
        repo_url=repo_url,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        selected_test_paths=tuple(str(path) for path in selected_test_paths),
        adapter_filtered_generation_applied=bool(candidates),
    )


def select_one_opportunity_per_line(
    opportunities: Iterable[MutationOpportunity],
) -> Tuple[Tuple[MutationOpportunity, ...], Tuple[MutationOpportunity, ...]]:
    """Select one deterministic representative opportunity per changed line."""

    by_line: Dict[Tuple[str, int], List[MutationOpportunity]] = {}
    for opportunity in opportunities or []:
        by_line.setdefault((opportunity.source_path, int(opportunity.line)), []).append(opportunity)
    selected: List[MutationOpportunity] = []
    omitted: List[MutationOpportunity] = []
    for key in sorted(by_line):
        ranked = sorted(
            by_line[key],
            key=lambda item: (
                not bool(item.covered),
                -int(item.operator_priority),
                str(item.symbol),
                str(item.operator_name),
                str(item.opportunity_id),
            ),
        )
        selected.append(ranked[0])
        omitted.extend(sorted(ranked[1:], key=lambda item: item.opportunity_id))
    return tuple(selected), tuple(omitted)


def low_value_side_effect_lines(source_file: Path) -> Dict[int, str]:
    """Return low-value side-effect statement lines mapped to engine reasons."""

    try:
        source = Path(source_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return {}
    return {
        line: reason
        for line, reason in _suppressed_line_reasons(tree).items()
        if reason in {LOW_VALUE_LOGGING, METRICS_ONLY}
    }


def _opportunity(
    *,
    source_path: str,
    line: int,
    symbol: str,
    operator_name: str,
    family_hint: str,
    operator_priority: int,
    source_line: str,
    covered: bool,
    executable: bool,
    selection_reason: str,
) -> MutationOpportunity:
    diff_hunk = f"@@ {line} @@\n{source_line.strip()}"
    opportunity_id = make_opportunity_id(
        language="python",
        source_path=source_path,
        line=line,
        symbol=symbol,
        operator_name=operator_name,
        source_line=source_line,
        diff_hunk=diff_hunk,
    )
    return MutationOpportunity(
        language="python",
        source_path=source_path,
        line=int(line),
        line_span=(int(line), int(line)),
        symbol=symbol,
        opportunity_id=opportunity_id,
        operator_name=operator_name,
        family_hint=family_hint,
        diff_hunk=diff_hunk,
        operator_priority=int(operator_priority),
        roi_score=float(operator_priority),
        selection_rank=(
            0 if covered else 1,
            -int(operator_priority),
            source_path,
            int(line),
            symbol,
            operator_name,
            opportunity_id,
        ),
        covered=bool(covered),
        executable=bool(executable),
        selection_reason=selection_reason,
    )


def _source_lines(source: str) -> Dict[int, str]:
    return {index: line for index, line in enumerate(source.splitlines(), start=1)}


def _read_mutmut_meta(repo: Path, source_path: str) -> Dict[str, Any]:
    meta_path = Path(repo) / "mutants" / f"{_normalize_relpath(source_path)}.meta"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(meta, dict):
        return {}
    sidecar_path = meta_path.with_suffix(meta_path.suffix + ".uta.json")
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sidecar = {}
    if isinstance(sidecar, dict):
        for key, value in sidecar.items():
            meta.setdefault(str(key), value)
    return meta


def _opportunity_plan_from_generation_policy(
    generation_policy: Optional[Mapping[str, Any]],
) -> Optional[PythonMutationOpportunityPlan]:
    if not isinstance(generation_policy, Mapping):
        return None
    selected = tuple(
        item
        for item in (_opportunity_from_payload(payload) for payload in generation_policy.get("selected") or [])
        if item is not None
    )
    if not selected:
        return None
    selected_before_cap = tuple(
        item
        for item in (
            _opportunity_from_payload(payload)
            for payload in generation_policy.get("selectedBeforeCap") or generation_policy.get("selected") or []
        )
        if item is not None
    )
    omitted = tuple(
        item
        for item in (
            _opportunity_from_payload(payload)
            for payload in generation_policy.get("omittedByOnePerLineOpportunities") or []
        )
        if item is not None
    )
    suppressed = tuple(
        item
        for item in (
            _suppressed_from_payload(payload)
            for payload in generation_policy.get("suppressed") or []
        )
        if item is not None
    )
    return PythonMutationOpportunityPlan(
        eligible=tuple(sorted(selected_before_cap or selected, key=lambda item: item.selection_rank)),
        selected=tuple(sorted(selected, key=lambda item: item.selection_rank)),
        omitted_by_one_per_line=tuple(sorted(omitted, key=lambda item: item.selection_rank)),
        suppressed=tuple(sorted(suppressed, key=lambda item: item.opportunity.selection_rank)),
    )


def _opportunity_from_payload(payload: Any) -> Optional[MutationOpportunity]:
    if not isinstance(payload, Mapping):
        return None
    try:
        line = int(payload.get("line") or 0)
        line_span = payload.get("lineSpan") if isinstance(payload.get("lineSpan"), list) else [line, line]
        return MutationOpportunity(
            language=str(payload.get("language") or "python"),
            source_path=str(payload.get("sourcePath") or ""),
            line=line,
            line_span=(int(line_span[0]), int(line_span[-1])),
            symbol=str(payload.get("symbol") or "<module>"),
            opportunity_id=str(payload.get("opportunityId") or ""),
            operator_name=str(payload.get("operatorName") or "statement"),
            family_hint=str(payload.get("familyHint") or "other"),
            diff_hunk=str(payload.get("diffHunk") or ""),
            operator_priority=int(payload.get("operatorPriority") or 10),
            roi_score=float(payload.get("roiScore") or payload.get("operatorPriority") or 10.0),
            selection_rank=tuple(payload.get("selectionRank") or (0, line)),
            covered=bool(payload.get("covered")),
            executable=bool(payload.get("executable", True)),
            selection_reason=str(payload.get("selectionReason") or "generation policy"),
        )
    except (TypeError, ValueError, IndexError):
        return None


def _suppressed_from_payload(payload: Any) -> Optional[SuppressedMutationOpportunity]:
    if not isinstance(payload, Mapping):
        return None
    opportunity = _opportunity_from_payload(payload.get("opportunity"))
    if opportunity is None:
        return None
    return SuppressedMutationOpportunity(
        opportunity=opportunity,
        reason_code=str(payload.get("reasonCode") or ""),
        reason=str(payload.get("reason") or ""),
    )


def _exit_code_by_key(meta: Mapping[str, Any]) -> Dict[str, object]:
    exit_code_by_key = meta.get("exit_code_by_key")
    if not isinstance(exit_code_by_key, dict):
        return {}
    return {str(key): value for key, value in exit_code_by_key.items()}


def _line_by_key(meta: Mapping[str, Any]) -> Dict[str, int]:
    raw = meta.get("line_by_key")
    if isinstance(raw, dict):
        return {
            str(key): int(value)
            for key, value in raw.items()
            if _is_positive_int(value)
        }
    return {}


def _operator_by_key(meta: Mapping[str, Any]) -> Dict[str, str]:
    raw = meta.get("operator_by_key") or meta.get("policy_operator_by_key")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(value or "").strip()}


def _opportunity_id_by_key(meta: Mapping[str, Any]) -> Dict[str, str]:
    raw = meta.get("opportunity_id_by_key")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(value or "").strip()}


def _is_positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _module_name(source_path: str) -> str:
    value = _normalize_relpath(source_path)
    if value.endswith(".py"):
        value = value[:-3]
    module = ".".join(part for part in value.split("/") if part and part != "__init__")
    if module.startswith("src."):
        return module[len("src.") :]
    return module


def _mutmut_key_prefixes(module_name: str, symbol: str) -> Tuple[str, ...]:
    if not symbol or symbol == "<module>":
        return (f"{module_name}.x_",)
    return (
        f"{module_name}.x_{symbol}__mutmut_",
        f"{module_name}.xǁ{symbol}__mutmut_",
    )


def _suppression_counts(items: Sequence[SuppressedMutationOpportunity]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        counts[item.reason_code] = counts.get(item.reason_code, 0) + 1
    return counts


def _normalize_relpath(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")


def _attach_parent_links(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "_uta_parent", parent)


def _symbol_by_line(tree: ast.AST) -> Dict[int, str]:
    symbols: Dict[int, str] = {}

    class SymbolVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: List[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._record(node, str(node.name))
            self.stack.append(str(node.name))
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_callable(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_callable(node)

        def _visit_callable(self, node: ast.AST) -> None:
            name = str(getattr(node, "name", "<module>"))
            qualified = "ǁ".join([*self.stack, name]) if self.stack else name
            self._record(node, qualified)
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

        def _record(self, node: ast.AST, symbol: str) -> None:
            start = int(getattr(node, "lineno", 0) or 0)
            end = int(getattr(node, "end_lineno", start) or start)
            if start <= 0:
                return
            for line in range(start, end + 1):
                symbols[line] = symbol

    SymbolVisitor().visit(tree)
    return symbols


def _suppressed_line_reasons(tree: ast.AST) -> Dict[int, str]:
    reasons: Dict[int, str] = {}
    for node in ast.walk(tree):
        reason = _suppression_reason_for_node(node)
        if not reason:
            continue
        start = int(getattr(node, "lineno", 0) or 0)
        end = int(getattr(node, "end_lineno", start) or start)
        for line in range(start, end + 1):
            reasons.setdefault(line, reason)
    return reasons


def _suppression_reason_for_node(node: ast.AST) -> str:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return IMPORT_WIRING
    if _nested_callable(node):
        return MUTATION_TOOL_UNSUPPORTED
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
        return GENERATED_FRAMEWORK_GLUE
    if isinstance(node, ast.Return) and _empty_return(node):
        return MUTATION_TOOL_UNSUPPORTED
    if isinstance(node, (ast.If, ast.While)) and _low_yield_guard(node.test):
        return MUTATION_TOOL_UNSUPPORTED
    if isinstance(node, ast.Pass):
        return MUTATION_TOOL_UNSUPPORTED
    if isinstance(node, ast.Assign) and _framework_glue_assign(node):
        return GENERATED_FRAMEWORK_GLUE
    if isinstance(node, ast.Assign) and _logging_assign(node):
        return LOW_VALUE_LOGGING
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and _module_state_or_config_assign(node):
        return PURE_CONFIG_CONSTANT
    if isinstance(node, ast.Assign) and _module_constant_assign(node):
        return PURE_CONFIG_CONSTANT
    # mutmut issue #104 documents the same failure mode we reproduced:
    # function default-argument mutants can survive mutmut's trampoline runner
    # even when applying the mutant to source makes the selected tests fail.
    # Do not generate these candidates; they are a mutation-tool limitation,
    # not useful unit-test repair work.
    if isinstance(node, ast.Constant) and _inside_function_default(node):
        return MUTATION_TOOL_UNSUPPORTED
    if isinstance(node, ast.Await) and _awaits_perpetual_scheduler_sleep(node):
        return ASYNC_SCHEDULER_LOOP
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and _module_init_call(node):
        return PURE_CONFIG_CONSTANT
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        return _framework_registration_reason(node.value.func) or _side_effect_reason(node.value.func)
    if _mutmut3_candidate_shape(node) and not _inside_mutmut3_materializable_callable(node):
        return MUTATION_TOOL_UNSUPPORTED
    return ""


def _nested_callable(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    parent = getattr(node, "_uta_parent", None)
    return isinstance(parent, ast.AST) and not isinstance(parent, (ast.Module, ast.ClassDef))


def _empty_return(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not value.elts:
        return True
    if isinstance(value, ast.Dict) and not value.keys:
        return True
    if isinstance(value, ast.Call) and not value.args and not value.keywords:
        names = [name.lower() for name in _call_name_parts(value.func)]
        return bool(names) and names[0] in {"dict", "list", "set", "tuple"}
    return False


def _low_yield_guard(test: ast.AST) -> bool:
    if _is_isinstance_call(test):
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _low_yield_guard(test.operand)
    if isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
        return True
    return False


def _is_isinstance_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"


def _inside_function_default(node: ast.AST) -> bool:
    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.arguments):
            return any(current is default for default in [*parent.defaults, *parent.kw_defaults] if default is not None)
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


def _module_constant_assign(node: ast.Assign) -> bool:
    if not all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets):
        return False
    if isinstance(node.value, (ast.Constant, ast.Str, ast.Num, ast.NameConstant, ast.Tuple, ast.List, ast.Dict, ast.Set)):
        return True
    if isinstance(node.value, ast.Call):
        names = [name.lower() for name in _call_name_parts(node.value.func)]
        return bool(names) and names[0] in {"frozenset", "set", "tuple", "list", "dict"}
    return False


def _awaits_perpetual_scheduler_sleep(node: ast.Await) -> bool:
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    if not _is_scheduler_sleep_call(call):
        return False
    parent = getattr(node, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.While) and _is_literal_true(parent.test):
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        parent = getattr(parent, "_uta_parent", None)
    return False


def _is_scheduler_sleep_call(node: ast.Call) -> bool:
    names = [name.lower() for name in _call_name_parts(node.func)]
    if not names:
        return False
    dotted = ".".join(reversed(names))
    return dotted in {"asyncio.sleep", "sleep"} or dotted.endswith(".asyncio.sleep")


def _is_literal_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _module_init_call(node: ast.Expr) -> bool:
    parent = getattr(node, "_uta_parent", None)
    if not isinstance(parent, ast.Module):
        return False
    names = [name.lower() for name in _call_name_parts(node.value.func)]
    if not names:
        return False
    name = names[0]
    return name.startswith("_init") or name.startswith("init_")


def _framework_glue_assign(node: ast.Assign) -> bool:
    target_names = {_target_name(target).lower() for target in node.targets}
    if any(name in {"urlpatterns", "app_name"} for name in target_names):
        return True
    if not isinstance(node.value, ast.Call):
        return False
    call_names = [name.lower() for name in _call_name_parts(node.value.func)]
    constructor = call_names[0].strip("_") if call_names else ""
    if constructor not in {"apirouter", "blueprint", "celery", "fastapi", "flask"}:
        return False
    return any(name in {"api", "app", "application", "blueprint", "bp", "celery_app", "router"} for name in target_names)


def _logging_assign(node: ast.Assign) -> bool:
    target_names = {_target_name(target).lower() for target in node.targets}
    if not any("log" in name or "logger" in name for name in target_names):
        return False
    if not isinstance(node.value, ast.Call):
        return False
    call_names = [name.lower() for name in _call_name_parts(node.value.func)]
    joined = ".".join(reversed(call_names))
    return "logging.getlogger" in joined or "logger" in joined


def _module_state_or_config_assign(node: ast.AST) -> bool:
    if not isinstance(getattr(node, "_uta_parent", None), ast.Module):
        return False
    names = _assignment_target_names(node)
    if not names:
        return False
    return all(name.isupper() or name.startswith("_") or name == "__all__" for name in names)


def _assignment_target_names(node: ast.AST) -> List[str]:
    if isinstance(node, ast.Assign):
        return [_target_name(target) for target in node.targets if _target_name(target)]
    target = _target_name(node.target)
    return [target] if target else []


def _framework_registration_reason(func: ast.AST) -> str:
    names = [name.lower() for name in _call_name_parts(func)]
    if not names:
        return ""
    method = names[0].strip("_")
    if method not in {
        "add_api_route",
        "add_event_handler",
        "add_url_rule",
        "connect",
        "include_router",
        "register",
        "register_blueprint",
    }:
        return ""
    receiver = names[1].strip("_") if len(names) > 1 else ""
    if receiver in {"admin", "api", "app", "application", "blueprint", "bp", "router", "signal", "signals"}:
        return GENERATED_FRAMEWORK_GLUE
    return ""


def _target_name(target: ast.AST) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _operator_hints_for_line(tree: ast.AST, line: int, text: str) -> Tuple[Tuple[str, str, int], ...]:
    hints: List[Tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if int(getattr(node, "lineno", 0) or 0) != int(line):
            continue
        if not _inside_mutmut3_materializable_callable(node):
            continue
        if isinstance(node, ast.Compare):
            hints.append(("comparison_boundary", "boundary", 100))
            hints.append(("comparison_negation", "conditional", 100))
        elif isinstance(node, (ast.If, ast.While, ast.UnaryOp)) and _truthiness_condition_node(node):
            hints.append(("boolean_operator", "conditional", 100))
        elif isinstance(node, ast.BoolOp):
            hints.append(("boolean_operator", "conditional", 100))
        elif isinstance(node, ast.BinOp):
            if _inside_annotation(node):
                continue
            hints.append(("math_operator", "math", 90))
        elif isinstance(node, ast.Call):
            if _inside_decorator(node) or _inside_raise(node):
                continue
            if _no_argument_call_without_value_mutation(node):
                continue
            hints.append(("call_argument", "call", 90))
        elif isinstance(node, ast.Constant):
            if (
                _inside_annotation(node)
                or _inside_docstring(node)
                or _inside_decorator(node)
                or _inside_raise(node)
                or _inside_formatted_string(node)
            ):
                continue
            hints.append(("constant_value", "literal", 80))
    if not hints and _statement_mutation_node_on_line(tree, line):
        hints.append(("statement", "other", 20))
    dedup: Dict[str, Tuple[str, str, int]] = {}
    for hint in hints:
        dedup.setdefault(hint[0], hint)
    return tuple(dedup.values())


def _truthiness_condition_node(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, ast.Not)
    test = getattr(node, "test", None)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    return isinstance(test, (ast.Name, ast.Attribute, ast.Subscript, ast.Call))


def _statement_mutation_node_on_line(tree: ast.AST, line: int) -> bool:
    return any(
        int(getattr(node, "lineno", 0) or 0) == int(line)
        and _inside_mutmut3_materializable_callable(node)
        and isinstance(
            node,
            (
                ast.Assign,
                ast.AugAssign,
                ast.AnnAssign,
                ast.Break,
                ast.Continue,
                ast.Pass,
                ast.Assert,
                ast.Delete,
            ),
        )
        and not _inside_class_field_definition(node)
        for node in ast.walk(tree)
    )


def _mutmut3_candidate_shape(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, (ast.If, ast.While, ast.UnaryOp)) and _truthiness_condition_node(node):
        return True
    if isinstance(node, ast.BoolOp):
        return True
    if isinstance(node, ast.BinOp) and not _inside_annotation(node):
        return True
    if isinstance(node, ast.Call):
        return not (
            _inside_decorator(node)
            or _inside_raise(node)
            or _no_argument_call_without_value_mutation(node)
        )
    if isinstance(node, ast.Constant):
        return not (
            _inside_annotation(node)
            or _inside_docstring(node)
            or _inside_decorator(node)
            or _inside_raise(node)
            or _inside_formatted_string(node)
        )
    if isinstance(
        node,
        (
            ast.Assign,
            ast.AugAssign,
            ast.AnnAssign,
            ast.Break,
            ast.Continue,
            ast.Pass,
            ast.Assert,
            ast.Delete,
        ),
    ):
        return not _inside_class_field_definition(node)
    return False


def _inside_mutmut3_materializable_callable(node: ast.AST) -> bool:
    """Return whether mutmut 3 can emit an exact executable key for this node.

    mutmut 3 names and executes mutants through trampolines generated only for
    top-level functions and direct class methods. Class-body statements,
    module-level calls, and nested function bodies are not materialized into
    runnable mutant keys by the adapter.
    """

    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = getattr(parent, "_uta_parent", None)
            return isinstance(owner, (ast.Module, ast.ClassDef))
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


def _inside_decorator(node: ast.AST) -> bool:
    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and current in parent.decorator_list:
            return True
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


def _inside_raise(node: ast.AST) -> bool:
    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.Raise):
            return True
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


def _no_argument_call_without_value_mutation(node: ast.Call) -> bool:
    if node.args or node.keywords:
        return False
    parent = getattr(node, "_uta_parent", None)
    if isinstance(parent, ast.Expr) and parent.value is node:
        return True
    if isinstance(parent, ast.For) and parent.iter is node:
        return True
    return False


def _inside_class_field_definition(node: ast.AST) -> bool:
    if isinstance(node, ast.AnnAssign):
        return isinstance(getattr(node, "_uta_parent", None), ast.ClassDef)
    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.AnnAssign):
            owner = getattr(parent, "_uta_parent", None)
            return isinstance(owner, ast.ClassDef)
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


def _inside_docstring(node: ast.AST) -> bool:
    parent = getattr(node, "_uta_parent", None)
    if not isinstance(parent, ast.Expr) or parent.value is not node:
        return False
    owner = getattr(parent, "_uta_parent", None)
    return isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and bool(owner.body) and owner.body[0] is parent


def _inside_formatted_string(node: ast.AST) -> bool:
    """Exclude f-string literal fragments that mutmut 3 does not materialize."""

    parent = getattr(node, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.JoinedStr):
            return True
        parent = getattr(parent, "_uta_parent", None)
    return False


def _inside_annotation(node: ast.AST) -> bool:
    current = node
    parent = getattr(current, "_uta_parent", None)
    while isinstance(parent, ast.AST):
        if isinstance(parent, ast.AnnAssign) and parent.annotation is current:
            return True
        if isinstance(parent, ast.arg) and parent.annotation is current:
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and parent.returns is current:
            return True
        current = parent
        parent = getattr(current, "_uta_parent", None)
    return False


_LOW_VALUE_SIDE_EFFECT_TOKENS = {
    "audit",
    "counter",
    "histogram",
    "log",
    "logger",
    "logging",
    "meter",
    "metric",
    "metrics",
    "span",
    "stats",
    "statsd",
    "telemetry",
    "timer",
    "trace",
    "tracer",
}

_LOW_VALUE_SIDE_EFFECT_METHODS = {
    "add_event",
    "count",
    "critical",
    "debug",
    "decr",
    "decrement",
    "emit",
    "emit_audit",
    "emit_metric",
    "error",
    "exception",
    "gauge",
    "histogram",
    "incr",
    "increment",
    "info",
    "log",
    "mark",
    "meter",
    "notice",
    "observe",
    "record",
    "record_event",
    "record_metric",
    "set_tag",
    "tag",
    "timer",
    "trace",
    "warning",
    "warn",
}


def _side_effect_reason(func: ast.AST) -> str:
    names = _call_name_parts(func)
    if not names:
        return ""
    lowered = [name.lower() for name in names]
    method = lowered[0].strip("_")
    if method not in _LOW_VALUE_SIDE_EFFECT_METHODS:
        return ""
    if not any(any(token in name for token in _LOW_VALUE_SIDE_EFFECT_TOKENS) for name in lowered):
        return ""
    joined = ".".join(reversed(lowered))
    if any(token in joined for token in ("metric", "metrics", "stats", "statsd", "counter", "histogram", "gauge", "timer")):
        return METRICS_ONLY
    return LOW_VALUE_LOGGING


def _call_name_parts(func: ast.AST) -> List[str]:
    names: List[str] = []
    current = func
    while isinstance(current, ast.Attribute):
        names.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        names.append(current.id)
    return names
