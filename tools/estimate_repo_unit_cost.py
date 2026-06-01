#!/usr/bin/env python3
"""Estimate repo-wide UTA cost from selectable Java classes.

The workflow is intentionally two-step:

1. ``scan`` records every selectable class, line/complexity metrics, and three
   calibration classes at p50, p95, and max complexity.
2. ``estimate`` combines the scan inventory with completed benchmark reports
   for those calibration classes to project token, cost, and elapsed time.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from uta.language.java.parse.graph_builder import GraphBuilder
from uta.language.java.parse.java_parser import JavaParser
from uta.graph.nodes import (
    _is_accessor_like_method,
    _is_testable_class,
)

OPENAI_GPT54_STANDARD_PRICING = {
    "model": "gpt-5.4",
    "mode": "standard",
    "usd_per_1m_input_tokens": 2.50,
    "usd_per_1m_cached_input_tokens": 0.25,
    "usd_per_1m_output_tokens": 15.00,
    "source": "https://openai.com/api/pricing/",
}


@dataclass
class ClassMetric:
    class_fqn: str
    module: str
    source_path: str
    source_lines: int
    nonblank_source_lines: int
    non_private_method_count: int
    behavior_method_count: int
    method_body_lines: int
    jacoco_line_count: int
    coverage_line_basis: str
    estimated_behavior_required_lines: int
    required_coverage_lines: int
    cyclomatic_sum: int
    cyclomatic_max: int
    branch_nodes: int
    loop_nodes: int
    catch_nodes: int
    switch_cases: int
    external_calls: int
    complexity_score: float
    complexity_bucket: str = ""


@dataclass
class CalibrationRun:
    class_fqn: str
    report_path: str
    status: str
    coverage: Optional[float]
    mutation: Optional[float]
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    non_cache_tokens: int
    required_coverage_lines: int
    method_body_lines: int
    complexity_score: float
    complexity_bucket: str
    estimate_bucket: str
    non_cache_tokens_per_required_line: float
    total_tokens_per_required_line: float
    input_tokens_per_required_line: float
    cache_read_tokens_per_required_line: float
    elapsed_seconds_per_required_line: float
    cache_hit_ratio: float
    non_cache_tokens_per_file: int
    total_tokens_per_file: int
    elapsed_seconds_per_file: float


def _repo_slug(repo: Path) -> str:
    return repo.name.replace(" ", "-").lower()


def _normalize_modules(values: Sequence[str]) -> List[str]:
    modules: List[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                modules.append(part)
    return modules


def _java_files(repo: Path, modules: Sequence[str]) -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    if modules:
        for module in modules:
            root = repo / module
            files.extend((module, path) for path in sorted(root.glob("**/src/main/java/**/*.java")))
    else:
        for path in sorted(repo.glob("**/src/main/java/**/*.java")):
            module = _module_from_path(repo, path)
            files.append((module, path))
    return [(module, path) for module, path in files if path.is_file()]


def _module_from_path(repo: Path, path: Path) -> str:
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if parts else ""


def _read_source_counts(path: Path) -> Tuple[int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    nonblank = 0
    in_block_comment = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        nonblank += 1
    return len(lines), nonblank


def _percentile(sorted_values: Sequence[ClassMetric], pct: float) -> ClassMetric:
    if not sorted_values:
        raise ValueError("no class metrics available")
    idx = round((len(sorted_values) - 1) * pct)
    return sorted_values[max(0, min(len(sorted_values) - 1, idx))]


def _assign_buckets(metrics: List[ClassMetric]) -> None:
    if not metrics:
        return
    ordered = sorted(metrics, key=lambda item: item.complexity_score)
    low_cut = _percentile(ordered, 1 / 3).complexity_score
    high_cut = _percentile(ordered, 2 / 3).complexity_score
    for item in metrics:
        if item.complexity_score <= low_cut:
            item.complexity_bucket = "simple"
        elif item.complexity_score <= high_cut:
            item.complexity_bucket = "medium"
        else:
            item.complexity_bucket = "complex"


def _representatives(metrics: List[ClassMetric]) -> List[Tuple[str, float, ClassMetric]]:
    if not metrics:
        return []
    ordered = sorted(metrics, key=lambda item: item.complexity_score)
    # Unit-test generation cost is usually skewed by the complex tail, so use
    # median, high-tail, and worst-case classes rather than low/medium/high
    # equal-count buckets.
    targets = [("p50", 0.50), ("p95", 0.95), ("max", 1.0)]
    selected: List[Tuple[str, float, ClassMetric]] = []
    seen: set[str] = set()
    for tier, pct in targets:
        candidate = _percentile(ordered, pct)
        if candidate.class_fqn in seen:
            target_idx = round((len(ordered) - 1) * pct)
            alternatives = [
                item for _, item in sorted(
                    ((abs(idx - target_idx), item) for idx, item in enumerate(ordered)),
                    key=lambda pair: pair[0],
                )
                if item.class_fqn not in seen
            ]
            if alternatives:
                candidate = alternatives[0]
        if candidate.class_fqn not in seen:
            selected.append((tier, pct, candidate))
            seen.add(candidate.class_fqn)
    return selected


def _discover_jacoco_xmls(repo: Path, modules: Sequence[str], explicit_paths: Sequence[str]) -> List[Path]:
    if explicit_paths:
        return [Path(path).expanduser().resolve() for path in explicit_paths]
    candidates: List[Path] = []
    if modules:
        for module in modules:
            candidates.append(repo / module / "target" / "site" / "jacoco" / "jacoco.xml")
    else:
        candidates.extend(repo.glob("*/target/site/jacoco/jacoco.xml"))
        candidates.append(repo / "target" / "site" / "jacoco" / "jacoco.xml")
    return [path for path in candidates if path.exists()]


def _load_jacoco_line_counts(xml_paths: Sequence[Path]) -> Tuple[Dict[str, int], Dict[str, Any]]:
    line_counts: Dict[str, int] = {}
    loaded: List[str] = []
    missing: List[str] = []
    for path in xml_paths:
        if not path.exists():
            missing.append(str(path))
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            missing.append(str(path))
            continue
        loaded.append(str(path))
        for package in root.findall("package"):
            for cls in package.findall("class"):
                raw_name = cls.get("name") or ""
                if not raw_name:
                    continue
                class_fqn = raw_name.replace("/", ".")
                for counter in cls.findall("counter"):
                    if counter.get("type") != "LINE":
                        continue
                    missed = int(counter.get("missed", "0") or 0)
                    covered = int(counter.get("covered", "0") or 0)
                    line_counts[class_fqn] = missed + covered
                    break
    return line_counts, {
        "jacoco_xml_loaded": loaded,
        "jacoco_xml_missing_or_invalid": missing,
        "jacoco_class_line_counts": len(line_counts),
    }


def _parse_metrics(
    repo: Path,
    modules: Sequence[str],
    coverage_gate: int,
    *,
    jacoco_line_counts: Optional[Dict[str, int]] = None,
) -> Tuple[List[ClassMetric], Dict[str, Any]]:
    parser = JavaParser()
    files = _java_files(repo, modules)
    results = []
    module_by_path: Dict[str, str] = {}
    for module, path in files:
        module_by_path[str(path.resolve())] = module
        results.append(parser.parse_file(str(path)))

    graph = GraphBuilder().build(results)
    metrics: List[ClassMetric] = []
    for fqn, node in sorted(graph.nodes.items()):
        if node.kind != "class" or not _is_testable_class(fqn, graph):
            continue
        path = Path(node.file_path)
        source_lines, nonblank_source_lines = _read_source_counts(path)
        methods = [
            method
            for method in graph.nodes.values()
            if method.kind == "method"
            and method.metadata.get("parent_fqn") == fqn
            and "private" not in (method.metadata.get("modifiers") or [])
        ]
        behavior_methods = [
            method
            for method in methods
            if not _is_accessor_like_method(method)
        ]
        method_body_lines = 0
        cyclomatic_sum = 0
        cyclomatic_max = 0
        branch_nodes = 0
        loop_nodes = 0
        catch_nodes = 0
        switch_cases = 0
        external_calls = 0
        for method in behavior_methods:
            complexity = method.metadata.get("complexity") or {}
            body_lines = int(complexity.get("body_lines", 0) or 0)
            cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
            method_body_lines += body_lines
            cyclomatic_sum += cyclomatic
            cyclomatic_max = max(cyclomatic_max, cyclomatic)
            branch_nodes += int(complexity.get("branches", 0) or 0)
            loop_nodes += int(complexity.get("loops", 0) or 0)
            catch_nodes += int(complexity.get("catches", 0) or 0)
            switch_cases += int(complexity.get("switch_cases", 0) or 0)
            external_calls += int(complexity.get("external_calls", 0) or 0)
        estimated_behavior_required_lines = int(math.ceil(method_body_lines * coverage_gate / 100.0))
        jacoco_line_count = int((jacoco_line_counts or {}).get(fqn, 0) or 0)
        coverage_base_lines = jacoco_line_count or method_body_lines
        coverage_line_basis = "jacoco_line_counter" if jacoco_line_count else "parser_behavior_lines"
        required_coverage_lines = int(math.ceil(coverage_base_lines * coverage_gate / 100.0))
        complexity_score = (
            method_body_lines
            + 12 * max(0, cyclomatic_sum - len(behavior_methods))
            + 5 * (branch_nodes + loop_nodes + catch_nodes + switch_cases)
            + 0.5 * external_calls
        )
        rel_path = str(path.resolve().relative_to(repo.resolve()))
        metrics.append(
            ClassMetric(
                class_fqn=fqn,
                module=module_by_path.get(str(path.resolve()), _module_from_path(repo, path)),
                source_path=rel_path,
                source_lines=source_lines,
                nonblank_source_lines=nonblank_source_lines,
                non_private_method_count=len(methods),
                behavior_method_count=len(behavior_methods),
                method_body_lines=method_body_lines,
                jacoco_line_count=jacoco_line_count,
                coverage_line_basis=coverage_line_basis,
                estimated_behavior_required_lines=estimated_behavior_required_lines,
                required_coverage_lines=required_coverage_lines,
                cyclomatic_sum=cyclomatic_sum,
                cyclomatic_max=cyclomatic_max,
                branch_nodes=branch_nodes,
                loop_nodes=loop_nodes,
                catch_nodes=catch_nodes,
                switch_cases=switch_cases,
                external_calls=external_calls,
                complexity_score=round(complexity_score, 2),
            )
        )
    _assign_buckets(metrics)
    parse_summary = {
        "production_java_files": len(files),
        "parsed_graph_nodes": len(graph.nodes),
        "parsed_graph_edges": len(graph.edges),
        "coverage_line_basis": "jacoco_line_counter" if jacoco_line_counts else "parser_behavior_lines",
        "classes_with_jacoco_line_counts": sum(1 for item in metrics if item.jacoco_line_count > 0),
        "classes_using_parser_line_fallback": sum(1 for item in metrics if item.jacoco_line_count <= 0),
    }
    return metrics, parse_summary


def _summarize_metrics(metrics: Sequence[ClassMetric]) -> Dict[str, Any]:
    def total(attr: str) -> int:
        return int(sum(getattr(item, attr) for item in metrics))

    by_bucket: Dict[str, Dict[str, Any]] = {}
    for bucket in ("simple", "medium", "complex"):
        items = [item for item in metrics if item.complexity_bucket == bucket]
        by_bucket[bucket] = {
            "classes": len(items),
            "method_body_lines": sum(item.method_body_lines for item in items),
            "jacoco_line_count": sum(item.jacoco_line_count for item in items),
            "required_coverage_lines": sum(item.required_coverage_lines for item in items),
            "parser_fallback_classes": sum(1 for item in items if item.coverage_line_basis != "jacoco_line_counter"),
            "complexity_score_avg": round(statistics.mean([item.complexity_score for item in items]), 2) if items else 0,
        }
    scores = [item.complexity_score for item in metrics]
    return {
        "classes": len(metrics),
        "source_lines": total("source_lines"),
        "nonblank_source_lines": total("nonblank_source_lines"),
        "method_body_lines": total("method_body_lines"),
        "jacoco_line_count": total("jacoco_line_count"),
        "required_coverage_lines": total("required_coverage_lines"),
        "estimated_behavior_required_lines": total("estimated_behavior_required_lines"),
        "classes_with_jacoco_line_counts": sum(1 for item in metrics if item.coverage_line_basis == "jacoco_line_counter"),
        "classes_using_parser_line_fallback": sum(1 for item in metrics if item.coverage_line_basis != "jacoco_line_counter"),
        "non_private_methods": total("non_private_method_count"),
        "behavior_methods": total("behavior_method_count"),
        "cyclomatic_sum": total("cyclomatic_sum"),
        "branch_nodes": total("branch_nodes"),
        "loop_nodes": total("loop_nodes"),
        "external_calls": total("external_calls"),
        "complexity_score_min": round(min(scores), 2) if scores else 0,
        "complexity_score_p50": round(statistics.median(scores), 2) if scores else 0,
        "complexity_score_max": round(max(scores), 2) if scores else 0,
        "by_bucket": by_bucket,
    }


def _complexity_percentile_buckets(classes: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not classes:
        return {}
    ordered = sorted(classes, key=lambda item: float(item.get("complexity_score", 0) or 0))
    p50_score = float(_percentile_dict(ordered, 0.50).get("complexity_score", 0) or 0)
    p95_score = float(_percentile_dict(ordered, 0.95).get("complexity_score", 0) or 0)
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "le_p50": [],
        "p50_p95": [],
        "p95_max": [],
    }
    for item in classes:
        score = float(item.get("complexity_score", 0) or 0)
        if score <= p50_score:
            buckets["le_p50"].append(item)
        elif score <= p95_score:
            buckets["p50_p95"].append(item)
        else:
            buckets["p95_max"].append(item)

    summary: Dict[str, Dict[str, Any]] = {}
    for bucket, items in buckets.items():
        scores = [float(item.get("complexity_score", 0) or 0) for item in items]
        summary[bucket] = {
            "classes": len(items),
            "required_coverage_lines": int(sum(int(item.get("required_coverage_lines", 0) or 0) for item in items)),
            "method_body_lines": int(sum(int(item.get("method_body_lines", 0) or 0) for item in items)),
            "jacoco_line_count": int(sum(int(item.get("jacoco_line_count", 0) or 0) for item in items)),
            "complexity_score_min": round(min(scores), 2) if scores else 0,
            "complexity_score_max": round(max(scores), 2) if scores else 0,
            "complexity_score_avg": round(statistics.mean(scores), 2) if scores else 0,
        }
    return summary


def _percentile_dict(sorted_items: Sequence[Dict[str, Any]], pct: float) -> Dict[str, Any]:
    if not sorted_items:
        raise ValueError("no items available")
    idx = round((len(sorted_items) - 1) * pct)
    return sorted_items[max(0, min(len(sorted_items) - 1, idx))]


def _benchmark_command(args: argparse.Namespace, item: ClassMetric) -> str:
    parts = [
        "scripts/benmark.sh",
        "--repo",
        args.repo,
        "--module",
        item.module,
        "--class-fqn",
        item.class_fqn,
        "--coverage-gate",
        str(args.coverage_gate),
        "--mutation-gate",
        str(args.mutation_gate),
        "--provider",
        args.provider,
        "--model",
        args.model,
    ]
    if args.variant:
        parts.extend(["--variant", args.variant])
    if args.timeout_multiplier:
        parts.extend(["--timeout-multiplier", str(args.timeout_multiplier)])
    if args.trace:
        parts.append("--trace")
    else:
        parts.append("--no-trace")
    return " ".join(_shell_quote(part) for part in parts)


def _shell_quote(value: str) -> str:
    if not value or any(ch.isspace() or ch in "'\"$`" for ch in value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


def _write_scan_markdown(path: Path, payload: Dict[str, Any]) -> None:
    summary = payload["summary"]
    reps = payload["calibration_plan"]["representative_classes"]
    lines = [
        f"# Unit Test Cost Inventory: {payload['repo']}",
        "",
        "## Scope",
        "",
        f"- Repo: `{payload['repo']}`",
        f"- Modules: `{', '.join(payload['modules']) if payload['modules'] else '<all>'}`",
        f"- Coverage gate: `{payload['coverage_gate']}%`",
        f"- Mutation gate: `{payload['mutation_gate']}%`",
        f"- Generated: `{payload['generated_at']}`",
        "",
        "## Inventory Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Selectable classes | `{summary['classes']}` |",
        f"| Source lines | `{summary['source_lines']}` |",
        f"| Nonblank source lines | `{summary['nonblank_source_lines']}` |",
        f"| Behavior method body lines | `{summary['method_body_lines']}` |",
        f"| JaCoCo executable line denominator | `{summary['jacoco_line_count']}` |",
        f"| Required coverage lines | `{summary['required_coverage_lines']}` |",
        f"| Parser fallback classes | `{summary['classes_using_parser_line_fallback']}` |",
        f"| Behavior methods | `{summary['behavior_methods']}` |",
        f"| Cyclomatic sum | `{summary['cyclomatic_sum']}` |",
        f"| Branch nodes | `{summary['branch_nodes']}` |",
        f"| External calls | `{summary['external_calls']}` |",
        "",
        "## Complexity Buckets",
        "",
        "| Bucket | Classes | Body Lines | JaCoCo Lines | Required Coverage Lines | Parser Fallback Classes | Avg Score |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for bucket, row in summary["by_bucket"].items():
        lines.append(
            f"| `{bucket}` | `{row['classes']}` | `{row['method_body_lines']}` | "
            f"`{row['jacoco_line_count']}` | `{row['required_coverage_lines']}` | "
            f"`{row['parser_fallback_classes']}` | `{row['complexity_score_avg']}` |"
        )
    lines.extend(
        [
            "",
            "## Calibration Classes",
            "",
            "Run these three classes first. They represent p50, p95, and max complexity. After each run, feed the generated benchmark/UTA report into `estimate`.",
            "",
            "| Tier | Bucket | Class | Module | Required Lines | JaCoCo Lines | Body Lines | Score | Command |",
            "|---|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for rep in reps:
        lines.append(
            f"| `{rep['calibration_tier']}` | `{rep['complexity_bucket']}` | `{rep['class_fqn']}` | `{rep['module']}` | "
            f"`{rep['required_coverage_lines']}` | `{rep['jacoco_line_count']}` | `{rep['method_body_lines']}` | "
            f"`{rep['complexity_score']}` | `{rep['benchmark_command']}` |"
        )
    lines.extend(
        [
            "",
            "## Estimation Formula",
            "",
            "- `required_coverage_lines = ceil(jacoco_line_count * coverage_gate / 100)` when a JaCoCo XML line counter exists.",
            "- Classes missing from JaCoCo XML fall back to `ceil(behavior_method_body_lines * coverage_gate / 100)` and are counted as parser fallbacks.",
            "- Final `estimate` uses a two-part calibration model: fixed per-file setup/stage cost plus variable per-required-line cost.",
            "- `repo_cost = selectable_classes * fixed_per_file + required_coverage_lines * variable_per_required_line`.",
            "- Cost is only computed when pricing is supplied to `estimate`.",
            "",
            "## Top Complex Targets",
            "",
            "| Rank | Class | Module | Required Lines | JaCoCo Lines | Body Lines | Score | Path |",
            "|---:|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for idx, item in enumerate(payload["top_complex_classes"], 1):
        lines.append(
            f"| `{idx}` | `{item['class_fqn']}` | `{item['module']}` | "
            f"`{item['required_coverage_lines']}` | `{item['jacoco_line_count']}` | `{item['method_body_lines']}` | "
            f"`{item['complexity_score']}` | `{item['source_path']}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _extract_token_bucket(report: Dict[str, Any]) -> Dict[str, int]:
    phases = report.get("phase_token_usage")
    if isinstance(phases, dict):
        return _sum_phase_buckets(phases.values())
    token_usage = report.get("token_usage")
    if isinstance(token_usage, dict):
        if isinstance(token_usage.get("total_tokens"), dict):
            return _normalize_bucket(token_usage["total_tokens"])
        return _sum_phase_buckets(token_usage.values())
    return _normalize_bucket(report.get("total_tokens") or report.get("tokens") or {})


def _sum_phase_buckets(values: Iterable[Any]) -> Dict[str, int]:
    total = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    for value in values:
        if not isinstance(value, dict):
            continue
        bucket = _normalize_bucket(value)
        for key in total:
            total[key] += bucket[key]
    return total


def _normalize_bucket(value: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input": int(value.get("input", 0) or value.get("input_tokens", 0) or 0),
        "output": int(value.get("output", 0) or value.get("output_tokens", 0) or 0),
        "reasoning": int(value.get("reasoning", 0) or value.get("reasoning_tokens", 0) or 0),
        "cache_read": int(value.get("cache_read", 0) or value.get("cache_read_tokens", 0) or 0),
        "cache_write": int(value.get("cache_write", 0) or value.get("cache_write_tokens", 0) or 0),
        "total": int(value.get("total", 0) or value.get("total_tokens", 0) or 0),
    }


def _extract_elapsed(report: Dict[str, Any]) -> float:
    timing = report.get("timing") or {}
    if isinstance(timing, dict):
        for key in ("wall_clock_seconds", "total_elapsed_seconds", "elapsed_seconds"):
            if timing.get(key) is not None:
                return float(timing.get(key) or 0)
    project = report.get("project_summary") or {}
    if isinstance(project, dict) and project.get("total_elapsed_seconds") is not None:
        return float(project.get("total_elapsed_seconds") or 0)
    metrics = report.get("per_file_metrics") or []
    if isinstance(metrics, list):
        return float(sum(float(item.get("elapsed_seconds", 0) or 0) for item in metrics if isinstance(item, dict)))
    return 0.0


def _extract_outcome(report: Dict[str, Any], class_fqn: str) -> Tuple[str, Optional[float], Optional[float]]:
    outcome = report.get("outcome") or {}
    if isinstance(outcome, dict) and outcome:
        return (
            str(outcome.get("status") or "UNKNOWN"),
            _optional_float(outcome.get("coverage")),
            _optional_float(outcome.get("mutation")),
        )
    metrics = report.get("per_file_metrics") or []
    if isinstance(metrics, list):
        for item in metrics:
            if not isinstance(item, dict) or item.get("class_fqn") != class_fqn:
                continue
            return (
                str(item.get("status") or "UNKNOWN"),
                _optional_float(item.get("coverage")),
                _optional_float(item.get("mutation_score")),
            )
    results = report.get("results") or report
    if isinstance(results, dict) and isinstance(results.get(class_fqn), dict):
        item = results[class_fqn]
        return (
            str(item.get("status") or "UNKNOWN"),
            _optional_float(item.get("coverage")),
            _optional_float(item.get("mutation_score")),
        )
    return "UNKNOWN", None, None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _load_inventory(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_calibration_reports(paths: Sequence[str], inventory: Dict[str, Any]) -> List[CalibrationRun]:
    by_class = {item["class_fqn"]: item for item in inventory["classes"]}
    runs: List[CalibrationRun] = []
    for raw_path in paths:
        path = Path(raw_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        class_fqn = report.get("class_fqn")
        if not class_fqn:
            metrics = report.get("per_file_metrics") or []
            if len(metrics) == 1 and isinstance(metrics[0], dict):
                class_fqn = metrics[0].get("class_fqn")
        if not class_fqn or class_fqn not in by_class:
            raise ValueError(f"Cannot map calibration report to inventory class: {path}")
        metric = by_class[class_fqn]
        bucket = _extract_token_bucket(report)
        total = bucket["total"] or sum(bucket[key] for key in ("input", "output", "reasoning", "cache_read", "cache_write"))
        non_cache = bucket["input"] + bucket["output"] + bucket["reasoning"]
        required_lines = int(metric["required_coverage_lines"] or 0)
        if required_lines <= 0:
            raise ValueError(f"Calibration class has no required coverage lines: {class_fqn}")
        status, coverage, mutation = _extract_outcome(report, class_fqn)
        elapsed = _extract_elapsed(report)
        runs.append(
            CalibrationRun(
                class_fqn=class_fqn,
                report_path=str(path),
                status=status,
                coverage=coverage,
                mutation=mutation,
                elapsed_seconds=elapsed,
                input_tokens=bucket["input"],
                output_tokens=bucket["output"],
                reasoning_tokens=bucket["reasoning"],
                cache_read_tokens=bucket["cache_read"],
                cache_write_tokens=bucket["cache_write"],
                total_tokens=total,
                non_cache_tokens=non_cache,
                required_coverage_lines=required_lines,
                method_body_lines=int(metric["method_body_lines"]),
                complexity_score=float(metric["complexity_score"]),
                complexity_bucket=str(metric["complexity_bucket"]),
                estimate_bucket="",
                non_cache_tokens_per_required_line=non_cache / required_lines,
                total_tokens_per_required_line=total / required_lines,
                input_tokens_per_required_line=bucket["input"] / required_lines,
                cache_read_tokens_per_required_line=bucket["cache_read"] / required_lines,
                elapsed_seconds_per_required_line=elapsed / required_lines if elapsed else 0.0,
                cache_hit_ratio=_ratio(bucket["cache_read"], bucket["input"] + bucket["cache_read"]),
                non_cache_tokens_per_file=non_cache,
                total_tokens_per_file=total,
                elapsed_seconds_per_file=elapsed,
            )
        )
    return runs


def _linear_fixed_variable_model(
    runs: Sequence[CalibrationRun],
    value_attr: str,
    required_attr: str = "required_coverage_lines",
    fixed_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Fit y = fixed_per_file + variable_per_required_line * required_lines.

    Each calibration report is a separate per-file run, so the intercept is the
    setup/stage cost paid once per target class and the slope is the line-scaled
    cost. Clamp to non-negative values because negative setup or line costs are
    not meaningful for forecasting.
    """
    xs = [float(getattr(run, required_attr) or 0.0) for run in runs]
    ys = [float(getattr(run, value_attr) or 0.0) for run in runs]
    if not xs or not ys:
        return {
            "source": "empty",
            "fixed_per_file": 0.0,
            "variable_per_required_line": 0.0,
            "r2": None,
        }

    if fixed_override is not None:
        fixed = max(0.0, float(fixed_override))
        denominator = sum(xs)
        variable = max(0.0, (sum(ys) - fixed * len(ys)) / denominator) if denominator else 0.0
        return {
            "source": "fixed_override",
            "fixed_per_file": fixed,
            "variable_per_required_line": variable,
            "r2": None,
        }

    if len(xs) == 1 or len(set(xs)) == 1:
        return {
            "source": "single_point_per_line_only",
            "fixed_per_file": 0.0,
            "variable_per_required_line": sum(ys) / sum(xs) if sum(xs) else 0.0,
            "r2": None,
        }

    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    covariance_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variable = covariance_xy / variance_x if variance_x else 0.0
    fixed = mean_y - variable * mean_x

    source = "linear_regression"
    if fixed < 0 or variable < 0:
        source = "linear_regression_clamped"
    fixed = max(0.0, fixed)
    variable = max(0.0, variable)

    predictions = [fixed + variable * x for x in xs]
    ss_res = sum((y - pred) ** 2 for y, pred in zip(ys, predictions))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    return {
        "source": source,
        "fixed_per_file": fixed,
        "variable_per_required_line": variable,
        "r2": r2,
    }


