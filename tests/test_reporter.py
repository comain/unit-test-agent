import json

from uta.output.reporter import Reporter


def test_build_report_contains_project_summary_and_per_file_metrics(tmp_path):
    reporter = Reporter(str(tmp_path))
    existing = tmp_path / "src" / "test" / "java" / "com" / "example"
    existing.mkdir(parents=True, exist_ok=True)
    (existing / "ATest.java").write_text("class ATest {}", encoding="utf-8")
    (existing / "BTest.java").write_text("class BTest {}", encoding="utf-8")
    results = {
        "com.example.A": {
            "status": "PASS",
            "coverage": 80.0,
            "tests_pass": True,
            "mutation_score": 90.0,
            "surviving_mutants": 0,
            "total_mutants": 10,
            "killed_mutants": 9,
            "test_file_path": "src/test/java/com/example/ATest.java",
            "elapsed_seconds": 12.5,
            "generation_seconds": 4.0,
            "test_seconds": 3.0,
            "mutation_seconds": 5.0,
        },
        "com.example.B": {
            "status": "MUTATION_FAIL",
            "coverage": 75.0,
            "tests_pass": True,
            "mutation_score": 60.0,
            "surviving_mutants": 1,
            "total_mutants": 8,
            "killed_mutants": 5,
            "no_coverage_mutants": 2,
            "test_file_path": "src/test/java/com/example/BTest.java",
            "elapsed_seconds": 9.0,
        },
    }

    report = reporter.build_report(
        results,
        metadata={
            "total_candidates": 3,
            "total_elapsed_seconds": 30.0,
            "phase_timings": {"generation_session_seconds": 10.0},
            "session_token_usage": {
                "main_model_tokens": {"input": 100, "output": 40, "reasoning": 10, "cache_read": 50, "cache_write": 0, "total": 200},
                "small_model_tokens": {"input": 20, "output": 5, "reasoning": 0, "cache_read": 10, "cache_write": 0, "total": 35},
                "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
                "total_tokens": {"input": 120, "output": 45, "reasoning": 10, "cache_read": 60, "cache_write": 0, "total": 235},
            },
            "session_retrospect": {"hints": ["Prefer sibling api repos before jars."]},
        },
    )

    assert report["project_summary"]["total_candidates"] == 3
    assert report["project_summary"]["generated_test_files"] == 2
    assert report["project_summary"]["passed"] == 1
    assert report["project_summary"]["failed"] == 1
    assert report["mutation_summary"]["total_mutants"] == 18
    assert report["mutation_summary"]["no_coverage_mutants"] == 2
    assert len(report["per_file_metrics"]) == 2
    assert report["timing_details"]["generation_session_seconds"] == 10.0
    assert report["token_usage"]["main_model_tokens"]["total"] == 200
    assert report["token_usage"]["small_model_tokens"]["total"] == 35
    assert report["retrospect"]["hints"] == ["Prefer sibling api repos before jars."]


def test_save_report_persists_rich_report_shape(tmp_path):
    reporter = Reporter(str(tmp_path))
    reporter.save_report(
        {
            "com.example.A": {
                "status": "PASS",
                "coverage": 88.0,
                "tests_pass": True,
                "mutation_score": 100.0,
                "surviving_mutants": 0,
            }
        },
        "summary.json",
        metadata={
            "total_elapsed_seconds": 5.0,
            "phase_timings": {"compile_verification_seconds": 1.0},
            "session_token_usage": {
                "main_model_tokens": {"input": 10, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 15},
                "small_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
                "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
                "total_tokens": {"input": 10, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 15},
            },
            "session_retrospect": {"hints": ["Bias first round toward stronger branch assertions."]},
        },
    )

    data = json.loads((tmp_path / ".uta_reports" / "summary.json").read_text())
    assert "project_summary" in data
    assert "per_file_metrics" in data
    assert "timing_details" in data
    assert "token_usage" in data
    assert "retrospect" in data
    assert data["project_summary"]["total_elapsed_seconds"] == 5.0


def test_build_report_counts_only_existing_generated_test_files(tmp_path):
    reporter = Reporter(str(tmp_path))
    existing = tmp_path / "src" / "test" / "java" / "com" / "example"
    existing.mkdir(parents=True, exist_ok=True)
    (existing / "ATest.java").write_text("class ATest {}", encoding="utf-8")

    report = reporter.build_report(
        {
            "com.example.A": {
                "status": "PASS",
                "coverage": 80.0,
                "tests_pass": True,
                "test_file_path": "src/test/java/com/example/ATest.java",
            },
            "com.example.B": {
                "status": "INCOMPLETE_BATCH",
                "coverage": 0.0,
                "tests_pass": False,
                "test_file_path": "src/test/java/com/example/BTest.java",
            },
        }
    )

    assert report["project_summary"]["generated_test_files"] == 1


def test_python_report_uses_target_metadata_without_java_only_labels(tmp_path):
    reporter = Reporter(str(tmp_path))
    generated = tmp_path / "tests" / "uta_generated"
    generated.mkdir(parents=True)
    (generated / "test_jobs_forecast.py").write_text("def test_forecast():\n    assert True\n", encoding="utf-8")

    report = reporter.build_report(
        {
            "pysymbol:jobs/forecast.py::forecast_for_store": {
                "status": "PASS",
                "language": "python",
                "target_id": "pysymbol:jobs/forecast.py::forecast_for_store",
                "display_name": "jobs/forecast.py::forecast_for_store",
                "source_path": "jobs/forecast.py",
                "target_granularity": "function",
                "coverage": 0.0,
                "tests_pass": True,
                "mutation_score": 0.0,
                "test_file_path": "tests/uta_generated/test_jobs_forecast.py",
            }
        },
        metadata={"language": "python"},
    )

    metric = report["per_file_metrics"][0]
    assert metric["target"] == {
        "language": "python",
        "target_id": "pysymbol:jobs/forecast.py::forecast_for_store",
        "display_name": "jobs/forecast.py::forecast_for_store",
        "source_path": "jobs/forecast.py",
        "granularity": "function",
    }
    assert metric["language"] == "python"
    assert metric["target_id"] == "pysymbol:jobs/forecast.py::forecast_for_store"
    assert metric["display_name"] == "jobs/forecast.py::forecast_for_store"
    assert metric["source_path"] == "jobs/forecast.py"
    assert metric["target_granularity"] == "function"
    assert metric["class_fqn"] == "pysymbol:jobs/forecast.py::forecast_for_store"
    assert report["project_summary"]["target_label"] == "Target"
    assert report["project_summary"]["generated_test_files"] == 1
