import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

from uta.config import settings

_PITEST_IPV6_LOCALHOST_ARGS = "-Djava.net.preferIPv6Addresses=true -Djava.net.preferIPv4Stack=false"


def find_latest_pitest_report(repo_path: str, module: Optional[str] = None) -> Optional[str]:
    """Find the latest Pitest mutations.xml in timestamped report dirs.

    Pitest generates reports in target/pit-reports/YYYYMMDDHHMI/mutations.xml.
    """
    base = Path(repo_path) / (module if module else "") / "target" / "pit-reports"
    if not base.exists():
        return None

    # Find all mutations.xml files, pick the newest by directory name (timestamp)
    report_dirs = sorted(
        [d for d in base.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for d in report_dirs:
        mutations_file = d / "mutations.xml"
        if mutations_file.exists():
            return str(mutations_file)
    return None


def run_pitest(repo_path: str, class_fqn: str, test_class_fqn: str,
               module: Optional[str] = None, timeout: int = 600) -> Tuple[bool, str]:
    """Run Pitest mutation testing via Maven command line (no pom.xml changes needed).

    Uses the pitest-maven plugin with full coordinates so it works even if the
    plugin is not declared in the project's pom.xml.
    """
    cmd = [
        settings.maven_bin,
        "org.pitest:pitest-maven:1.15.3:mutationCoverage",
        f"-DtargetClasses={class_fqn}",
        f"-DtargetTests={test_class_fqn}",
        "-DoutputFormats=XML",
        "-DtimestampedReports=true",
        "-DfailWhenNoMutations=false",
        "-DskipTests=false",
        "-Dmaven.test.skip=false",
        f"-DjvmArgs={_PITEST_IPV6_LOCALHOST_ARGS}",
    ]
    if module:
        cmd.extend(["-pl", module, "-am"])

    env = os.environ.copy()
    existing_maven_opts = env.get("MAVEN_OPTS", "").strip()
    env["MAVEN_OPTS"] = (
        f"{existing_maven_opts} {_PITEST_IPV6_LOCALHOST_ARGS}".strip()
        if existing_maven_opts
        else _PITEST_IPV6_LOCALHOST_ARGS
    )

    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, timeout=timeout, env=env,
        )
        output = result.stdout.decode(errors="replace")
        stderr = result.stderr.decode(errors="replace")
        if result.returncode != 0:
            combined = f"{stderr}\n{output}"
            green_suite_failure = parse_pitest_green_suite_failure(combined)
            if green_suite_failure:
                return False, format_pitest_green_suite_failure(green_suite_failure)
            return False, f"Pitest failed:\n{stderr[-2000:]}\n{output[-2000:]}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Pitest timed out"


def parse_pitest_green_suite_failure(output: str) -> Optional[Dict[str, Any]]:
    """Detect PIT pre-scan failures caused by tests failing before mutation starts."""
    text = output or ""
    if "Mutation testing requires a green suite" not in text and "did not pass without mutation" not in text:
        return None

    method_match = re.search(r"Description \[testClass=([^,\]]+), name=([^,\]]+)", text)
    junit_match = re.search(r"^\s*([A-Za-z0-9_$]+)\(([A-Za-z0-9_$.]+)\)", text, re.MULTILINE)
    summary_match = re.search(
        r"(\d+)\s+tests did not pass without mutation when calculating line coverage",
        text,
    )

    test_class = None
    test_method = None
    if method_match:
        test_class = method_match.group(1).strip()
        test_method = method_match.group(2).strip()
    elif junit_match:
        test_method = junit_match.group(1).strip()
        test_class = junit_match.group(2).strip()

    summary_lines: List[str] = []
    interesting_markers = (
        "did not pass without mutation",
        "Mutation testing requires a green suite",
        "Description [testClass=",
        "AssertionError",
        "ComparisonFailure",
        "expected:<",
        " but was:<",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(marker in line for marker in interesting_markers):
            summary_lines.append(line)
        if len(summary_lines) >= 8:
            break

    return {
        "failing_test_count": int(summary_match.group(1)) if summary_match else 0,
        "test_class": test_class,
        "test_method": test_method,
        "summary_lines": summary_lines,
    }


def format_pitest_green_suite_failure(details: Dict[str, Any]) -> str:
    """Render a concise, mutation-stage friendly summary for PIT green-suite failures."""
    count = int(details.get("failing_test_count", 0) or 0)
    test_class = details.get("test_class")
    test_method = details.get("test_method")
    summary_lines = list(details.get("summary_lines") or [])

    lines = ["Pitest precheck failed: mutation testing requires a green suite."]
    if count:
        lines.append(f"- failing tests before mutation: {count}")
    if test_class or test_method:
        target = ".".join(part for part in [test_class, test_method] if part)
        lines.append(f"- failing test: {target}")
    if summary_lines:
        lines.append("")
        lines.append("Relevant PIT output:")
        lines.extend(f"- {line}" for line in summary_lines[:6])
    return "\n".join(lines)


def parse_pitest_report(xml_path: str, class_fqn: str) -> List[Dict[str, Any]]:
    """Parse Pitest XML and return surviving mutants for a class."""
    if not os.path.exists(xml_path):
        return []

    tree = ET.parse(xml_path)
    root = tree.getroot()

    survivors = []
    for mutation in root.findall("mutation"):
        if mutation.get("status") == "SURVIVED":
            mutated_class = mutation.find("mutatedClass")
            if mutated_class is not None and mutated_class.text == class_fqn:
                mutated_method = mutation.find("mutatedMethod")
                survivors.append({
                    "line": int(mutation.find("lineNumber").text),
                    "method": mutated_method.text if mutated_method is not None else "",
                    "mutation_type": mutation.find("mutator").text,
                    "detail": mutation.find("description").text if mutation.find("description") is not None else "",
                })
    return survivors


def _mutation_family(mutator: str, detail: str) -> str:
    lowered_mutator = (mutator or "").lower()
    lowered_detail = (detail or "").lower()
    if "boundary" in lowered_mutator:
        return "boundary"
    if "negate" in lowered_mutator or "conditional" in lowered_mutator:
        return "conditional"
    if "return" in lowered_mutator:
        return "return_value"
    if "voidmethodcall" in lowered_mutator or "removed call" in lowered_detail:
        return "side_effect"
    if "math" in lowered_mutator or "increment" in lowered_mutator:
        return "math"
    if "invert" in lowered_mutator:
        return "negation"
    return "other"


def _is_metric_side_effect(method_name: str, detail: str) -> bool:
    lowered_method = (method_name or "").lower()
    lowered_detail = (detail or "").lower()
    metric_tokens = ("metrics", "counter(", ".counter(", ".inc(", "increment counter", "meter", "timer")
    return any(token in lowered_method or token in lowered_detail for token in metric_tokens)


def _killability_score(method_name: str, family: str, detail: str) -> Tuple[int, str]:
    lowered_method = (method_name or "").lower()
    lowered_detail = (detail or "").lower()

    if _is_metric_side_effect(method_name, detail):
        return (0, "low")

    if family in {"boundary", "conditional", "return_value"}:
        return (3, "high")

    if family == "side_effect":
        if any(token in lowered_method for token in ("query", "build", "filter", "exist", "can", "open", "revert")):
            return (2, "medium")
        if "removed call" in lowered_detail:
            return (1, "medium")
        return (1, "low")

    if family in {"math", "negation"}:
        return (1, "medium")

    return (1, "low")


def summarize_surviving_mutants(
    xml_path: str,
    class_fqn: str,
    *,
    max_families: int = 5,
    max_examples_per_family: int = 3,
    method_efforts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Cluster surviving mutants into ranked method families for focused repair.

    When ``method_efforts`` is supplied (from Java ``coverage_roi.compute_class_roi``),
    families are scored by ROI = (count × killability) / effort and sorted by
    that ROI; otherwise the original count/killability ordering is used.
    """
    survivors = parse_pitest_report(xml_path, class_fqn)
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for survivor in survivors:
        family = _mutation_family(survivor.get("mutation_type", ""), survivor.get("detail", ""))
        method = survivor.get("method", "") or "(unknown)"
        key = (method, family, survivor.get("mutation_type", ""))
        grouped[key].append(survivor)

    ranked: List[Dict[str, Any]] = []
    for (method, family, mutator), items in grouped.items():
        ordered = sorted(items, key=lambda item: (int(item.get("line", 0) or 0), item.get("detail", "")))
        lines = sorted({int(item.get("line", 0) or 0) for item in ordered})
        killability_score, killability = _killability_score(method, family, ordered[0].get("detail", "") if ordered else "")
        metric_only = bool(ordered) and all(_is_metric_side_effect(method, item.get("detail", "")) for item in ordered)
        ranked.append(
            {
                "method": method,
                "family": family,
                "mutator": mutator,
                "count": len(ordered),
                "lines": lines,
                "killability_score": killability_score,
                "killability": killability,
                "deprioritized": metric_only,
                "examples": ordered[:max_examples_per_family],
            }
        )

    if method_efforts is not None:
        from uta.language.java.scoring.mutation_roi import score_families, roi_sort_key
        score_families(ranked, method_efforts)
        ranked.sort(key=roi_sort_key)
    else:
        ranked.sort(
            key=lambda item: (
                bool(item.get("deprioritized", False)),
                -int(item.get("count", 0) or 0),
                -int(item.get("killability_score", 0) or 0),
                item.get("method", ""),
                item["lines"][0] if item["lines"] else 0,
            )
        )
    return ranked[:max_families]


def format_mutation_families_markdown(families: List[Dict[str, Any]]) -> str:
    if not families:
        return "- No surviving mutant families were parsed.\n"

    has_roi = any("roi" in f for f in families)
    lines: List[str] = []
    if has_roi:
        lines.append("_Ranked by kill-per-effort ROI — tackle the top entry first._")
        lines.append("")
    for family in families:
        family_name = family.get("family", "other")
        method = family.get("method", "(unknown)")
        mutator = family.get("mutator", "")
        count = int(family.get("count", 0) or 0)
        killability = family.get("killability", "low")
        deprioritized = bool(family.get("deprioritized", False))
        likely_equivalent = bool(family.get("likely_equivalent", False))
        line_list = ", ".join(str(line) for line in family.get("lines", [])[:8]) or "unknown"
        if likely_equivalent:
            priority_note = "likely_equivalent — skip, not worth a test"
        elif deprioritized:
            priority_note = "deprioritize unless there is an easy seam"
        else:
            priority_note = "target early"
        roi_suffix = ""
        if has_roi:
            roi_suffix = (
                f", effort {family.get('effort_score', '?')} ({family.get('effort_band', '?')}),"
                f" roi {family.get('roi', 0.0)}"
            )
        lines.append(
            f"- method `{method}` — `{family_name}` via `{mutator}` — {count} survivor(s), "
            f"killability `{killability}`{roi_suffix}, {priority_note}, line(s): {line_list}"
        )
        for example in family.get("examples", []):
            detail = example.get("detail", "")
            suffix = f" — {detail}" if detail else ""
            lines.append(
                f"  - line {int(example.get('line', 0) or 0)} in `{example.get('method', '') or method}`: "
                f"`{example.get('mutation_type', '')}`{suffix}"
            )
    return "\n".join(lines) + "\n"


def compute_mutation_stats(xml_path: str, class_fqn: str) -> Dict[str, Any]:
    """Compute mutation statistics from Pitest XML.

    Returns dict with score (%), totals by status, and a compact summary.
    """
    if not os.path.exists(xml_path):
        return {
            "score": 0.0,
            "killed": 0,
            "survived": 0,
            "total": 0,
            "status_counts": {},
        }

    tree = ET.parse(xml_path)
    root = tree.getroot()

    killed = 0
    survived = 0
    total = 0
    status_counts: Dict[str, int] = {}
    for mutation in root.findall("mutation"):
        mutated_class = mutation.find("mutatedClass")
        if mutated_class is not None and mutated_class.text == class_fqn:
            total += 1
            status = mutation.get("status") or "UNKNOWN"
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "KILLED":
                killed += 1
            elif status == "SURVIVED":
                survived += 1

    score = (killed / total * 100.0) if total > 0 else 100.0
    return {
        "score": score,
        "killed": killed,
        "survived": survived,
        "total": total,
        "status_counts": status_counts,
        "no_coverage": status_counts.get("NO_COVERAGE", 0),
        "timed_out": status_counts.get("TIMED_OUT", 0),
        "non_viable": status_counts.get("NON_VIABLE", 0),
        "memory_error": status_counts.get("MEMORY_ERROR", 0),
        "run_error": status_counts.get("RUN_ERROR", 0),
    }


def compute_mutation_score(xml_path: str, class_fqn: str) -> float:
    """Compute mutation score (% of mutants killed) from Pitest XML."""
    return compute_mutation_stats(xml_path, class_fqn)["score"]
