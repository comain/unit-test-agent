from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from e2e_staged_harness import E2EStageRunner, E2ELaneConfig, JavaE2ELaneConfig, java_lane_config
from e2e_real_harness import (
    collect_tool_versions,
    default_real_e2e_report_path,
    git_current_branch,
    missing_lane,
    new_real_e2e_run_id,
    prepare_real_e2e_workspace,
    real_e2e_mode,
    require_real_e2e_mode,
    run_python_diff_repair_lane,
    write_real_e2e_report,
)


def _real_opencode_enabled() -> bool:
    return os.environ.get("UTA_E2E_REAL_OPENCODE", "").lower() in {"1", "true", "yes"}
from uta.shared.languages import RawTargetSelection, default_registry
from uta.language.java.verification.runner import (
    JavaCoverageSummary,
    JavaMutationSummary,
    JavaVerificationResult,
)
from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.test_selection import discover_strict_python_test_candidates

DEFAULT_UTA_PY3_TARGET = "uta_e2e_probe.py::discounted_total"
DEFAULT_UTA_PY3_TEST_PATHS = ("tests/test_uta_e2e_probe.py",)


def test_real_e2e_mode_accepts_only_real_and_nightly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UTA_E2E_MODE", "real")
    assert real_e2e_mode() == "real"

    monkeypatch.setenv("UTA_E2E_MODE", "nightly")
    assert real_e2e_mode() == "nightly"

    monkeypatch.setenv("UTA_E2E_MODE", "REAL")
    assert real_e2e_mode() == "real"

    monkeypatch.setenv("UTA_E2E_MODE", "smoke")
    assert real_e2e_mode() is None

    monkeypatch.delenv("UTA_E2E_MODE")
    assert real_e2e_mode() is None


def test_require_real_e2e_mode_skips_without_real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UTA_E2E_MODE", raising=False)

    with pytest.raises(pytest.skip.Exception, match="UTA_E2E_MODE=real or nightly"):
        require_real_e2e_mode()


def test_require_real_e2e_mode_returns_configured_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UTA_E2E_MODE", "nightly")

    assert require_real_e2e_mode() == "nightly"


