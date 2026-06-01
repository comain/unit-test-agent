"""Coverage ROI scorer: method-level effort scoring and class-level ROI ranking.

Computes how expensive each method is to unit-test, then ranks methods by
coverage ROI (uncovered lines / effort). The output is used to guide LLM
test generation toward cheap methods first.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.language.java.parse.models import CodeGraph, GraphNode
from uta.language.java.parse import graph_builder as java_parse_graph_builder
from uta.language.java.parse import java_parser as java_parse_java_parser

logger = logging.getLogger("uta.language.java.scoring")
ROI_SCORER_VERSION = "2026-04-23-v3"

# Callee names that signal non-deterministic behavior
_NONDET_SIGNALS = frozenset({
    "currentTimeMillis", "nanoTime", "random", "nextInt", "nextLong",
    "randomUUID", "UUID", "sleep", "submit", "execute", "invokeAll",
    "await", "get", "join", "schedule", "scheduleAtFixedRate",
})

# Receiver/callee patterns indicating async/thread pool usage
_ASYNC_TYPES = frozenset({
    "CompletableFuture", "CountDownLatch", "ExecutorService",
    "ThreadPoolExecutor", "ScheduledExecutorService", "Future",
})

# Field type suffixes that indicate domain boundaries
_STORAGE_SUFFIXES = ("Mapper", "Dao", "Repository", "JdbcTemplate")
_REMOTE_SUFFIXES = ("Client", "Remote", "Rpc", "Feign", "Stub", "RestTemplate")
_MESSAGING_SUFFIXES = ("Producer", "Sender", "Publisher", "Template")

# Annotations that indicate injected dependencies
_INJECT_ANNOTATIONS = frozenset({"Autowired", "Resource", "Inject", "Value"})


def _control_flow_score(complexity: Optional[Dict[str, Any]]) -> int:
    if not complexity:
        return 0
    cc = complexity.get("cyclomatic_approx", 1)
    if cc <= 3:
        return 0
    if cc <= 7:
        return 1
    if cc <= 12:
        return 2
    return 3


def _get_method_calls(method_fqn: str, graph: CodeGraph) -> List[str]:
    """Return callee FQNs for outgoing CALLS edges from this method."""
    return [e.target for e in graph.edges
            if e.source == method_fqn and e.relation == "CALLS"]


def _get_collaborator_calls(method_fqn: str, class_fqn: str, graph: CodeGraph) -> List[str]:
    """Return calls to methods on OTHER classes (not self/parent)."""
    all_calls = _get_method_calls(method_fqn, graph)
    return [c for c in all_calls if not c.startswith(class_fqn + ".")]


def _get_class_field_types(class_fqn: str, graph: CodeGraph) -> Dict[str, str]:
    """Return {field_name: field_type} for fields of the class."""
    fields = {}
    for fqn, node in graph.nodes.items():
        if node.kind == "field" and node.metadata.get("parent_fqn") == class_fqn:
            field_name = fqn.split(".")[-1]
            field_type = node.metadata.get("field_type") or ""
            fields[field_name] = field_type
    return fields


def _get_injected_field_count(class_fqn: str, graph: CodeGraph) -> int:
    """Count fields with injection annotations."""
    count = 0
    for fqn, node in graph.nodes.items():
        if node.kind == "field" and node.metadata.get("parent_fqn") == class_fqn:
            annotations = set(node.metadata.get("annotations", []))
            if annotations & _INJECT_ANNOTATIONS:
                count += 1
    return count


def _crosses_domain_boundary(collaborator_calls: List[str], field_types: Dict[str, str]) -> bool:
    """Check if calls cross multiple domain boundaries (storage, remote, messaging)."""
    boundaries = set()
    all_type_names = set(field_types.values())
    for call_fqn in collaborator_calls:
        # Check the class part of the call FQN
        call_class = ".".join(call_fqn.split(".")[:-1])
        class_simple = call_class.split(".")[-1] if call_class else ""
        for t in list(all_type_names) + [class_simple]:
            if any(t.endswith(s) for s in _STORAGE_SUFFIXES):
                boundaries.add("storage")
            elif any(t.endswith(s) for s in _REMOTE_SUFFIXES):
                boundaries.add("remote")
            elif any(t.endswith(s) for s in _MESSAGING_SUFFIXES):
                boundaries.add("messaging")
    return len(boundaries) > 1


def _dependency_score(
    collaborator_calls: List[str],
    field_types: Dict[str, str],
    *,
    collab_count_override: Optional[int] = None,
) -> int:
    n = collab_count_override if collab_count_override is not None else len(collaborator_calls)
    if n <= 1:
        score = 0
    elif n <= 3:
        score = 1
    elif n <= 5:
        score = 2
    else:
        score = 3
    if _crosses_domain_boundary(collaborator_calls, field_types):
        score += 1
    return min(score, 4)


def _nondet_score(method_fqn: str, graph: CodeGraph) -> int:
    """Detect non-deterministic behavior from call names."""
    calls = _get_method_calls(method_fqn, graph)
    signals = 0
    for call_fqn in calls:
        callee_name = call_fqn.split(".")[-1]
        callee_class = call_fqn.split(".")[-2] if "." in call_fqn else ""
        if callee_name in _NONDET_SIGNALS:
            signals += 1
        if callee_class in _ASYNC_TYPES:
            signals += 1
    return min(signals, 4)


def _nondet_score_from_complexity(
    complexity: Optional[Dict[str, Any]],
    method_fqn: str,
    graph: CodeGraph,
) -> int:
    """Detect non-determinism from both raw receiver names and resolved graph edges."""
    signals = 0
    # Check receiver names from AST complexity data
    if complexity:
        for rname in complexity.get("receiver_names", []):
            # Check if the receiver type name looks async/non-deterministic
            if rname in ("executor", "executorService", "threadPool", "scheduler",
                         "future", "completableFuture", "countDownLatch", "latch"):
                signals += 2
    # Also check resolved graph edges
    signals += _nondet_score(method_fqn, graph)
    return min(signals, 4)


def _setup_score(
    param_count: int,
    collaborator_count: int,
    injected_field_count: int,
) -> int:
    if param_count <= 2 and collaborator_count <= 1:
        score = 0
    elif param_count <= 4 and collaborator_count <= 3:
        score = 1
    else:
        score = 2
    if injected_field_count > 6:
        score += 1
    return min(score, 3)


def _purity_bonus(collaborator_count: int, cyclomatic: int) -> int:
    if collaborator_count == 0 and cyclomatic <= 3:
        return -2
    if collaborator_count <= 1 and cyclomatic <= 5:
        return -1
    return 0


def _effort_band(score: int) -> str:
    if score <= 2:
        return "cheap"
    if score <= 5:
        return "medium"
    return "expensive"


def compute_method_effort(
    method_fqn: str,
    graph: CodeGraph,
    class_fqn: str,
    *,
    injected_field_count: int = 0,
    field_types: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compute effort score for a single method."""
    node = graph.nodes.get(method_fqn)
    if not node:
        return {"effort_score": 0, "effort_band": "cheap", "effort_reasons": []}

    complexity = node.metadata.get("complexity")
    cc = (complexity or {}).get("cyclomatic_approx", 1)
    body_lines = (complexity or {}).get("body_lines", 0)

    # Count collaborator calls: only calls on class fields (injected dependencies),
    # not standard library calls like list.add(), builder.build(), etc.
    ft = field_types or {}
    field_names = set(ft.keys())
    receiver_names = (complexity or {}).get("receiver_names", [])
    # Count how many receivers match a class field
    field_call_receivers = [r for r in receiver_names if r in field_names]
    collab_count_from_ast = len(field_call_receivers)

    # Also check resolved graph edges
    resolved_collab = _get_collaborator_calls(method_fqn, class_fqn, graph)
    collab_count = max(collab_count_from_ast, len(resolved_collab))

    # Build list for boundary detection
    collab_for_boundary = list(resolved_collab)
    for rname in field_call_receivers:
        rtype = ft.get(rname, "")
        if rtype:
            collab_for_boundary.append(f"_.{rtype}.call")

    cf_score = _control_flow_score(complexity)
    dep_score = _dependency_score(collab_for_boundary, ft, collab_count_override=collab_count)
    nd_score = _nondet_score_from_complexity(complexity, method_fqn, graph)
    param_count = len(node.metadata.get("params", []))
    su_score = _setup_score(param_count, collab_count, injected_field_count)
    pu_bonus = _purity_bonus(collab_count, cc)

    total = max(0, cf_score + dep_score + nd_score + su_score + pu_bonus)
    band = _effort_band(total)

    reasons = []
    if cf_score > 0:
        reasons.append(f"cyclomatic={cc}")
    if dep_score > 0:
        reasons.append(f"{collab_count}-collaborators")
    if nd_score > 0:
        reasons.append("non-deterministic")
    if su_score > 0:
        reasons.append(f"setup(params={param_count})")
    if pu_bonus < 0:
        reasons.append("pure-calc")
    if not reasons:
        reasons.append("simple")

    return {
        "name": method_fqn.split(".")[-1],
        "fqn": method_fqn,
        "signature": _build_signature(node),
        "start_line": node.line,
        "body_lines": body_lines,
        "effort_score": total,
        "effort_band": band,
        "effort_reasons": reasons,
        "detail": {
            "control_flow": cf_score,
            "dependency": dep_score,
            "non_determinism": nd_score,
            "setup": su_score,
            "purity_bonus": pu_bonus,
        },
    }


