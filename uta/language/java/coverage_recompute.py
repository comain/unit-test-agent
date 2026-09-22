"""Java project-coverage recompute via JaCoCo, batched per Maven module."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.enforcement.coverage_recompute import CoverageRecomputeResult
from uta.language.java.maven.jacoco import (
    find_jacoco_report,
    parse_jacoco_line_coverage_for_classes,
    run_tests_with_jacoco_batch,
)


class JavaProjectCoverageRecomputer:
    language = "java"

    def recompute(
        self,
        repo_path: str,
        class_rows: List[Dict[str, Any]],
        *,
        current_total: Optional[float] = None,
    ) -> CoverageRecomputeResult:
        module_batches: Dict[str, Dict[str, List[str]]] = {}
        for row in class_rows:
            test_file_path = row.get("test_file_path")
            if not test_file_path:
                continue
            module = str(row.get("module") or "")
            bucket = module_batches.setdefault(module, {"tests": [], "classes": []})
            test_selector = Path(str(test_file_path)).stem
            if test_selector and test_selector not in bucket["tests"]:
                bucket["tests"].append(test_selector)
            class_fqn = str(row.get("class_fqn") or "")
            if class_fqn and class_fqn not in bucket["classes"]:
                bucket["classes"].append(class_fqn)

        overall_covered = 0
        overall_missed = 0
        overall_matched_classes = 0
        module_results: List[Dict[str, Any]] = []
        for module, payload in module_batches.items():
            tests = payload["tests"]
            target_classes = payload["classes"]
            if not tests or not target_classes:
                continue
            ok, output = run_tests_with_jacoco_batch(str(repo_path), tests, module or None)
            jacoco_path = find_jacoco_report(str(repo_path), module or None)
            result = {
                "module": module,
                "test_count": len(tests),
                "ok": ok,
                "output": output[-500:] if isinstance(output, str) else "",
                "jacoco_path": jacoco_path,
            }
            if jacoco_path:
                module_cov = parse_jacoco_line_coverage_for_classes(jacoco_path, target_classes)
                result.update(module_cov)
                overall_covered += int(module_cov.get("covered_lines", 0) or 0)
                overall_missed += int(module_cov.get("missed_lines", 0) or 0)
                overall_matched_classes += int(module_cov.get("matched_classes", 0) or 0)
            module_results.append(result)

        coverage_total = current_total
        if overall_covered or overall_missed:
            denominator = overall_covered + overall_missed
            coverage_total = (overall_covered / denominator * 100.0) if denominator > 0 else 100.0

        recalc = {
            "ran": bool(module_results),
            "modules": module_results,
            "covered_lines": overall_covered if overall_matched_classes > 0 else None,
            "missed_lines": overall_missed if overall_matched_classes > 0 else None,
            "matched_classes": overall_matched_classes,
        }
        return CoverageRecomputeResult(recalc=recalc, coverage_total=coverage_total)