def _estimate_from_two_part_model(class_count: int, required_lines: int, model: Dict[str, Any]) -> float:
    return (
        class_count * float(model.get("fixed_per_file", 0.0) or 0.0)
        + required_lines * float(model.get("variable_per_required_line", 0.0) or 0.0)
    )


def _bucket_for_complexity_score(score: float, buckets: Dict[str, Dict[str, Any]]) -> str:
    if score <= float(buckets.get("le_p50", {}).get("complexity_score_max", 0) or 0):
        return "le_p50"
    if score <= float(buckets.get("p50_p95", {}).get("complexity_score_max", 0) or 0):
        return "p50_p95"
    return "p95_max"


def _bucket_calibration_runs(
    runs: Sequence[CalibrationRun],
    buckets: Dict[str, Dict[str, Any]],
) -> Dict[str, CalibrationRun]:
    selected: Dict[str, CalibrationRun] = {}
    for bucket, summary in buckets.items():
        candidates = [run for run in runs if run.estimate_bucket == bucket]
        if candidates:
            target_score = float(summary.get("complexity_score_max", 0) or 0)
            selected[bucket] = min(candidates, key=lambda run: abs(run.complexity_score - target_score))
            continue
        target_score = float(summary.get("complexity_score_avg", 0) or 0)
        selected[bucket] = min(runs, key=lambda run: abs(run.complexity_score - target_score))
    return selected