def _build_signature(node) -> str:
    params = node.metadata.get("params", [])
    param_str = ", ".join(f"{p[0]} {p[1]}" for p in params)
    ret = node.metadata.get("return_type") or "void"
    name = node.fqn.split(".")[-1]
    return f"{ret} {name}({param_str})"


def _visibility_rank(node: Optional[GraphNode]) -> int:
    modifiers = list((node.metadata if node else {}).get("modifiers", []))
    if "public" in modifiers:
        return 0
    if "protected" in modifiers:
        return 1
    return 2


def _same_class_reach_lines(
    method_fqn: str,
    class_fqn: str,
    methods_by_fqn: Dict[str, Dict[str, Any]],
    graph: CodeGraph,
    _memo: Optional[Dict[str, int]] = None,
    _stack: Optional[set] = None,
) -> int:
    """Estimate uncovered reach exposed by this method through same-class calls."""
    memo = _memo if _memo is not None else {}
    stack = _stack if _stack is not None else set()
    if method_fqn in memo:
        return memo[method_fqn]
    if method_fqn in stack:
        return int((methods_by_fqn.get(method_fqn) or {}).get("missed_lines", 0) or 0)

    stack.add(method_fqn)
    visited = set()

    def dfs(cur: str):
        if cur in visited:
            return
        visited.add(cur)
        for callee in _get_method_calls(cur, graph):
            if callee.startswith(class_fqn + ".") and callee in methods_by_fqn:
                dfs(callee)

    dfs(method_fqn)
    stack.remove(method_fqn)

    reach = sum(int((methods_by_fqn.get(fqn) or {}).get("missed_lines", 0) or 0) for fqn in visited)
    memo[method_fqn] = reach
    return reach


