import json
import os
import shutil
from pathlib import Path

from e2e_staged_harness import E2EStageRunner, java_lane_config, python_lane_configs


def _copy_fixture_repo(tmp_path: Path, fixture_name: str) -> Path:
    source = Path(__file__).resolve().parents[1] / "fixtures" / "python_projects" / fixture_name
    destination = tmp_path / fixture_name
    ignore = shutil.ignore_patterns(".uta_cache", ".uta_reports", "__pycache__")
    shutil.copytree(source, destination, ignore=ignore)
    return destination


def _lane(language: str):
    return next(config for config in python_lane_configs() if config.language == language)


def _make_java_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "java_fixture"
    source_root = repo / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True)
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "SampleService.java"
    (source_root / "SampleService.java").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return repo


def test_phase9_java_stage1_uses_same_staged_harness(tmp_path):
    repo = _make_java_fixture_repo(tmp_path)
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage1 = runner.run_java_stage1_scan_context(java_lane_config().replace(repo_path=repo, module=""))

    assert stage1.status == "passed"
    assert stage1.details["language"] == "java"
    assert stage1.details["language_decision"]["language"] == "java"
    assert stage1.details["selected_count"] == 1
    assert stage1.details["parsed_symbol_count"] > 0


def test_phase9_python3_stages_1_2_3_record_results(tmp_path):
    repo = _copy_fixture_repo(tmp_path, "py3_flat_project")
    config = _lane("python3").replace(repo_path=repo, target="jobs/forecast.py::forecast_for_store")
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage1 = runner.run_stage1_scan_context(config)
    stage2 = runner.run_stage2_enforcement(config, fake_tools=True)
    stage3 = runner.run_stage3_batch_plumbing(config)
    report_path = runner.write_report([stage1, stage2, stage3])

    assert stage1.status == "passed"
    assert "jobs/forecast.py" in stage1.details["selected_targets"]
    assert stage1.details["language_decision"]["language"] == "python"
    assert stage1.details["syntax_version"] == "python3"
    assert stage1.details["context_found"] is True

    assert stage2.status == "passed"
    assert stage2.details["backend"] == "python_enforcer"
    assert stage2.details["coverage_passed"] is True
    assert stage2.details["mutation_passed"] is True

    assert stage3.status == "passed"
    assert stage3.details["task_status"] == "COMPLETED"
    assert stage3.details["target_status"] == "PASS"
    assert stage3.details["generated_test_path"].startswith("tests/uta_generated/")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [item["stage"] for item in report["records"]] == [1, 2, 3]
    assert report["summary"]["passed"] == 3


def test_phase9_python3_full_stages_record_1_to_5_with_stage4_skip(tmp_path, monkeypatch):
    repo = _copy_fixture_repo(tmp_path, "py3_flat_project")
    config = _lane("python3").replace(repo_path=repo, target="jobs/forecast.py::forecast_for_store")
    runner = E2EStageRunner(results_dir=tmp_path / "results")
    monkeypatch.delenv("UTA_E2E_REAL_OPENCODE", raising=False)

    records = runner.run_python_stages(config, fake_enforcement=True)
    report_path = runner.write_report(records)

    assert [record.stage for record in records] == [1, 2, 3, 4, 5]
    assert records[3].status == "skipped"
    assert records[3].name == "real_generation_smoke"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"] == {"failed": 0, "passed": 4, "skipped": 1}


def test_phase9_python2_stage1_runs_and_stage2_skips_with_missing_runtime(tmp_path):
    repo = _copy_fixture_repo(tmp_path, "py2_legacy_project")
    config = _lane("python2").replace(
        repo_path=repo,
        target="legacy_job.py::legacy_total",
        python_bin="/missing/python2",
        mutmut_bin="/missing/mutmut",
    )
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage1 = runner.run_stage1_scan_context(config)
    stage2 = runner.run_stage2_enforcement(config)

    assert stage1.status == "passed"
    assert stage1.details["language_decision"]["language"] == "python"
    assert stage1.details["syntax_version"] == "python2"
    assert stage1.details["context_found"] is True

    assert stage2.status == "skipped"
    assert stage2.details["reason"] == "missing_python2_runtime"
    assert "/missing/python2" in stage2.details["detail"]