def _bucket_specific_hybrid_model(global_model: Dict[str, Any], run: CalibrationRun, value_attr: str) -> Dict[str, Any]:
    """Use the global intercept and the bucket sample for the per-line slope.

    One calibration run per bucket cannot identify both fixed setup cost and
    variable line cost. Keep the globally fitted fixed per-file cost so target
    file count remains represented, then derive a bucket-specific slope from
    the representative run.
    """
    fixed = max(0.0, float(global_model.get("fixed_per_file", 0.0) or 0.0))
    value = max(0.0, float(getattr(run, value_attr) or 0.0))
    required_lines = max(1.0, float(run.required_coverage_lines or 0.0))
    variable = max(0.0, (value - fixed) / required_lines)
    return {
        "source": "bucket_sample_with_global_fixed_per_file",
        "calibration_class": run.class_fqn,
        "calibration_estimate_bucket": run.estimate_bucket,
        "calibration_required_lines": run.required_coverage_lines,
        "calibration_value": value,
        "fixed_per_file": fixed,
        "variable_per_required_line": variable,
        "r2": None,
    }


def _token_cost_usd(
    *,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    input_price: Optional[float],
    cached_input_price: Optional[float],
    output_price: Optional[float],
) -> Optional[float]:
    if input_price is None or cached_input_price is None or output_price is None:
        return None
    return round(
        input_tokens / 1_000_000 * input_price
        + cache_read_tokens / 1_000_000 * cached_input_price
        + output_tokens / 1_000_000 * output_price,
        4,
    )