def _same_class_public_caller_count(
    method_fqn: str,
    class_fqn: str,
    methods_by_fqn: Dict[str, Dict[str, Any]],
    graph: CodeGraph,
) -> int:
    count = 0
    for edge in graph.edges:
        if edge.relation != "CALLS" or edge.target != method_fqn:
            continue
        caller = methods_by_fqn.get(edge.source)
        if not caller:
            continue
        if edge.source.startswith(class_fqn + ".") and int(caller.get("visibility_rank", 2)) == 0:
            count += 1
    return count


def _wrapper_style_penalty(method: Dict[str, Any]) -> float:
    """Penalize thin wrapper-family entry points, not rich business methods.

    Batch/delegate methods often expose a large same-class reach by forwarding
    into heavier methods. They are useful, but they should not outrank the
    primary business/state transitions that actually carry the gate.
    """
    callers = int(method.get("same_class_public_callers", 0) or 0)
    if callers <= 0:
        return 1.0

    detail = method.get("detail") or {}
    name = str(method.get("name") or "").lower()
    direct_lines = int(method.get("missed_lines", 0) or 0)
    total_reach = int(method.get("planning_reach_lines", direct_lines) or 0)
    helper_bonus = max(0, total_reach - direct_lines)

    low_branch = (
        int(detail.get("control_flow", 0) or 0) <= 1
        and int(detail.get("dependency", 0) or 0) <= 1
        and int(detail.get("non_determinism", 0) or 0) == 0
    )
    primary_business = (
        direct_lines >= 70
        or (
            direct_lines >= 50
            and (
                int(detail.get("dependency", 0) or 0) >= 2
                or int(detail.get("non_determinism", 0) or 0) >= 1
            )
        )
    )
    thin_direct = direct_lines <= 30
    helper_heavy = helper_bonus >= max(20, direct_lines)
    batchish = name.startswith("batch") or "batch" in name
    queryish = name.startswith("query") and direct_lines <= 35 and helper_bonus > direct_lines

    penalty = 1.0
    if not primary_business:
        penalty += 0.35 * callers
    if helper_heavy and (thin_direct or low_branch):
        penalty += 0.45 * callers
    if batchish:
        penalty += 0.35
    if queryish:
        penalty += 0.15
    return penalty