def test_phase9_python2_stage2_fake_runtime_records_legacy_mutmut_lane(tmp_path):
    repo = _copy_fixture_repo(tmp_path, "py2_legacy_project")
    config = _lane("python2").replace(
        repo_path=repo,
        target="legacy_job.py::legacy_total",
        python_bin="python2",
        mutmut_bin="mutmut",
    )
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage2 = runner.run_stage2_enforcement(config, fake_tools=True)

    assert stage2.status == "passed"
    assert stage2.details["runtime_lane"] == "mutmut-legacy-py2"


def test_phase9_stage4_skip_contract_is_well_formed(tmp_path, monkeypatch):
    repo = _copy_fixture_repo(tmp_path, "py3_flat_project")
    config = _lane("python3").replace(repo_path=repo, target="jobs/forecast.py::forecast_for_store")
    runner = E2EStageRunner(results_dir=tmp_path / "results")
    monkeypatch.delenv("UTA_E2E_REAL_OPENCODE", raising=False)

    stage4 = runner.run_stage4_real_generation(config)

    assert stage4.lane == "python3"
    assert stage4.stage == 4
    assert stage4.name == "real_generation_smoke"
    assert stage4.status == "skipped"
    assert "UTA_E2E_REAL_OPENCODE" in stage4.details["reason"]


def test_phase9_stage4_real_opencode_python3_when_configured(tmp_path):
    if os.environ.get("UTA_E2E_REAL_OPENCODE", "").lower() not in {"1", "true", "yes"}:
        import pytest

        pytest.skip("set UTA_E2E_REAL_OPENCODE=1 to run real OpenCode generation")
    repo = _copy_fixture_repo(tmp_path, "py3_flat_project")
    config = _lane("python3").replace(repo_path=repo, target="jobs/forecast.py::forecast_for_store")
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage4 = runner.run_stage4_real_generation(config)

    if stage4.details.get("target_status") == "PROVIDER_ERROR":
        assert stage4.status == "failed", stage4.details
        assert stage4.details["task_status"] in {"COMPLETED", "FAILED"}
        return
    assert stage4.status == "passed", stage4.details
    assert stage4.details["task_status"] == "COMPLETED"
    assert stage4.details["target_status"] == "PASS"


def test_phase9_stage5_python_ci_trigger_repair_and_rerun(tmp_path):
    repo = _copy_fixture_repo(tmp_path, "py3_flat_project")
    config = _lane("python3").replace(repo_path=repo, target="jobs/forecast.py::forecast_for_store")
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage5 = runner.run_stage5_ci_plugin_repair(config)

    assert stage5.status == "passed"
    assert stage5.details["initial_status"] == "failed"
    assert stage5.details["repair_task_language"] == "python"
    assert stage5.details["repair_quality_gate_backend"] == "python_enforcer"
    assert stage5.details["rerun_status"] == "success"
    assert stage5.details["runner_calls"] == 2


def test_phase9_real_python3_stages_1_2_when_configured(tmp_path):
    repo = os.environ.get("UTA_E2E_PY3_REPO")
    if not repo:
        import pytest

        pytest.skip("set UTA_E2E_PY3_REPO to run real Python 3 stage 1/2")
    config = _lane("python3")
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage1 = runner.run_stage1_scan_context(config)
    stage2 = runner.run_stage2_enforcement(config)

    assert stage1.status == "passed", stage1.details
    assert stage2.status in {"passed", "skipped"}, stage2.details


def test_phase9_real_python2_stage1_and_runtime_diagnostic_when_configured(tmp_path):
    repo = os.environ.get("UTA_E2E_PY2_REPO")
    if not repo:
        import pytest

        pytest.skip("set UTA_E2E_PY2_REPO to run real Python 2 stage 1/2")
    config = _lane("python2")
    runner = E2EStageRunner(results_dir=tmp_path / "results")

    stage1 = runner.run_stage1_scan_context(config)
    stage2 = runner.run_stage2_enforcement(config)

    assert stage1.status == "passed", stage1.details
    assert stage1.details["syntax_version"] == "python2"
    assert stage2.status in {"passed", "skipped"}, stage2.details
    if stage2.status == "skipped":
        assert stage2.details["reason"] in {"missing_python2_runtime", "missing_python2_mutmut"}