def test_prepare_real_clone_workspace_copies_source_without_mutating_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (source / "pkg.py").write_text("VALUE = 1\n", encoding="utf-8")

    workspace = prepare_real_e2e_workspace(
        source,
        workspace_root=tmp_path / "workspaces",
        lane_name="python",
        run_id="run-1",
    )
    (workspace / "pkg.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert workspace != source
    assert workspace.name == "python"
    assert (source / "pkg.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace / "pyproject.toml").exists()


def test_write_real_e2e_report_records_summary_environment_and_artifacts(tmp_path: Path) -> None:
    report_path = write_real_e2e_report(
        tmp_path / "real-e2e-results.json",
        lanes=[
            {
                "name": "java",
                "language": "java",
                "status": "passed",
                "repo": "/source/java",
                "branch": "feature/java",
                "baseRef": "origin/master",
                "targetCount": 2,
                "elapsedSeconds": 1.25,
                "workspace": "/work/java",
                "artifacts": {"log": "/work/java/run.log"},
            },
            {
                "name": "python3",
                "language": "python",
                "status": "skipped",
                "reason": "missing_repo",
                "repo": "/source/python",
                "workspace": "",
                "artifacts": {},
            },
        ],
        environment={"mode": "real", "run_id": "run-1"},
        tool_versions={"python": "Python 3.11.0"},
    )

    payload = pytest.importorskip("json").loads(report_path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 1
    assert payload["generatedAt"]
    assert payload["summary"] == {"passed": 1, "failed": 0, "skipped": 1}
    assert payload["environment"]["mode"] == "real"
    assert payload["toolVersions"]["python"] == "Python 3.11.0"
    java_lane = payload["lanes"][0]
    skipped_lane = payload["lanes"][1]
    for lane in (java_lane, skipped_lane):
        assert {"repo", "branch", "baseRef", "targetCount", "elapsedSeconds", "artifacts"} <= set(lane)
    assert java_lane["branch"] == "feature/java"
    assert java_lane["baseRef"] == "origin/master"
    assert java_lane["targetCount"] == 2
    assert java_lane["elapsedSeconds"] == 1.25
    assert java_lane["artifacts"]["log"] == "/work/java/run.log"
    assert skipped_lane["branch"] == ""
    assert skipped_lane["baseRef"] == "origin/master"
    assert skipped_lane["targetCount"] == 0
    assert skipped_lane["elapsedSeconds"] == 0.0


def test_real_lane_skip_records_missing_repo() -> None:
    lane = missing_lane("python3", language="python", repo="/missing/repo", reason="missing_repo")

    assert lane["status"] == "skipped"
    assert lane["reason"] == "missing_repo"
    assert lane["repo"] == "/missing/repo"
    assert lane["branch"] == ""
    assert lane["baseRef"] == "origin/master"
    assert lane["targetCount"] == 0
    assert lane["elapsedSeconds"] == 0.0


def test_java_real_enforcement_stage_runs_existing_verifier_for_matching_unit_test(tmp_path: Path) -> None:
    _write_java_pair(
        tmp_path,
        module="biz",
        class_fqn="com.example.order.OrderService",
        test_class_fqn="com.example.order.OrderServiceTest",
    )
    captured = {}

    def fake_verify(repo_path, target, **kwargs):
        captured["repo_path"] = repo_path
        captured["target"] = target.target_id
        captured["kwargs"] = kwargs
        return JavaVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=JavaCoverageSummary(line_rate=91.0, gate=80.0, passed=True, xml_path="target/jacoco.xml"),
            mutation=JavaMutationSummary(
                generated=3,
                killed=3,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=70.0,
                passed=True,
                report_path="target/mutations.xml",
            ),
        )

    record = E2EStageRunner(results_dir=tmp_path / "results").run_java_stage2_enforcement(
        JavaE2ELaneConfig(language="java", repo_path=tmp_path, module="biz"),
        verify_func=fake_verify,
        require_tools=False,
    )

    assert record.status == "passed"
    assert record.details["backend"] == "java_verifier"
    assert record.details["target"] == "com.example.order.OrderService"
    assert record.details["test_class_fqn"] == "com.example.order.OrderServiceTest"
    assert record.details["coverage"]["line_rate"] == 91.0
    assert record.details["mutation"]["generated"] == 3
    assert record.details["artifacts"]["coverageXml"] == "target/jacoco.xml"
    assert record.details["artifacts"]["mutationReport"] == "target/mutations.xml"
    assert captured["repo_path"] == tmp_path
    assert captured["target"] == "com.example.order.OrderService"
    assert captured["kwargs"]["test_class_fqn"] == "com.example.order.OrderServiceTest"
    assert captured["kwargs"]["config"].module == "biz"


def test_java_real_enforcement_stage_skips_when_no_matching_unit_test(tmp_path: Path) -> None:
    main_path = tmp_path / "biz" / "src" / "main" / "java" / "com" / "example" / "OrderService.java"
    main_path.parent.mkdir(parents=True)
    main_path.write_text("package com.example;\npublic class OrderService {}\n", encoding="utf-8")

    record = E2EStageRunner(results_dir=tmp_path / "results").run_java_stage2_enforcement(
        JavaE2ELaneConfig(language="java", repo_path=tmp_path, module="biz"),
        verify_func=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("verify should not run")),
        require_tools=False,
    )

    assert record.status == "skipped"
    assert record.details["reason"] == "missing_java_test_candidate"


def test_java_real_enforcement_stage_fails_when_verifier_fails(tmp_path: Path) -> None:
    _write_java_pair(tmp_path, module="", class_fqn="com.example.OrderService", test_class_fqn="com.example.OrderServiceTest")

    record = E2EStageRunner(results_dir=tmp_path / "results").run_java_stage2_enforcement(
        JavaE2ELaneConfig(language="java", repo_path=tmp_path),
        verify_func=lambda *args, **kwargs: JavaVerificationResult(
            status="failed",
            reason_code="mutation_gate_failed",
            tests_pass=True,
            mutation=JavaMutationSummary(
                generated=4,
                killed=2,
                survived=2,
                no_coverage=0,
                rate=50.0,
                gate=70.0,
                passed=False,
                report_path="target/mutations.xml",
            ),
            message="Java mutation score 50.00% is below gate 70.00%",
        ),
        require_tools=False,
    )

    assert record.status == "failed"
    assert record.details["reason_code"] == "mutation_gate_failed"
    assert record.details["mutation"]["rate"] == 50.0


def test_python_real_lane_config_auto_selects_target_with_strict_test(tmp_path: Path) -> None:
    source = tmp_path / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def forecast():\n    return 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_forecast.py").write_text("from jobs.forecast import forecast\n", encoding="utf-8")

    config = _python3_config_for_workspace(tmp_path, explicit_target="", explicit_test_paths=())

    assert config.target == "jobs/forecast.py"
    assert config.test_paths == ("tests/test_forecast.py",)


def test_python_real_lane_config_reports_no_strict_test(tmp_path: Path) -> None:
    source = tmp_path / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def forecast():\n    return 1\n", encoding="utf-8")

    config = _python3_config_for_workspace(tmp_path, explicit_target="", explicit_test_paths=())

    assert config.target == "jobs/forecast.py"
    assert config.test_paths == ()


def test_python_real_lane_config_preserves_explicit_target_and_tests(tmp_path: Path) -> None:
    source = tmp_path / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def forecast():\n    return 1\n", encoding="utf-8")

    config = _python3_config_for_workspace(
        tmp_path,
        explicit_target="jobs/forecast.py",
        explicit_test_paths=("tests/test_forecast.py",),
    )

    assert config.target == "jobs/forecast.py"
    assert config.test_paths == ("tests/test_forecast.py",)


def test_python_real_lane_config_prefers_uta_self_test_target(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "unit-test-agent"\n', encoding="utf-8")
    target = tmp_path / DEFAULT_UTA_PY3_TARGET.split("::", 1)[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def discounted_total(subtotal, discount):\n    return subtotal - discount\n", encoding="utf-8")
    test_file = tmp_path / DEFAULT_UTA_PY3_TEST_PATHS[0]
    test_file.parent.mkdir(parents=True)
    test_file.write_text("from uta_e2e_probe import discounted_total\n", encoding="utf-8")

    config = _python3_config_for_workspace(tmp_path, explicit_target="", explicit_test_paths=())

    assert config.target == DEFAULT_UTA_PY3_TARGET
    assert config.test_paths == DEFAULT_UTA_PY3_TEST_PATHS


@pytest.mark.real_e2e
def test_real_opencode_python_diff_repair_session(tmp_path: Path) -> None:
    """Live-OpenCode lane: real `HEAD~n` diff drives a genuine repair fix session.

    Gated on UTA_E2E_MODE=real|nightly AND UTA_E2E_REAL_OPENCODE, since it needs
    live model access. The diff (last n commits of the cloned repo) gives mutmut a
    real changed-line scope so surviving mutants actually trigger a repair turn.
    """
    require_real_e2e_mode()
    if not _real_opencode_enabled():
        pytest.skip("set UTA_E2E_REAL_OPENCODE=1 to run the live OpenCode repair lane")
    run_id = os.environ.get("UTA_E2E_RUN_ID", new_real_e2e_run_id())
    workspace_root = Path(os.environ.get("UTA_E2E_REAL_WORKSPACE_ROOT", str(tmp_path / "workspaces")))
    source = Path(os.environ.get("UTA_E2E_PY3_REPO", str(_default_uta_repo_path()))).expanduser()
    lane = run_python_diff_repair_lane(
        source=source,
        workspace_root=workspace_root,
        run_id=run_id,
        diff_depth=int(os.environ.get("UTA_E2E_REAL_OPENCODE_DIFF_DEPTH", "10")),
        coverage_gate=float(os.environ.get("UTA_E2E_REAL_OPENCODE_COVERAGE_GATE", "80")),
        mutation_gate=float(os.environ.get("UTA_E2E_REAL_OPENCODE_MUTATION_GATE", "90")),
        timeout_seconds=int(os.environ.get("UTA_E2E_REAL_OPENCODE_TIMEOUT_SECONDS", "900")),
    )
    report_path = Path(os.environ.get("UTA_E2E_REPORT_PATH", str(default_real_e2e_report_path())))
    write_real_e2e_report(
        report_path.with_name("real-e2e-opencode-results.json"),
        lanes=[lane],
        environment={"mode": real_e2e_mode(), "run_id": run_id, "lane": "opencode_diff_repair"},
        tool_versions=collect_tool_versions(),
    )
    if lane["status"] == "failed":
        pytest.fail(f"live OpenCode repair lane failed: {lane}")
    if lane["status"] == "skipped":
        pytest.skip(f"live OpenCode repair lane skipped: {lane.get('reason')}")
    # The lane is meaningful only if it actually drove a model session over a real diff.
    assert lane.get("sessionCount", 0) >= 1, lane
    assert lane.get("changedLineCount", 0) >= 1, lane


@pytest.mark.real_e2e
def test_real_e2e_java_and_python_lanes_write_report(tmp_path: Path) -> None:
    mode = require_real_e2e_mode()
    run_id = os.environ.get("UTA_E2E_RUN_ID", new_real_e2e_run_id())
    workspace_root = Path(os.environ.get("UTA_E2E_REAL_WORKSPACE_ROOT", str(tmp_path / "workspaces")))
    report_path = Path(os.environ.get("UTA_E2E_REPORT_PATH", str(default_real_e2e_report_path())))
    runner = E2EStageRunner(results_dir=report_path.parent / "stage-results")
    lanes = [
        _run_real_java_lane(runner, workspace_root=workspace_root, run_id=run_id),
        _run_real_python3_lane(runner, workspace_root=workspace_root, run_id=run_id),
    ]
    write_real_e2e_report(
        report_path,
        lanes=lanes,
        environment={"mode": mode, "run_id": run_id, "workspaceRoot": str(workspace_root)},
        tool_versions=collect_tool_versions(),
    )

    failed = [lane for lane in lanes if lane["status"] == "failed"]
    if failed:
        pytest.fail(f"real_e2e lane failure: {failed}")
    if all(lane["status"] == "skipped" for lane in lanes):
        pytest.skip("all real_e2e lanes skipped; see .uta_reports/real-e2e/real-e2e-results.json")


def _run_real_java_lane(runner: E2EStageRunner, *, workspace_root: Path, run_id: str) -> dict:
    source = Path(os.environ.get("UTA_E2E_REPO", "~/wms/sample-inbound-core")).expanduser()
    base_ref = os.environ.get("UTA_E2E_BASE_REF", "origin/master")
    if not source.exists():
        return missing_lane("java-wms", language="java", repo=source, reason="missing_repo", base_ref=base_ref)
    started = time.monotonic()
    try:
        workspace = prepare_real_e2e_workspace(source, workspace_root=workspace_root, lane_name="java-wms", run_id=run_id)
        config = java_lane_config().replace(
            repo_path=workspace,
            module=os.environ.get("UTA_E2E_MODULE", java_lane_config().module),
        )
        stage1 = runner.run_java_stage1_scan_context(config)
        records = [stage1]
        if stage1.status == "passed":
            records.append(runner.run_java_stage2_enforcement(config))
        if any(_record_status(record) == "failed" for record in records):
            status = "failed"
        elif any(_record_status(record) == "skipped" for record in records):
            status = "skipped"
        else:
            status = "passed"
        scan_details = stage1.details or {}
        enforcement_details = (_record_as_dict(records[-1]).get("details") or {}) if len(records) > 1 else {}
        return {
            "name": "java-wms",
            "language": "java",
            "status": status,
            "reason": _first_skip_reason(records) if status == "skipped" else "",
            "repo": str(source),
            "branch": git_current_branch(workspace),
            "baseRef": base_ref,
            "targetCount": 1 if enforcement_details.get("target") else int(scan_details.get("selected_count") or 0),
            "elapsedSeconds": time.monotonic() - started,
            "workspace": str(workspace),
            "target": str(enforcement_details.get("target") or ""),
            "stages": [_record_as_dict(record) for record in records],
            "artifacts": {
                "stageResultsDir": str(runner.results_dir),
                "coverageXml": (enforcement_details.get("artifacts") or {}).get("coverageXml", ""),
                "mutationReport": (enforcement_details.get("artifacts") or {}).get("mutationReport", ""),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "java-wms",
            "language": "java",
            "status": "failed",
            "repo": str(source),
            "branch": "",
            "baseRef": base_ref,
            "targetCount": 0,
            "elapsedSeconds": time.monotonic() - started,
            "workspace": "",
            "reason": str(exc),
            "artifacts": {"stageResultsDir": str(runner.results_dir)},
        }


def _run_real_python3_lane(runner: E2EStageRunner, *, workspace_root: Path, run_id: str) -> dict:
    source = Path(os.environ.get("UTA_E2E_PY3_REPO", str(_default_uta_repo_path()))).expanduser()
    base_ref = os.environ.get("UTA_E2E_BASE_REF", "origin/master")
    if not source.exists():
        return missing_lane("python3-uta", language="python", repo=source, reason="missing_repo", base_ref=base_ref)
    target = os.environ.get("UTA_E2E_PY3_TARGET", "")
    started = time.monotonic()
    try:
        workspace = prepare_real_e2e_workspace(source, workspace_root=workspace_root, lane_name="python3-uta", run_id=run_id)
        if not target and not os.environ.get("UTA_E2E_PY3_TEST_PATHS", "") and _is_uta_repo(workspace):
            _write_python_probe(workspace)
        config = _python3_config_for_workspace(
            workspace,
            explicit_target=target,
            explicit_test_paths=tuple(path for path in os.environ.get("UTA_E2E_PY3_TEST_PATHS", "").split(os.pathsep) if path),
            tool_source=source,
        )
        target = config.target
        stage1 = runner.run_stage1_scan_context(config)
        records = [stage1]
        if stage1.status == "passed" and config.test_paths:
            records.append(runner.run_stage2_enforcement(config))
        elif stage1.status == "passed":
            records.append(
                missing_lane("python3-uta-stage2", language="python", repo=source, reason="no_strict_python_test")
            )
        if any(_record_status(record) == "failed" for record in records):
            status = "failed"
        elif any(_record_status(record) == "skipped" for record in records):
            status = "skipped"
        else:
            status = "passed"
        enforcement_details = (_record_as_dict(records[-1]).get("details") or {}) if len(records) > 1 else {}
        return {
            "name": "python3-uta",
            "language": "python",
            "status": status,
            "reason": _first_skip_reason(records) if status == "skipped" else "",
            "repo": str(source),
            "branch": git_current_branch(workspace),
            "baseRef": base_ref,
            "targetCount": 1 if target else 0,
            "elapsedSeconds": time.monotonic() - started,
            "workspace": str(workspace),
            "target": target,
            "stages": [_record_as_dict(record) for record in records],
            "artifacts": {
                "stageResultsDir": str(runner.results_dir),
                "coverageXml": (enforcement_details.get("artifacts") or {}).get("coverageXml", ""),
                "mutationArtifacts": (enforcement_details.get("artifacts") or {}).get("mutationArtifacts", {}),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "python3-uta",
            "language": "python",
            "status": "failed",
            "repo": str(source),
            "branch": "",
            "baseRef": base_ref,
            "targetCount": 1 if target else 0,
            "elapsedSeconds": time.monotonic() - started,
            "workspace": "",
            "target": target,
            "reason": str(exc),
            "artifacts": {"stageResultsDir": str(runner.results_dir)},
        }


def _record_status(record) -> str:
    return record.get("status") if isinstance(record, dict) else record.status


def _record_as_dict(record) -> dict:
    return dict(record) if isinstance(record, dict) else record.as_dict()


def _first_skip_reason(records) -> str:
    for record in records:
        if _record_status(record) == "skipped":
            data = _record_as_dict(record)
            details = data.get("details") or {}
            return str(data.get("reason") or details.get("reason") or "skipped")
    return ""


def _default_uta_repo_path() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_python_target_for_workspace(workspace: Path) -> str:
    candidates = list(default_registry().adapter_for("python").scan_candidates(workspace))
    if not candidates:
        raise ValueError(f"no Python production source files found in {workspace}")
    target = candidates[0]
    return target.display_name or target.source_path or target.target_id


def _python3_config_for_workspace(
    workspace: Path,
    *,
    explicit_target: str,
    explicit_test_paths: tuple[str, ...],
    tool_source: Path | None = None,
) -> E2ELaneConfig:
    target = explicit_target or _default_uta_python_target(workspace) or _default_python_target_for_workspace(workspace)
    test_paths = explicit_test_paths
    if not test_paths:
        if target == DEFAULT_UTA_PY3_TARGET and _is_uta_repo(workspace):
            test_paths = DEFAULT_UTA_PY3_TEST_PATHS
        selected = None if test_paths else _python_target_with_strict_tests(workspace, explicit_target=explicit_target)
        if selected:
            target, test_paths = selected
    tool_source = tool_source or workspace
    return E2ELaneConfig(
        language="python3",
        repo_path=workspace,
        target=target,
        test_paths=test_paths,
        python_bin=os.environ.get("UTA_E2E_PY3_BIN", _repo_tool(tool_source, ".venv/bin/python", "python3")),
        mutmut_bin=os.environ.get("UTA_E2E_PY3_MUTMUT_BIN", _repo_tool(tool_source, ".venv/bin/mutmut", "mutmut")),
        repo_env="UTA_E2E_PY3_REPO",
        target_env="UTA_E2E_PY3_TARGET",
    )


def _default_uta_python_target(workspace: Path) -> str:
    if not _is_uta_repo(workspace):
        return ""
    source_path = DEFAULT_UTA_PY3_TARGET.split("::", 1)[0]
    if (workspace / source_path).is_file() and all((workspace / path).is_file() for path in DEFAULT_UTA_PY3_TEST_PATHS):
        return DEFAULT_UTA_PY3_TARGET
    return ""


def _is_uta_repo(workspace: Path) -> bool:
    pyproject = workspace / "pyproject.toml"
    return pyproject.is_file() and 'name = "unit-test-agent"' in pyproject.read_text(encoding="utf-8", errors="ignore")


def _repo_tool(source: Path, relative_tool: str, fallback: str) -> str:
    candidate = source / relative_tool
    return str(candidate) if candidate.is_file() else fallback


def _write_python_probe(workspace: Path) -> None:
    source_path = workspace / DEFAULT_UTA_PY3_TARGET.split("::", 1)[0]
    test_path = workspace / DEFAULT_UTA_PY3_TEST_PATHS[0]
    source_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    source_text = source_path.read_text(encoding="utf-8", errors="ignore") if source_path.exists() else ""
    probe_source = "\n".join(
        [
            "",
            "",
            "def discounted_total(subtotal: float, discount_rate: float) -> float:",
            "    if subtotal < 0:",
            "        raise ValueError('subtotal must be non-negative')",
            "    if not 0 <= discount_rate <= 1:",
            "        raise ValueError('discount_rate must be between 0 and 1')",
            "    return round(subtotal * (1 - discount_rate), 2)",
            "",
        ]
    )
    if "def discounted_total(" not in source_text:
        source_path.write_text(source_text.rstrip() + probe_source, encoding="utf-8")
    test_path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "from uta_e2e_probe import discounted_total",
                "",
                "def test_discounted_total_applies_discount():",
                "    assert discounted_total(100, 0.15) == 85.0",
                "",
                "",
                "def test_discounted_total_allows_full_discount():",
                "    assert discounted_total(50, 1) == 0",
                "",
                "",
                "def test_discounted_total_rejects_invalid_inputs():",
                "    with pytest.raises(ValueError):",
                "        discounted_total(-1, 0.1)",
                "    with pytest.raises(ValueError):",
                "        discounted_total(10, -0.1)",
                "    with pytest.raises(ValueError):",
                "        discounted_total(10, 1.1)",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _python_target_with_strict_tests(workspace: Path, *, explicit_target: str) -> tuple[str, tuple[str, ...]] | None:
    adapter = default_registry().adapter_for("python")
    builder = PythonContextBuilder(workspace)
    raw_targets = [explicit_target] if explicit_target else [
        candidate.display_name or candidate.source_path or candidate.target_id
        for candidate in adapter.scan_candidates(workspace)
    ]
    for raw_target in raw_targets:
        if not raw_target:
            continue
        target = adapter.normalize_target(RawTargetSelection(target=raw_target))
        context = builder.build_target_context(target)
        matches = discover_strict_python_test_candidates(workspace, target, context_payload=context)
        if matches:
            return raw_target, tuple(match.path for match in matches)
    return None


def _write_java_pair(tmp_path: Path, *, module: str, class_fqn: str, test_class_fqn: str) -> None:
    module_root = tmp_path / module if module else tmp_path
    main_file = module_root / "src" / "main" / "java" / Path(*class_fqn.split(".")).with_suffix(".java")
    test_file = module_root / "src" / "test" / "java" / Path(*test_class_fqn.split(".")).with_suffix(".java")
    main_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    main_package, main_simple = class_fqn.rsplit(".", 1)
    test_package, test_simple = test_class_fqn.rsplit(".", 1)
    main_file.write_text(f"package {main_package};\npublic class {main_simple} {{}}\n", encoding="utf-8")
    test_file.write_text(f"package {test_package};\npublic class {test_simple} {{}}\n", encoding="utf-8")
