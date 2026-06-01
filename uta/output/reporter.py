import json
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console
from rich.table import Table


def _round2(value: float) -> float:
    return round(float(value), 2)


class Reporter:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.report_dir = self.repo_path / ".uta_reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.console = Console()

    def _generated_test_file_exists(self, test_file_path: Optional[str]) -> bool:
        if not test_file_path:
            return False
        path = Path(test_file_path)
        if not path.is_absolute():
            path = self.repo_path / path
        return path.exists()

    def build_report(
        self,
        results: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata = metadata or {}
        phase_timings = metadata.get("phase_timings", {}) or {}
        token_usage = metadata.get("session_token_usage", {}) or {}
        report_language = str(metadata.get("language") or "java")
        target_metadata = metadata.get("targets_by_id", {}) or {}

        file_metrics = []
        pass_count = 0
        fail_count = 0
        generated_count = 0
        skip_count = 0
        coverages = []
        mutation_scores = []
        total_survivors = 0
        mutation_totals = {
            "total_mutants": 0,
            "killed_mutants": 0,
            "survived_mutants": 0,
            "no_coverage_mutants": 0,
            "timed_out_mutants": 0,
            "non_viable_mutants": 0,
            "memory_error_mutants": 0,
            "run_error_mutants": 0,
        }

        for fqn in sorted(results):
            res = results[fqn]
            target_meta = target_metadata.get(fqn) if isinstance(target_metadata, dict) else {}
            target_id = str(res.get("target_id") or (target_meta or {}).get("target_id") or fqn)
            language = str(res.get("language") or (target_meta or {}).get("language") or report_language or "java")
            display_name = str(
                res.get("display_name")
                or (target_meta or {}).get("display_name")
                or res.get("class_fqn")
                or target_id
            )
            source_path = res.get("source_path") or (target_meta or {}).get("source_path")
            target_granularity = (
                res.get("target_granularity")
                or res.get("granularity")
                or (target_meta or {}).get("target_granularity")
                or (target_meta or {}).get("granularity")
                or ("class" if language == "java" else "file")
            )
            status = res.get("status", "FAIL")
            if status == "PASS":
                pass_count += 1
            elif status == "GENERATED":
                generated_count += 1
            elif status == "SKIP":
                skip_count += 1
            else:
                fail_count += 1

            coverage = float(res.get("coverage", 0.0) or 0.0)
            mutation_score = float(res.get("mutation_score", 0.0) or 0.0)
            surviving_mutants = int(res.get("surviving_mutants", 0) or 0)
            coverages.append(coverage)
            mutation_scores.append(mutation_score)
            total_survivors += surviving_mutants

            mutation_totals["total_mutants"] += int(res.get("total_mutants", 0) or 0)
            mutation_totals["killed_mutants"] += int(res.get("killed_mutants", 0) or 0)
            mutation_totals["survived_mutants"] += surviving_mutants
            mutation_totals["no_coverage_mutants"] += int(res.get("no_coverage_mutants", 0) or 0)
            mutation_totals["timed_out_mutants"] += int(res.get("timed_out_mutants", 0) or 0)
            mutation_totals["non_viable_mutants"] += int(res.get("non_viable_mutants", 0) or 0)
            mutation_totals["memory_error_mutants"] += int(res.get("memory_error_mutants", 0) or 0)
            mutation_totals["run_error_mutants"] += int(res.get("run_error_mutants", 0) or 0)

            file_metrics.append(
                {
                    "target": {
                        "language": language,
                        "target_id": target_id,
                        "display_name": display_name,
                        "source_path": source_path,
                        "granularity": target_granularity,
                    },
                    "class_fqn": fqn,
                    "language": language,
                    "target_id": target_id,
                    "display_name": display_name,
                    "source_path": source_path,
                    "target_granularity": target_granularity,
                    "status": status,
                    "coverage": _round2(coverage),
                    "mutation_score": _round2(mutation_score),
                    "surviving_mutants": surviving_mutants,
                    "tests_pass": bool(res.get("tests_pass", False)),
                    "compile_pass": status not in {"COMPILE_FAIL", "GENERATED"},
                    "elapsed_seconds": _round2(res.get("elapsed_seconds", 0.0) or 0.0),
                    "generation_seconds": _round2(res.get("generation_seconds", 0.0) or 0.0),
                    "test_seconds": _round2(res.get("test_seconds", 0.0) or 0.0),
                    "mutation_seconds": _round2(res.get("mutation_seconds", 0.0) or 0.0),
                    "test_file_path": res.get("test_file_path"),
                }
            )

        is_java_report = report_language == "java" and all(item.get("language") == "java" for item in file_metrics)
        project_summary = {
            "language": report_language,
            "target_label": "Class FQN" if is_java_report else "Target",
            "total_candidates": int(metadata.get("total_candidates", len(results))),
            "generated_test_files": sum(
                1 for res in results.values() if self._generated_test_file_exists(res.get("test_file_path"))
            ),
            "total_results": len(results),
            "passed": pass_count,
            "failed": fail_count,
            "generated": generated_count,
            "skipped": skip_count,
            "coverage_average": _round2(sum(coverages) / len(coverages)) if coverages else 0.0,
            "coverage_min": _round2(min(coverages)) if coverages else 0.0,
            "mutation_average": _round2(sum(mutation_scores) / len(mutation_scores)) if mutation_scores else 0.0,
            "mutation_min": _round2(min(mutation_scores)) if mutation_scores else 0.0,
            "surviving_mutants": total_survivors,
            "total_elapsed_seconds": _round2(metadata.get("total_elapsed_seconds", 0.0) or 0.0),
        }

        return {
            "project_summary": project_summary,
            "per_file_metrics": file_metrics,
            "timing_details": {k: _round2(v) for k, v in phase_timings.items()},
            "token_usage": token_usage,
            "retrospect": metadata.get("session_retrospect", {}) or {},
            "metadata": {
                k: v for k, v in metadata.items()
                if k not in {"phase_timings", "session_retrospect", "session_token_usage"}
            },
            "mutation_summary": mutation_totals,
            "results": results,
        }

    def display_summary(
        self,
        results: Dict[str, Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        report = self.build_report(results, metadata)
        project = report["project_summary"]

        summary = Table(title="UTA Project Summary")
        summary.add_column("Metric", style="cyan")
        summary.add_column("Value", justify="right")
        summary.add_row("Candidates", str(project["total_candidates"]))
        summary.add_row("Generated Files", str(project["generated_test_files"]))
        summary.add_row("Passed / Failed / Generated / Skipped", f'{project["passed"]} / {project["failed"]} / {project["generated"]} / {project["skipped"]}')
        summary.add_row("Coverage Avg / Min", f'{project["coverage_average"]:.1f}% / {project["coverage_min"]:.1f}%')
        summary.add_row("Mutation Avg / Min", f'{project["mutation_average"]:.1f}% / {project["mutation_min"]:.1f}%')
        summary.add_row("Elapsed", f'{project["total_elapsed_seconds"]:.1f}s')
        self.console.print(summary)

        target_label = project.get("target_label") or "Class FQN"
        files = Table(title="UTA Per-Target Metrics" if target_label != "Class FQN" else "UTA Per-File Metrics")
        files.add_column(target_label, style="cyan")
        files.add_column("Status", style="bold")
        files.add_column("Coverage", justify="right")
        files.add_column("Mutation", justify="right")
        files.add_column("Survivors", justify="right")
        files.add_column("Compile/Test", justify="center")
        files.add_column("Elapsed", justify="right")

        for item in report["per_file_metrics"]:
            status = item["status"]
            status_style = "green" if status == "PASS" else ("yellow" if status in {"SKIP", "GENERATED"} else "red")
            compile_test = f'{"Y" if item["compile_pass"] else "N"}/{"Y" if item["tests_pass"] else "N"}'
            files.add_row(
                item.get("display_name") or item["class_fqn"],
                f"[{status_style}]{status}[/{status_style}]",
                f'{item["coverage"]:.1f}%',
                f'{item["mutation_score"]:.1f}%',
                str(item["surviving_mutants"]),
                compile_test,
                f'{item["elapsed_seconds"]:.1f}s',
            )
        self.console.print(files)

        if report["timing_details"]:
            timing = Table(title="UTA Timing Details")
            timing.add_column("Phase", style="cyan")
            timing.add_column("Seconds", justify="right")
            for key, value in sorted(report["timing_details"].items()):
                timing.add_row(key, f"{value:.2f}")
            self.console.print(timing)

        token_usage = report.get("token_usage") or {}
        total_tokens = token_usage.get("total_tokens") or {}
        if total_tokens:
            tokens = Table(title="UTA Token Usage")
            tokens.add_column("Bucket", style="cyan")
            tokens.add_column("Input", justify="right")
            tokens.add_column("Output", justify="right")
            tokens.add_column("Reasoning", justify="right")
            tokens.add_column("Cache Read", justify="right")
            tokens.add_column("Total", justify="right")

            def add_bucket(name: str, bucket: Dict[str, Any]) -> None:
                if not bucket:
                    return
                tokens.add_row(
                    name,
                    str(int(bucket.get("input", 0) or 0)),
                    str(int(bucket.get("output", 0) or 0)),
                    str(int(bucket.get("reasoning", 0) or 0)),
                    str(int(bucket.get("cache_read", 0) or 0)),
                    str(int(bucket.get("total", 0) or 0)),
                )

            add_bucket("Main Model", token_usage.get("main_model_tokens") or {})
            add_bucket("Small Model", token_usage.get("small_model_tokens") or {})
            other_bucket = token_usage.get("other_model_tokens") or {}
            if any(int(other_bucket.get(k, 0) or 0) for k in ("input", "output", "reasoning", "cache_read", "total")):
                add_bucket("Other Models", other_bucket)
            add_bucket("Total", total_tokens)
            self.console.print(tokens)

        retrospect = report.get("retrospect") or {}
        hints = retrospect.get("hints") or []
        if hints:
            retro = Table(title="UTA Session Retrospect")
            retro.add_column("Prompt Improvements", style="cyan")
            for hint in hints[:8]:
                retro.add_row(hint)
            self.console.print(retro)

    def save_report(
        self,
        results: Dict[str, Dict[str, Any]],
        filename: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        report_path = self.report_dir / filename
        report = self.build_report(results, metadata)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        self.console.print(f"[dim]Report saved to {report_path}[/dim]")