def _planning_priority(method: Dict[str, Any]) -> float:
    """Score planning value with direct branch/state yield first.

    Direct uncovered lines and branch complexity dominate. Same-class helper
    reach is only a secondary bonus so thin wrappers do not crowd out richer
    stateful entry points during planning.
    """
    detail = method.get("detail") or {}
    direct_lines = int(method.get("missed_lines", 0) or 0)
    total_reach = int(method.get("planning_reach_lines", direct_lines) or 0)
    helper_bonus = max(0, total_reach - direct_lines)

    branch_multiplier = (
        1.0
        + 0.8 * float(detail.get("control_flow", 0) or 0)
        + 0.3 * float(detail.get("dependency", 0) or 0)
        + 0.2 * float(detail.get("non_determinism", 0) or 0)
    )
    raw = direct_lines * branch_multiplier + helper_bonus * 0.15
    effort_penalty = 1.0 + 0.25 * float(method.get("effort_score", 0) or 0)
    wrapper_penalty = _wrapper_style_penalty(method)
    return round(raw / (effort_penalty * wrapper_penalty), 1)


def compute_class_roi(
    class_fqn: str,
    graph: CodeGraph,
    *,
    jacoco_xml_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute ROI scores for all public methods in a class.

    If jacoco_xml_path is provided, missed lines/branches come from JaCoCo.
    Otherwise, missed_lines defaults to body_lines (assume fully uncovered).
    """
    # Collect public methods
    methods = []
    for fqn, node in graph.nodes.items():
        if node.kind != "method" or node.metadata.get("parent_fqn") != class_fqn:
            continue
        modifiers = node.metadata.get("modifiers", [])
        if "private" in modifiers:
            continue
        methods.append(fqn)

    field_types = _get_class_field_types(class_fqn, graph)
    injected_count = _get_injected_field_count(class_fqn, graph)

    # Load JaCoCo per-method coverage if available
    jacoco_methods = {}
    if jacoco_xml_path and os.path.exists(jacoco_xml_path):
        jacoco_methods = _parse_jacoco_method_coverage(jacoco_xml_path, class_fqn)

    results = []
    for method_fqn in methods:
        node = graph.nodes.get(method_fqn)
        effort = compute_method_effort(
            method_fqn, graph, class_fqn,
            injected_field_count=injected_count,
            field_types=field_types,
        )
        method_name = method_fqn.split(".")[-1]

        # Get coverage data
        jc = jacoco_methods.get(method_name, {})
        missed_lines = jc.get("missed_line", effort["body_lines"])
        missed_branches = jc.get("missed_branch", 0)

        effort["missed_lines"] = missed_lines
        effort["missed_branches"] = missed_branches
        effort["roi_score"] = round(missed_lines / max(effort["effort_score"], 1), 1)
        effort["has_coverage_gain"] = missed_lines > 0
        effort["visibility_rank"] = _visibility_rank(node)
        results.append(effort)

    methods_by_fqn = {m["fqn"]: m for m in results}
    reach_memo: Dict[str, int] = {}
    for method in results:
        method["planning_reach_lines"] = _same_class_reach_lines(
            method["fqn"], class_fqn, methods_by_fqn, graph, reach_memo
        )
        method["same_class_public_callers"] = _same_class_public_caller_count(
            method["fqn"], class_fqn, methods_by_fqn, graph
        )
        method["planning_score"] = _planning_priority(method)

    # Sort by gate value first: methods that still cover uncovered lines always
    # come before zero-gain wrappers; public entry points come before internal
    # helpers, then by same-class reach exposed through that entry path.
    results.sort(
        key=lambda m: (
            -int(bool(m["has_coverage_gain"])),
            m["visibility_rank"],
            -int(bool(m["visibility_rank"] == 0)),
            -m["planning_score"],
            -m["missed_lines"],
            -m["planning_reach_lines"],
            -m["roi_score"],
            m["effort_score"],
            m["name"],
        )
    )

    cheap = [m for m in results if m["effort_band"] == "cheap"]
    medium = [m for m in results if m["effort_band"] == "medium"]
    expensive = [m for m in results if m["effort_band"] == "expensive"]
    cheap_gain = [m for m in cheap if m["has_coverage_gain"]]
    medium_gain = [m for m in medium if m["has_coverage_gain"]]
    expensive_gain = [m for m in expensive if m["has_coverage_gain"]]
    zero_gain = [m for m in results if not m["has_coverage_gain"]]

    return {
        "class_fqn": class_fqn,
        "methods": results,
        "provenance": {
            "roi_scorer_version": ROI_SCORER_VERSION,
            "jacoco_xml_path": jacoco_xml_path or "",
            "jacoco_used": bool(jacoco_methods),
        },
        "summary": {
            "total_methods": len(results),
            "cheap_count": len(cheap),
            "medium_count": len(medium),
            "expensive_count": len(expensive),
            "cheap_gain_count": len(cheap_gain),
            "medium_gain_count": len(medium_gain),
            "expensive_gain_count": len(expensive_gain),
            "zero_gain_count": len(zero_gain),
            "estimated_cheap_coverage_lines": sum(m["missed_lines"] for m in cheap),
            "estimated_medium_coverage_lines": sum(m["missed_lines"] for m in medium),
            "estimated_expensive_coverage_lines": sum(m["missed_lines"] for m in expensive),
        },
    }


def is_degenerate_roi_data(roi_data: Dict[str, Any]) -> bool:
    methods = list(roi_data.get("methods") or [])
    if not methods:
        return True

    missed_lines = [int(m.get("missed_lines", 0) or 0) for m in methods]
    roi_scores = [float(m.get("roi_score", 0.0) or 0.0) for m in methods]
    effort_scores = [int(m.get("effort_score", 0) or 0) for m in methods]
    summary = roi_data.get("summary") or {}

    if all(v == 0 for v in missed_lines):
        return True
    if (
        int(summary.get("estimated_cheap_coverage_lines", 0) or 0) == 0
        and int(summary.get("estimated_medium_coverage_lines", 0) or 0) == 0
    ):
        return True
    if len(set(roi_scores)) == 1 and roi_scores[0] == 0.0:
        return True
    if len(set(effort_scores)) == 1 and effort_scores[0] == 0 and all(v == 0 for v in missed_lines):
        return True
    return False


def is_degenerate_roi_markdown(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    cheap = medium = None
    for line in text.splitlines():
        if line.startswith("- Cheap (effort 0-2):"):
            cheap = line
        elif line.startswith("- Medium (effort 3-5):"):
            medium = line
    if cheap and medium and "~0 uncovered lines" in cheap and "~0 uncovered lines" in medium:
        return True
    method_rows = [line for line in text.splitlines() if line.startswith("| ") and "`" in line]
    if method_rows and all("| 0 | 0.0 |" in row for row in method_rows):
        return True
    return False


def _parse_jacoco_method_coverage(xml_path: str, class_fqn: str) -> Dict[str, Dict[str, int]]:
    """Extract per-method missed line/branch counts from JaCoCo XML."""
    import xml.etree.ElementTree as ET

    if not os.path.exists(xml_path):
        return {}

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return {}

    root = tree.getroot()
    package_name = ".".join(class_fqn.split(".")[:-1]).replace(".", "/")
    simple_name = class_fqn.split(".")[-1]
    target_class_name = f"{package_name}/{simple_name}"

    for package in root.findall("package"):
        if package.get("name") != package_name:
            continue
        for cls in package.findall("class"):
            if cls.get("name") != target_class_name:
                continue
            methods = {}
            for method in cls.findall("method"):
                name = method.get("name", "unknown")
                missed_line = 0
                missed_branch = 0
                for counter in method.findall("counter"):
                    ctype = counter.get("type")
                    if ctype == "LINE":
                        missed_line = int(counter.get("missed", "0") or 0)
                    elif ctype == "BRANCH":
                        missed_branch = int(counter.get("missed", "0") or 0)
                methods[name] = {"missed_line": missed_line, "missed_branch": missed_branch}
            return methods
    return {}


def format_roi_markdown(roi_data: Dict[str, Any], *, debug: bool = False) -> str:
    """Format ROI data as a markdown file for LLM consumption."""
    class_fqn = roi_data["class_fqn"]
    simple_name = class_fqn.split(".")[-1]
    summary = roi_data["summary"]
    methods = roi_data["methods"]

    lines = [
        f"# ROI Scores: {simple_name}",
        "",
        "## Summary",
        f"- Total public methods: {summary['total_methods']}",
        f"- Cheap (effort 0-2): {summary.get('cheap_gain_count', summary['cheap_count'])} methods with uncovered reach, ~{summary['estimated_cheap_coverage_lines']} uncovered lines",
        f"- Medium (effort 3-5): {summary.get('medium_gain_count', summary['medium_count'])} methods with uncovered reach, ~{summary['estimated_medium_coverage_lines']} uncovered lines",
        f"- Expensive (effort 6+): {summary.get('expensive_gain_count', summary['expensive_count'])} methods with uncovered reach, ~{summary.get('estimated_expensive_coverage_lines', 0)} uncovered lines",
        f"- Zero-gain public wrappers: {summary.get('zero_gain_count', 0)} methods, ~0 uncovered lines",
        "",
        "## Method Rankings (highest planning value first)",
        "",
        "| # | Method | Effort | Band | Missed Lines | Reach | ROI | Reasons |",
        "|---|--------|--------|------|-------------|-------|-----|---------|",
    ]

    for i, m in enumerate(methods, 1):
        reasons = ", ".join(m["effort_reasons"])
        lines.append(
            f"| {i} | `{m['name']}` | {m['effort_score']} | {m['effort_band']} "
            f"| {m['missed_lines']} | {m.get('planning_reach_lines', m['missed_lines'])} | {m['roi_score']} | {reasons} |"
        )

    # Coverage strategy guidance
    cheap_methods = [m for m in methods if m["effort_band"] == "cheap"]
    medium_methods = [m for m in methods if m["effort_band"] == "medium"]
    cheap_lines = summary["estimated_cheap_coverage_lines"]
    medium_lines = summary["estimated_medium_coverage_lines"]

    lines.extend([
        "",
        "## Coverage Strategy Guidance",
        "",
    ])

    if cheap_methods:
        cheap_gain_methods = sorted(
            [m for m in cheap_methods if m["missed_lines"] > 0 and m.get("visibility_rank", 2) == 0],
            key=lambda m: (-m.get("planning_score", 0.0), -m.get("planning_reach_lines", 0), m["name"]),
        )[:10]
        cheap_names = ", ".join(f"`{m['name']}`" for m in cheap_gain_methods)
        if cheap_names:
            lines.append(f"1. Cover cheap methods first ({cheap_names}) for ~{cheap_lines} lines")
        else:
            lines.append("1. Cheap-band methods are mostly zero-gain wrappers here; do not count them toward the coverage gate")
    if medium_methods:
        medium_gain_methods = sorted(
            [m for m in medium_methods if m["missed_lines"] > 0 and m.get("visibility_rank", 2) == 0],
            key=lambda m: (-m.get("planning_score", 0.0), -m.get("planning_reach_lines", 0), m["name"]),
        )[:10]
        medium_names = ", ".join(f"`{m['name']}`" for m in medium_gain_methods)
        if medium_names:
            lines.append(f"2. If gate not met, continue to medium-band methods ({medium_names}) for ~{medium_lines} more lines")
        else:
            lines.append("2. Medium-band methods also have little remaining uncovered reach; use them only if public entry paths are exhausted")

    lines.extend([
        "3. Do not count zero-gain public wrappers, empty delegates, or already-covered methods toward gate planning",
        "4. Expensive methods should only be attempted if the gate cannot be met otherwise",
        "5. If all remaining uncovered methods are expensive, the current coverage may be the realistic maximum — consider lowering the gate",
    ])

    if debug:
        provenance = roi_data.get("provenance") or {}
        lines.extend([
            "",
            "## Provenance",
            "",
            f"- scorer version: `{provenance.get('roi_scorer_version', ROI_SCORER_VERSION)}`",
            f"- jacoco used: `{bool(provenance.get('jacoco_used'))}`",
            f"- jacoco xml path: `{provenance.get('jacoco_xml_path', '')}`",
        ])

    return "\n".join(lines) + "\n"


def compute_roi_cache_key(
    source_path: str,
    jacoco_xml_path: Optional[str] = None,
    *,
    debug: bool = False,
) -> str:
    """Compute a cache key from source file mtime (and optionally JaCoCo mtime)."""
    parts = []
    parts.append(f"roi_scorer_version:{ROI_SCORER_VERSION}")
    try:
        parts.append(f"roi_scorer_file:{Path(__file__).resolve()}:{os.path.getmtime(__file__)}")
    except OSError:
        pass
    for mod in (java_parse_java_parser, java_parse_graph_builder):
        mod_path = getattr(mod, "__file__", "")
        if mod_path and os.path.exists(mod_path):
            parts.append(f"{Path(mod_path).name}:{os.path.getmtime(mod_path)}")
    parts.append(f"debug:{int(debug)}")
    if os.path.exists(source_path):
        parts.append(f"{source_path}:{os.path.getmtime(source_path)}")
    if jacoco_xml_path and os.path.exists(jacoco_xml_path):
        parts.append(f"{jacoco_xml_path}:{os.path.getmtime(jacoco_xml_path)}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]