def _estimate_payload(args: argparse.Namespace) -> Dict[str, Any]:
    inventory = _load_inventory(Path(args.inventory))
    runs = _load_calibration_reports(args.calibration_report, inventory)
    total_class_count = int(inventory["summary"]["classes"])
    total_required_lines = int(inventory["summary"]["required_coverage_lines"])
    estimate_buckets = _complexity_percentile_buckets(inventory["classes"])
    for run in runs:
        run.estimate_bucket = _bucket_for_complexity_score(run.complexity_score, estimate_buckets)
    calibration_required_lines = sum(run.required_coverage_lines for run in runs)
    weighted_non_cache_per_line = sum(run.non_cache_tokens for run in runs) / calibration_required_lines
    weighted_total_per_line = sum(run.total_tokens for run in runs) / calibration_required_lines
    weighted_input_per_line = sum(run.input_tokens for run in runs) / calibration_required_lines
    weighted_cache_read_per_line = sum(run.cache_read_tokens for run in runs) / calibration_required_lines
    weighted_elapsed_per_line = sum(run.elapsed_seconds for run in runs) / calibration_required_lines
    calibration_input_tokens = sum(run.input_tokens for run in runs)
    calibration_cache_read_tokens = sum(run.cache_read_tokens for run in runs)
    two_part_models = {
        "input_tokens": _linear_fixed_variable_model(
            runs,
            "input_tokens",
        ),
        "cache_read_tokens": _linear_fixed_variable_model(
            runs,
            "cache_read_tokens",
        ),
        "non_cache_tokens": _linear_fixed_variable_model(
            runs,
            "non_cache_tokens",
            fixed_override=args.fixed_non_cache_tokens_per_file,
        ),
        "total_tokens": _linear_fixed_variable_model(
            runs,
            "total_tokens",
            fixed_override=args.fixed_total_tokens_per_file,
        ),
        "elapsed_seconds": _linear_fixed_variable_model(
            runs,
            "elapsed_seconds",
            fixed_override=args.fixed_elapsed_seconds_per_file,
        ),
    }
    bucket_calibration_runs = _bucket_calibration_runs(runs, estimate_buckets)
    bucket_estimates: Dict[str, Dict[str, Any]] = {}
    est_non_cache = 0
    est_total = 0
    est_input = 0
    est_cache_read = 0
    est_elapsed = 0.0
    for bucket, summary in estimate_buckets.items():
        class_count = int(summary["classes"])
        required_lines = int(summary["required_coverage_lines"])
        difficulty_multiplier = args.p95_max_difficulty_multiplier if bucket == "p95_max" else 1.0
        calibration_run = bucket_calibration_runs[bucket]
        bucket_models = {
            "input_tokens": _bucket_specific_hybrid_model(
                two_part_models["input_tokens"],
                calibration_run,
                "input_tokens",
            ),
            "cache_read_tokens": _bucket_specific_hybrid_model(
                two_part_models["cache_read_tokens"],
                calibration_run,
                "cache_read_tokens",
            ),
            "non_cache_tokens": _bucket_specific_hybrid_model(
                two_part_models["non_cache_tokens"],
                calibration_run,
                "non_cache_tokens",
            ),
            "total_tokens": _bucket_specific_hybrid_model(
                two_part_models["total_tokens"],
                calibration_run,
                "total_tokens",
            ),
            "elapsed_seconds": _bucket_specific_hybrid_model(
                two_part_models["elapsed_seconds"],
                calibration_run,
                "elapsed_seconds",
            ),
        }
        bucket_input = int(round(_estimate_from_two_part_model(
            class_count,
            required_lines,
            bucket_models["input_tokens"],
        ) * difficulty_multiplier))
        bucket_cache_read = int(round(_estimate_from_two_part_model(
            class_count,
            required_lines,
            bucket_models["cache_read_tokens"],
        ) * difficulty_multiplier))
        bucket_non_cache = int(round(_estimate_from_two_part_model(
            class_count,
            required_lines,
            bucket_models["non_cache_tokens"],
        ) * difficulty_multiplier))
        bucket_total = int(round(_estimate_from_two_part_model(
            class_count,
            required_lines,
            bucket_models["total_tokens"],
        ) * difficulty_multiplier))
        bucket_output = max(0, bucket_non_cache - bucket_input)
        bucket_elapsed = _estimate_from_two_part_model(
            class_count,
            required_lines,
            bucket_models["elapsed_seconds"],
        ) * difficulty_multiplier
        est_input += bucket_input
        est_cache_read += bucket_cache_read
        est_non_cache += bucket_non_cache
        est_total += bucket_total
        est_elapsed += bucket_elapsed
        bucket_estimates[bucket] = {
            "classes": class_count,
            "required_coverage_lines": required_lines,
            "rate_source": "bucket_sample_with_global_fixed_per_file",
            "difficulty_multiplier": difficulty_multiplier,
            "calibration_class": calibration_run.class_fqn,
            "calibration_status": calibration_run.status,
            "calibration_required_lines": calibration_run.required_coverage_lines,
            "calibration_complexity_score": calibration_run.complexity_score,
            "complexity_score_min": summary["complexity_score_min"],
            "complexity_score_max": summary["complexity_score_max"],
            "complexity_score_avg": summary["complexity_score_avg"],
            "bucket_models": bucket_models,
            "input_tokens": bucket_input,
            "cache_read_tokens": bucket_cache_read,
            "cache_hit_ratio": round(_ratio(bucket_cache_read, bucket_input + bucket_cache_read), 4),
            "output_tokens": bucket_output,
            "non_cache_tokens": bucket_non_cache,
            "total_tokens": bucket_total,
            "cost_usd_from_split_token_prices": _token_cost_usd(
                input_tokens=bucket_input,
                cache_read_tokens=bucket_cache_read,
                output_tokens=bucket_output,
                input_price=args.usd_per_1m_input_tokens,
                cached_input_price=args.usd_per_1m_cached_input_tokens,
                output_price=args.usd_per_1m_output_tokens,
            ),
            "elapsed_seconds": round(bucket_elapsed, 2),
            "elapsed_hours": round(bucket_elapsed / 3600.0, 2),
            "non_cache_fixed_tokens": round(
                class_count * bucket_models["non_cache_tokens"]["fixed_per_file"] * difficulty_multiplier
            ),
            "non_cache_variable_tokens": round(
                required_lines * bucket_models["non_cache_tokens"]["variable_per_required_line"] * difficulty_multiplier
            ),
            "total_fixed_tokens": round(
                class_count * bucket_models["total_tokens"]["fixed_per_file"] * difficulty_multiplier
            ),
            "total_variable_tokens": round(
                required_lines * bucket_models["total_tokens"]["variable_per_required_line"] * difficulty_multiplier
            ),
            "elapsed_fixed_seconds": round(
                class_count * bucket_models["elapsed_seconds"]["fixed_per_file"] * difficulty_multiplier,
                2,
            ),
            "elapsed_variable_seconds": round(
                required_lines * bucket_models["elapsed_seconds"]["variable_per_required_line"] * difficulty_multiplier,
                2,
            ),
        }
    price_non_cache = args.usd_per_1m_non_cache_tokens
    price_total = args.usd_per_1m_total_tokens
    price_input = args.usd_per_1m_input_tokens
    price_cached_input = args.usd_per_1m_cached_input_tokens
    price_output = args.usd_per_1m_output_tokens
    est_output = max(0, est_non_cache - est_input)
    split_cost = _token_cost_usd(
        input_tokens=est_input,
        cache_read_tokens=est_cache_read,
        output_tokens=est_output,
        input_price=price_input,
        cached_input_price=price_cached_input,
        output_price=price_output,
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inventory_path": args.inventory,
        "repo": inventory["repo"],
        "coverage_gate": inventory["coverage_gate"],
        "mutation_gate": inventory["mutation_gate"],
        "inventory_summary": inventory["summary"],
        "calibration_runs": [asdict(run) for run in runs],
        "calibration_rates": {
            "model": "bucket_sample_with_global_fixed_per_file",
            "model_note": (
                "Totals use percentile-bucket calibration. Each bucket keeps the globally fitted fixed "
                "per-file setup cost and derives its variable per-required-line rate from the bucket's "
                "representative calibration run."
            ),
            "two_part": two_part_models,
            "weighted_non_cache_tokens_per_required_line": weighted_non_cache_per_line,
            "weighted_total_tokens_per_required_line": weighted_total_per_line,
            "weighted_input_tokens_per_required_line": weighted_input_per_line,
            "weighted_cache_read_tokens_per_required_line": weighted_cache_read_per_line,
            "weighted_cache_hit_ratio": _ratio(
                calibration_cache_read_tokens,
                calibration_input_tokens + calibration_cache_read_tokens,
            ),
            "weighted_elapsed_seconds_per_required_line": weighted_elapsed_per_line,
            "mean_non_cache_tokens_per_required_line": statistics.mean(
                [run.non_cache_tokens_per_required_line for run in runs]
            ),
            "mean_total_tokens_per_required_line": statistics.mean([run.total_tokens_per_required_line for run in runs]),
            "mean_input_tokens_per_required_line": statistics.mean(
                [run.input_tokens_per_required_line for run in runs]
            ),
            "mean_cache_read_tokens_per_required_line": statistics.mean(
                [run.cache_read_tokens_per_required_line for run in runs]
            ),
            "mean_cache_hit_ratio": statistics.mean([run.cache_hit_ratio for run in runs]),
            "mean_elapsed_seconds_per_required_line": statistics.mean(
                [run.elapsed_seconds_per_required_line for run in runs]
            ),
        },
        "estimate": {
            "classes": total_class_count,
            "required_coverage_lines": total_required_lines,
            "bucket_strategy": (
                "complexity_score_percentile_bands with bucket-specific representative rates: "
                "<=p50, p50-p95, p95-max"
            ),
            "p95_max_difficulty_multiplier": args.p95_max_difficulty_multiplier,
            "input_tokens": est_input,
            "cache_read_tokens": est_cache_read,
            "cache_hit_ratio": round(_ratio(est_cache_read, est_input + est_cache_read), 4),
            "output_tokens": est_output,
            "non_cache_tokens": est_non_cache,
            "total_tokens": est_total,
            "elapsed_seconds": round(est_elapsed, 2),
            "elapsed_hours": round(est_elapsed / 3600.0, 2),
            "cost_usd_from_split_token_prices": split_cost,
            "cost_usd_from_non_cache_tokens": round(est_non_cache / 1_000_000 * price_non_cache, 4)
            if price_non_cache is not None
            else None,
            "cost_usd_from_total_tokens": round(est_total / 1_000_000 * price_total, 4)
            if price_total is not None
            else None,
            "by_bucket": bucket_estimates,
        },
        "pricing": {
            "usd_per_1m_non_cache_tokens": price_non_cache,
            "usd_per_1m_total_tokens": price_total,
            "usd_per_1m_input_tokens": price_input,
            "usd_per_1m_cached_input_tokens": price_cached_input,
            "usd_per_1m_output_tokens": price_output,
            "openai_standard_reference": OPENAI_GPT54_STANDARD_PRICING,
        },
    }


def _write_estimate_markdown(path: Path, payload: Dict[str, Any]) -> None:
    estimate = payload["estimate"]
    rates = payload["calibration_rates"]
    pricing = payload["pricing"]
    buckets = estimate["by_bucket"]

    def fmt_number(value: float) -> str:
        if isinstance(value, int) or float(value).is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"

    def bucket_token_formula(bucket: str, token_key: str) -> str:
        row = buckets[bucket]
        model = row["bucket_models"][token_key]
        value = row[token_key]
        return (
            f"{bucket}_{token_key.replace('_tokens', '')} = "
            f"({row['classes']} * {model['fixed_per_file']:,.2f} + "
            f"{row['required_coverage_lines']} * {model['variable_per_required_line']:,.2f}) "
            f"* {row['difficulty_multiplier']} = {fmt_number(value)}"
        )

    def bucket_output_formula(bucket: str) -> str:
        row = buckets[bucket]
        return (
            f"{bucket}_output = {bucket}_non_cache - {bucket}_input = "
            f"{fmt_number(row['non_cache_tokens'])} - {fmt_number(row['input_tokens'])} = "
            f"{fmt_number(row['output_tokens'])}"
        )

    cost_formula = (
        f"({estimate['input_tokens']} / 1,000,000 * {pricing['usd_per_1m_input_tokens']}) + "
        f"({estimate['cache_read_tokens']} / 1,000,000 * {pricing['usd_per_1m_cached_input_tokens']}) + "
        f"({estimate['output_tokens']} / 1,000,000 * {pricing['usd_per_1m_output_tokens']}) "
        f"= {estimate['cost_usd_from_split_token_prices']}"
    )
    bucket_names = list(buckets)
    formula_lines = [
        "```text",
        "global fixed-per-file setup:",
        "For each metric, fit calibration_value = global_fixed_per_file + required_lines * global_variable_per_required_line",
        "using the three calibration runs:",
        *[
            (
                f"- {run['estimate_bucket']}: required_lines={run['required_coverage_lines']}, "
                f"input={fmt_number(run['input_tokens'])}, cache_read={fmt_number(run['cache_read_tokens'])}, "
                f"non_cache={fmt_number(run['non_cache_tokens'])}, total={fmt_number(run['total_tokens'])}, "
                f"elapsed_seconds={fmt_number(run['elapsed_seconds'])}"
            )
            for run in payload["calibration_runs"]
        ],
        "",
        f"fitted input_fixed_per_file = {rates['two_part']['input_tokens']['fixed_per_file']:,.2f}",
        f"fitted cache_read_fixed_per_file = {rates['two_part']['cache_read_tokens']['fixed_per_file']:,.2f}",
        f"fitted non_cache_fixed_per_file = {rates['two_part']['non_cache_tokens']['fixed_per_file']:,.2f}",
        f"fitted total_fixed_per_file = {rates['two_part']['total_tokens']['fixed_per_file']:,.2f}",
        f"fitted elapsed_fixed_seconds_per_file = {rates['two_part']['elapsed_seconds']['fixed_per_file']:,.2f}",
        "",
        "total_price =",
        f"  (({' + '.join(bucket + '_input' for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_input_tokens']})",
        f"+ (({' + '.join(bucket + '_cache' for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_cached_input_tokens']})",
        f"+ (({' + '.join(bucket + '_output' for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_output_tokens']})",
        "",
        "total_price =",
        f"  (({' + '.join(fmt_number(buckets[bucket]['input_tokens']) for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_input_tokens']})",
        f"+ (({' + '.join(fmt_number(buckets[bucket]['cache_read_tokens']) for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_cached_input_tokens']})",
        f"+ (({' + '.join(fmt_number(buckets[bucket]['output_tokens']) for bucket in bucket_names)}) / 1,000,000 * {pricing['usd_per_1m_output_tokens']})",
        f"= {estimate['cost_usd_from_split_token_prices']}",
        "",
        "bucket token formula:",
        "bucket_tokens = (bucket_class_count * global_fixed_per_file + bucket_required_lines * bucket_variable_per_required_line) * difficulty_multiplier",
        "",
    ]
    for bucket in bucket_names:
        formula_lines.extend(
            [
                bucket_token_formula(bucket, "input_tokens"),
                bucket_token_formula(bucket, "cache_read_tokens"),
                bucket_token_formula(bucket, "non_cache_tokens"),
                bucket_output_formula(bucket),
                "",
            ]
        )
    formula_lines.append("```")
    lines = [
        f"# Unit Test Cost Estimate: {payload['repo']}",
        "",
        f"- Generated: `{payload['generated_at']}`",
        f"- Coverage gate: `{payload['coverage_gate']}%`",
        f"- Mutation gate: `{payload['mutation_gate']}%`",
        "",
        "## Estimate",
        "",
        "| Metric | Value | Calculation |",
        "|---|---:|---|",
        f"| Target files/classes | `{estimate['classes']}` | Count of selectable Java classes after testability filtering. |",
        f"| Required coverage lines | `{estimate['required_coverage_lines']}` | Sum of `ceil(JaCoCo executable lines * coverage_gate)` across selectable classes. |",
        f"| **Estimated non-cached input tokens** | **`{estimate['input_tokens']}`** | Sum of bucket input-token estimates after bucket-specific rates and difficulty multipliers. |",
        f"| Estimated cache-read input tokens | `{estimate['cache_read_tokens']}` | Sum of bucket cached-input estimates after bucket-specific rates and difficulty multipliers. |",
        f"| **Estimated input cache hit ratio** | **`{estimate['cache_hit_ratio']:.2%}`** | `{estimate['cache_read_tokens']} / ({estimate['input_tokens']} + {estimate['cache_read_tokens']}) = {estimate['cache_hit_ratio']:.2%}`. |",
        f"| Estimated output/reasoning tokens | `{estimate['output_tokens']}` | `{estimate['non_cache_tokens']} - {estimate['input_tokens']} = {estimate['output_tokens']}`. |",
        f"| **Estimated non-cache tokens** | **`{estimate['non_cache_tokens']}`** | `{estimate['input_tokens']} + {estimate['output_tokens']} = {estimate['non_cache_tokens']}`; this is the uncached billable work. |",
        f"| Estimated total tokens | `{estimate['total_tokens']}` | Sum of bucket total-token estimates; effectively non-cache plus cache-read tokens. |",
        f"| Estimated elapsed seconds | `{estimate['elapsed_seconds']}` | Sum of bucket elapsed-time estimates after difficulty multipliers. |",
        f"| **Estimated elapsed hours** | **`{estimate['elapsed_hours']}`** | `{estimate['elapsed_seconds']} / 3600 = {estimate['elapsed_hours']}`. |",
        f"| **Estimated cost from split token prices** | **`{estimate['cost_usd_from_split_token_prices']}`** | `{cost_formula}`. |",
        f"| **>P95 difficult budget multiplier** | **`{estimate['p95_max_difficulty_multiplier']}`** | Applied only to the `p95_max` bucket tokens, cost, and elapsed time. |",
        f"| Split pricing input/cache/output USD per 1M | "
        f"`{payload['pricing']['usd_per_1m_input_tokens']}` / "
        f"`{payload['pricing']['usd_per_1m_cached_input_tokens']}` / "
        f"`{payload['pricing']['usd_per_1m_output_tokens']}` | Defaults to the OpenAI standard split-pricing reference in this script. |",
        "",
        "## Formula Overview",
        "",
        *formula_lines,
        "",
        "## Calibration Model",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Model | `{rates['model']}` |",
        f"| Model note | `{rates['model_note']}` |",
        f"| Input fixed tokens / file | `{rates['two_part']['input_tokens']['fixed_per_file']:.2f}` |",
        f"| Input variable tokens / required line | `{rates['two_part']['input_tokens']['variable_per_required_line']:.2f}` |",
        f"| Cache-read fixed tokens / file | `{rates['two_part']['cache_read_tokens']['fixed_per_file']:.2f}` |",
        f"| Cache-read variable tokens / required line | `{rates['two_part']['cache_read_tokens']['variable_per_required_line']:.2f}` |",
        f"| Non-cache fixed tokens / file | `{rates['two_part']['non_cache_tokens']['fixed_per_file']:.2f}` |",
        f"| Non-cache variable tokens / required line | `{rates['two_part']['non_cache_tokens']['variable_per_required_line']:.2f}` |",
        f"| Total fixed tokens / file | `{rates['two_part']['total_tokens']['fixed_per_file']:.2f}` |",
        f"| Total variable tokens / required line | `{rates['two_part']['total_tokens']['variable_per_required_line']:.2f}` |",
        f"| Fixed elapsed seconds / file | `{rates['two_part']['elapsed_seconds']['fixed_per_file']:.2f}` |",
        f"| Variable elapsed seconds / required line | `{rates['two_part']['elapsed_seconds']['variable_per_required_line']:.2f}` |",
        f"| Weighted input tokens / required line | `{rates['weighted_input_tokens_per_required_line']:.2f}` |",
        f"| Weighted cache-read tokens / required line | `{rates['weighted_cache_read_tokens_per_required_line']:.2f}` |",
        f"| Weighted input cache hit ratio | `{rates['weighted_cache_hit_ratio']:.2%}` |",
        f"| Weighted non-cache tokens / required line | `{rates['weighted_non_cache_tokens_per_required_line']:.2f}` |",
        f"| Weighted total tokens / required line | `{rates['weighted_total_tokens_per_required_line']:.2f}` |",
        f"| Weighted elapsed seconds / required line | `{rates['weighted_elapsed_seconds_per_required_line']:.2f}` |",
        "",
        "## Bucket Estimates",
        "",
        f"- Bucket strategy: `{estimate['bucket_strategy']}`",
        "",
        "| Bucket | Calibration Class | Difficulty Multiplier | Classes | Score Range | Avg Score | Required Lines | Input Tokens | Cache Read | Cache Hit | Output Tokens | Cost USD | Non-cache Tokens | Total Tokens | Elapsed Hours |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    legacy_cost_rows = []
    if estimate["cost_usd_from_non_cache_tokens"] is not None:
        legacy_cost_rows.append(
            f"| Estimated cost from non-cache token price | `{estimate['cost_usd_from_non_cache_tokens']}` | Optional legacy single-rate estimate from `--usd-per-1m-non-cache-tokens`. |"
        )
    if estimate["cost_usd_from_total_tokens"] is not None:
        legacy_cost_rows.append(
            f"| Estimated cost from total token price | `{estimate['cost_usd_from_total_tokens']}` | Optional legacy single-rate estimate from `--usd-per-1m-total-tokens`. |"
        )
    if legacy_cost_rows:
        insert_at = lines.index(
            f"| Split pricing input/cache/output USD per 1M | "
            f"`{payload['pricing']['usd_per_1m_input_tokens']}` / "
            f"`{payload['pricing']['usd_per_1m_cached_input_tokens']}` / "
            f"`{payload['pricing']['usd_per_1m_output_tokens']}` | Defaults to the OpenAI standard split-pricing reference in this script. |"
        )
        lines[insert_at:insert_at] = legacy_cost_rows
    for bucket, row in estimate["by_bucket"].items():
        lines.append(
            f"| `{bucket}` | `{row['calibration_class']}` | `{row['difficulty_multiplier']}` | `{row['classes']}` | "
            f"`{row['complexity_score_min']}-{row['complexity_score_max']}` | `{row['complexity_score_avg']}` | "
            f"`{row['required_coverage_lines']}` | "
            f"`{row['input_tokens']}` | `{row['cache_read_tokens']}` | "
            f"`{row['cache_hit_ratio']:.2%}` | `{row['output_tokens']}` | "
            f"`{row['cost_usd_from_split_token_prices']}` | `{row['non_cache_tokens']}` | "
            f"`{row['total_tokens']}` | `{row['elapsed_hours']}` |"
        )
    lines.extend(
        [
            "",
        "## Calibration Runs",
        "",
        "| Estimate Bucket | Scan Bucket | Class | Status | Required Lines | Input Tokens | Cache Read | Cache Hit | Non-cache Tokens/File | Total Tokens/File | Elapsed Seconds/File |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for run in payload["calibration_runs"]:
        lines.append(
            f"| `{run['estimate_bucket']}` | `{run['complexity_bucket']}` | `{run['class_fqn']}` | `{run['status']}` | "
            f"`{run['required_coverage_lines']}` | "
            f"`{run['input_tokens']}` | "
            f"`{run['cache_read_tokens']}` | "
            f"`{run['cache_hit_ratio']:.2%}` | "
            f"`{run['non_cache_tokens_per_file']}` | "
            f"`{run['total_tokens_per_file']}` | "
            f"`{run['elapsed_seconds_per_file']:.2f}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_scan(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    modules = _normalize_modules(args.module)
    jacoco_xmls = _discover_jacoco_xmls(repo, modules, args.jacoco_xml)
    jacoco_line_counts, jacoco_summary = _load_jacoco_line_counts(jacoco_xmls)
    metrics, parse_summary = _parse_metrics(
        repo,
        modules,
        args.coverage_gate,
        jacoco_line_counts=jacoco_line_counts,
    )
    parse_summary.update(jacoco_summary)
    reps = _representatives(metrics)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_repo_slug(repo)}_unit_cost_inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "modules": modules,
        "coverage_gate": args.coverage_gate,
        "mutation_gate": args.mutation_gate,
        "parse_summary": parse_summary,
        "summary": _summarize_metrics(metrics),
        "calibration_plan": {
            "model": args.model,
            "provider": args.provider,
            "representative_classes": [
                {
                    **asdict(item),
                    "calibration_tier": tier,
                    "calibration_percentile": percentile,
                    "benchmark_command": _benchmark_command(args, item),
                }
                for tier, percentile, item in reps
            ],
        },
        "top_complex_classes": [
            asdict(item) for item in sorted(metrics, key=lambda item: item.complexity_score, reverse=True)[:25]
        ],
        "classes": [asdict(item) for item in sorted(metrics, key=lambda item: (item.module, item.class_fqn))],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_scan_markdown(md_path, payload)
    print(f"inventory_json={json_path}")
    print(f"inventory_markdown={md_path}")
    print(f"selectable_classes={payload['summary']['classes']}")
    print(f"required_coverage_lines={payload['summary']['required_coverage_lines']}")
    print(f"classes_with_jacoco_line_counts={payload['summary']['classes_with_jacoco_line_counts']}")
    print(f"classes_using_parser_line_fallback={payload['summary']['classes_using_parser_line_fallback']}")
    print("calibration_classes=" + ",".join(item.class_fqn for _, _, item in reps))


def run_estimate(args: argparse.Namespace) -> None:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = _estimate_payload(args)
    stem = f"{_repo_slug(Path(payload['repo']))}_unit_cost_estimate_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_estimate_markdown(md_path, payload)
    print(f"estimate_json={json_path}")
    print(f"estimate_markdown={md_path}")
    print(f"estimated_non_cache_tokens={payload['estimate']['non_cache_tokens']}")
    print(f"estimated_elapsed_hours={payload['estimate']['elapsed_hours']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan a Java repo and choose calibration classes")
    scan.add_argument("--repo", required=True)
    scan.add_argument("--module", action="append", default=[], help="Module name. Repeat or use comma-separated values.")
    scan.add_argument("--coverage-gate", type=int, default=80)
    scan.add_argument("--mutation-gate", type=int, default=70)
    scan.add_argument("--provider", default="openai")
    scan.add_argument("--model", default="openai/gpt-5.4")
    scan.add_argument("--variant", default="")
    scan.add_argument("--timeout-multiplier", default="1.0")
    scan.add_argument("--trace", action=argparse.BooleanOptionalAction, default=True)
    scan.add_argument(
        "--jacoco-xml",
        action="append",
        default=[],
        help="JaCoCo XML path. Repeat for multiple module reports. Defaults to <module>/target/site/jacoco/jacoco.xml.",
    )
    scan.add_argument("--output-dir", default="benchmark/estimates")
    scan.set_defaults(func=run_scan)

    estimate = sub.add_parser("estimate", help="Estimate cost from inventory and calibration benchmark reports")
    estimate.add_argument("--inventory", required=True)
    estimate.add_argument("--calibration-report", action="append", required=True)
    estimate.add_argument("--usd-per-1m-non-cache-tokens", type=float, default=None)
    estimate.add_argument("--usd-per-1m-total-tokens", type=float, default=None)
    estimate.add_argument(
        "--usd-per-1m-input-tokens",
        type=float,
        default=OPENAI_GPT54_STANDARD_PRICING["usd_per_1m_input_tokens"],
        help="Input-token price for split cost estimates. Defaults to OpenAI GPT-5.4 standard pricing.",
    )
    estimate.add_argument(
        "--usd-per-1m-cached-input-tokens",
        type=float,
        default=OPENAI_GPT54_STANDARD_PRICING["usd_per_1m_cached_input_tokens"],
        help="Cached-input-token price for split cost estimates. Defaults to OpenAI GPT-5.4 standard pricing.",
    )
    estimate.add_argument(
        "--usd-per-1m-output-tokens",
        type=float,
        default=OPENAI_GPT54_STANDARD_PRICING["usd_per_1m_output_tokens"],
        help="Output-token price for split cost estimates. Defaults to OpenAI GPT-5.4 standard pricing.",
    )
    estimate.add_argument(
        "--fixed-non-cache-tokens-per-file",
        type=float,
        default=None,
        help="Override fixed non-cache token cost paid once per target file/class.",
    )
    estimate.add_argument(
        "--fixed-total-tokens-per-file",
        type=float,
        default=None,
        help="Override fixed total token cost paid once per target file/class.",
    )
    estimate.add_argument(
        "--fixed-elapsed-seconds-per-file",
        type=float,
        default=None,
        help="Override fixed elapsed seconds paid once per target file/class.",
    )
    estimate.add_argument(
        "--p95-max-difficulty-multiplier",
        type=float,
        default=1.4,
        help="Budget multiplier for the >P95 complexity bucket. Defaults to 1.4 for a 40%% difficult-tail budget.",
    )
    estimate.add_argument("--output-dir", default="benchmark/estimates")
    estimate.set_defaults(func=run_estimate)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
