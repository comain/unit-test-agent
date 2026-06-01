from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

import pytest
from uta.graph.workflow import build_workflow
from uta.graph.nodes import (
    ProviderRateLimitError,
    select_next_class,
    commit_to_branch,
    _session_progress_logger,
    _relax_surefire_skiptests,
    scan_and_select,
    _poll_with_continue_recovery,
    _upgrade_mockito,
    _mockito_api_guidance,
    store_and_push,
    _plan_needs_stricter_replan,
    _plan_breadth_replan_reason,
    _capture_phase_token_usage,
    parse_context,
    run_compile_fix_loop,
    generate_and_validate,
    _run_delegated_quality_gate_once,
    _run_generation_test_gate,
    _run_focused_mutation_fix_round,
    _run_focused_coverage_fix_round,
    _write_generation_plan,
    _write_generation_plan_candidate,
    _recover_plan_text_from_session_artifact,
    _clear_generation_plan,
    _llm_timeout,
    _is_accessor_like_method,
    _is_testable_class,
    _allowed_llm_path,
    _commit_ci_repair_to_branch,
)
from uta.engine.validation import BreadthResult, BreadthVerdict


def test_build_workflow():
    app = build_workflow()
    assert app is not None
    # We can't easily test execution without mocking everything,
    # but we can check the graph structure.
    # app.get_graph().print_ascii()


def test_select_next_class_single_and_batch():
    base = {
        "candidates": ["pkg.A", "pkg.B", "pkg.C"],
        "results": {},
    }
    out = select_next_class({**base, "classes_per_agent_run": 1})  # type: ignore[arg-type]
    assert out["finished"] is False
    assert out["current_batch"] == ["pkg.A"]
    assert out["current_class"] == "pkg.A"
    assert out["current_target"]["target_id"] == "pkg.A"
    assert out["current_target_batch"] == [
        {
            "language": "java",
            "target_id": "pkg.A",
            "display_name": "pkg.A",
            "granularity": "class",
            "symbol": "pkg.A",
        }
    ]

    out2 = select_next_class({**base, "classes_per_agent_run": 2})  # type: ignore[arg-type]
    assert out2["current_batch"] == ["pkg.A", "pkg.B"]
    assert [target["target_id"] for target in out2["current_target_batch"]] == ["pkg.A", "pkg.B"]

    done = select_next_class(
        {**base, "results": {"pkg.A": {}, "pkg.B": {}, "pkg.C": {}}, "classes_per_agent_run": 2}
    )  # type: ignore[arg-type]
    assert done["finished"] is True
    assert done["current_batch"] == []
    assert done["current_target"] is None
    assert done["current_target_batch"] == []


def test_select_next_class_smart_batches_simple_production_classes(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_simple_batch_size", 3)

    src = tmp_path / "biz" / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    (src / "A.java").write_text("package pkg;\npublic class A { public void a() {} }\n", encoding="utf-8")
    (src / "B.java").write_text("package pkg;\npublic class B { public void b() {} }\n", encoding="utf-8")

    out = select_next_class(
        {
            "repo_path": str(tmp_path),
            "module": "biz",
            "production": True,
            "coverage_gate": 80,
            "candidates": ["pkg.A", "pkg.B"],
            "results": {},
            "classes_per_agent_run": 1,
        }
    )  # type: ignore[arg-type]

    assert out["current_batch"] == ["pkg.A", "pkg.B"]


def test_select_next_class_smart_keeps_complex_class_single(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_complex_line_threshold", 100)

    src = tmp_path / "biz" / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    (src / "Huge.java").write_text(
        "package pkg;\npublic class Huge {\n"
        + "\n".join(f"  public void m{i}() {{}}" for i in range(5))
        + "\n}\n",
        encoding="utf-8",
    )
    (src / "Small.java").write_text("package pkg;\npublic class Small { public void s() {} }\n", encoding="utf-8")

    out = select_next_class(
        {
            "repo_path": str(tmp_path),
            "module": "biz",
            "production": True,
            "coverage_gate": 80,
            "candidates": ["pkg.Huge", "pkg.Small"],
            "results": {},
            "classes_per_agent_run": 1,
        }
    )  # type: ignore[arg-type]

    assert out["current_batch"] == ["pkg.Huge"]


def test_select_next_class_enables_smart_batching_for_ci_incremental(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.graph.nodes.uta_settings.smart_simple_batch_size", 3)

    src = tmp_path / "biz" / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    (src / "A.java").write_text("package pkg;\npublic class A { public void a() {} }\n", encoding="utf-8")
    (src / "B.java").write_text("package pkg;\npublic class B { public void b() {} }\n", encoding="utf-8")

    out = select_next_class(
        {
            "repo_path": str(tmp_path),
            "module": "biz",
            "quality_mode": "ci_incremental",
            "quality_gate_backend": "maven_enforcer",
            "coverage_gate": 95,
            "candidates": ["pkg.A", "pkg.B"],
            "results": {},
            "classes_per_agent_run": 1,
        }
    )  # type: ignore[arg-type]

    assert out["current_batch"] == ["pkg.A", "pkg.B"]


def test_select_next_class_does_not_restage_previous_finished_class(tmp_path):
    from uta.tasks.manager import TaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.Done", "pkg.Next"])
    done_row = manager.db.find_class_task(task_id, "pkg.Done")
    assert done_row is not None
    manager.db.update_class_task(
        done_row["id"],
        status="PASS",
        current_stage="finished",
        stage="finished",
        current_detail="PASS",
    )

    out = select_next_class(
        {
            "task_id": task_id,
            "task_db_path": str(db_path),
            "repo_path": str(repo),
            "candidates": ["pkg.Done", "pkg.Next"],
            "results": {},
            "current_batch": ["pkg.Done"],
            "current_class": "pkg.Done",
            "classes_per_agent_run": 1,
        }
    )  # type: ignore[arg-type]

    assert out["current_batch"] == ["pkg.Next"]
    refreshed = manager.db.find_class_task(task_id, "pkg.Done")
    assert refreshed["status"] == "PASS"
    assert refreshed["current_stage"] == "finished"
    assert refreshed["stage"] == "finished"


def test_generate_and_validate_skips_llm_when_existing_tests_meet_gates(monkeypatch, tmp_path):
    test_root = tmp_path / "src" / "test" / "java" / "pkg"
    test_root.mkdir(parents=True)
    (test_root / "ATest.java").write_text("class ATest { @org.junit.Test public void ok() {} }", encoding="utf-8")
    (test_root / "BTest.java").write_text("class BTest { @org.junit.Test public void ok() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "existing ok"))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr(
        "uta.graph.nodes.parse_surefire_results",
        lambda *args, **kwargs: {"ATest": {"passed": True, "output": ""}, "BTest": {"passed": True, "output": ""}},
    )
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 100.0})
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (True, "pit ok"))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: "pit.xml")
    monkeypatch.setattr(
        "uta.graph.nodes.compute_mutation_stats",
        lambda report, class_fqn: {
            "score": 100.0,
            "survived": 0,
            "total": 1,
            "killed": 1,
            "no_coverage": 0,
            "timed_out": 0,
            "non_viable": 0,
            "memory_error": 0,
            "run_error": 0,
            "status_counts": {"KILLED": 1},
        },
    )

    out = generate_and_validate(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "graph": None,
            "flows": [],
            "session_id": None,
            "session_ids": [],
            "coverage_gate": 80,
            "mutation_gate": 70,
            "current_batch": ["pkg.A", "pkg.B"],
            "current_class": "pkg.A",
            "results": {},
            "phase_timings": {},
        }
    )

    assert out["current_stage"] == "precheck_existing_tests"
    assert out["current_batch"] == []
    assert out["results"]["pkg.A"]["status"] == "PASS"
    assert out["results"]["pkg.B"]["status"] == "PASS"
    assert out["results"]["pkg.A"]["precheck_existing_tests"] is True
    assert out["results"]["pkg.A"]["generation_seconds"] == 0.0


def test_generate_and_validate_ci_incremental_precheck_passes_without_llm(monkeypatch, tmp_path):
    test_root = tmp_path / "src" / "test" / "java" / "pkg"
    test_root.mkdir(parents=True)
    (test_root / "ATest.java").write_text("class ATest {}", encoding="utf-8")

    def fail_if_instantiated(*args, **kwargs):
        raise AssertionError("ci incremental precheck pass should not start OpenCode")

    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", fail_if_instantiated)
    monkeypatch.setattr(
        "uta.graph.nodes._run_delegated_quality_gate_once",
        lambda *args, **kwargs: {
            "passed": True,
            "status": "passed",
            "summary": "test-enforcement passed",
            "command": ["mvn", "verify"],
            "stdout": (
                "[INFO] [test-enforcer] diff line coverage 100.00% passed for biz (16/16)\n"
                "[INFO] [test-enforcer] diff mutation score 100.00% passed for biz (5/5 detected)"
            ),
            "stderr": "",
        },
    )
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)

    out = generate_and_validate(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "graph": None,
            "flows": [],
            "session_id": None,
            "session_ids": [],
            "coverage_gate": 80,
            "mutation_gate": 70,
            "quality_mode": "ci_incremental",
            "quality_gate_backend": "maven_enforcer",
            "current_batch": ["pkg.A"],
            "current_class": "pkg.A",
            "results": {},
            "phase_timings": {},
        }
    )

    result = out["results"]["pkg.A"]
    assert out["current_stage"] == "precheck_existing_tests"
    assert out["current_batch"] == []
    assert out["finished"] is True
    assert out["stopped_early"] is True
    assert result["status"] == "PASS"
    assert result["coverage"] == 100.0
    assert result["mutation_score"] == 100.0
    assert result["precheck_existing_tests"] is True
    assert result["generation_seconds"] == 0.0


def test_generate_and_validate_ci_incremental_failed_precheck_repairs_from_enforcer_evidence(monkeypatch, tmp_path):
    repo = tmp_path
    test_file = repo / "biz" / "src" / "test" / "java" / "pkg" / "ATest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("package pkg; class ATest {}", encoding="utf-8")

    class DummyClient:
        def __init__(self, repo_path=None):
            self.created = []
            self.sent = []

        def create_session(self, model_id=None, permission=None):
            session_id = f"repair-{len(self.created) + 1}"
            self.created.append((session_id, model_id, permission))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            raise AssertionError("ci incremental repair should skip plan/generation")

    failed_gate = {
        "passed": False,
        "status": "failed",
        "summary": "test-enforcement failed",
        "command": ["mvn", "verify"],
        "stdout": (
            "[INFO] [test-enforcer] diff line coverage 100.00% passed for biz (16/16)\n"
            "[ERROR] [test-enforcer] diff mutation score 60.00% failed for biz (3/5 detected)"
        ),
        "stderr": "",
    }
    passed_gate = {
        "passed": True,
        "status": "passed",
        "summary": "test-enforcement passed",
        "command": ["mvn", "verify"],
        "stdout": (
            "[INFO] [test-enforcer] diff line coverage 100.00% passed for biz (16/16)\n"
            "[INFO] [test-enforcer] diff mutation score 100.00% passed for biz (5/5 detected)"
        ),
        "stderr": "",
    }
    gate_results = iter([failed_gate, passed_gate])
    client = DummyClient()

    monkeypatch.setattr(
        "uta.graph.nodes.ContextBuilder",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ci precheck repair should skip context export")),
    )
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *args, **kwargs: client)
    monkeypatch.setattr("uta.graph.nodes._run_delegated_quality_gate_once", lambda *args, **kwargs: next(gate_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_phase_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)

    out = generate_and_validate(
        {
            "repo_path": str(repo),
            "module": "biz",
            "graph": SimpleNamespace(nodes={}),
            "flows": [],
            "session_id": "ses-1",
            "session_ids": [],
            "coverage_gate": 80,
            "mutation_gate": 70,
            "quality_mode": "ci_incremental",
            "quality_gate_backend": "maven_enforcer",
            "current_batch": ["pkg.A"],
            "current_class": "pkg.A",
            "results": {},
            "phase_timings": {},
            "session_retrospect": {},
            "session_token_usage": {},
        }
    )

    result = out["results"]["pkg.A"]
    assert len(client.created) == 1
    assert client.created[0][0] == "repair-1"
    assert client.created[0][2] is None
    assert len(client.sent) == 1
    assert "TEST-ENFORCEMENT FEEDBACK" in client.sent[0][1]
    assert "diff mutation score 60.00% failed" in client.sent[0][1]
    assert result["status"] == "PASS"
    assert result["coverage"] == 100.0
    assert result["mutation_score"] == 100.0
    assert result["session_id"] == "repair-1"
    assert result["session_ids"] == ["repair-1"]


def test_commit_to_branch_stages_tests_and_cache_without_reports(monkeypatch, tmp_path):
    repo = tmp_path
    test_file = repo / "biz" / "src" / "test" / "java" / "pkg" / "ATest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class ATest {}", encoding="utf-8")
    (repo / ".uta_cache").mkdir()
    (repo / ".uta_reports").mkdir()
    (repo / ".uta_reports" / "live_status.json").write_text("{}", encoding="utf-8")

    calls = []

    def fake_git_run(repo_path, *args, **kwargs):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "deadbeef\n"
            stderr = ""

        return R()

    monkeypatch.setattr("uta.graph.nodes._git_run", fake_git_run)

    commit_to_branch(
        {
            "repo_path": str(repo),
            "current_batch": ["pkg.A"],
            "results": {"pkg.A": {"test_file_path": "biz/src/test/java/pkg/ATest.java", "status": "PASS", "coverage": 100.0}},
        }
    )

    add_calls = [args for args in calls if args and args[0] == "add"]
    assert add_calls
    staged = add_calls[0][1:]
    assert "biz/src/test/java/pkg/ATest.java" in staged
    assert ".uta_cache/" in staged
    assert ".uta_reports/" not in staged


def test_commit_to_branch_allows_missing_coverage(monkeypatch, tmp_path):
    repo = tmp_path
    test_file = repo / "biz" / "src" / "test" / "java" / "pkg" / "ATest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class ATest {}", encoding="utf-8")

    calls = []

    def fake_git_run(repo_path, *args, **kwargs):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "deadbeef\n"
            stderr = ""

        return R()

    monkeypatch.setattr("uta.graph.nodes._git_run", fake_git_run)

    commit_to_branch(
        {
            "repo_path": str(repo),
            "current_batch": ["pkg.A"],
            "results": {"pkg.A": {"test_file_path": "biz/src/test/java/pkg/ATest.java", "status": "PASS", "coverage": None}},
        }
    )

    commit_calls = [args for args in calls if args and args[0] == "commit"]
    assert commit_calls
    commit_command = " ".join(commit_calls[0])
    assert "ATest" not in commit_command
    assert "A[PASS,n/a]" in commit_command


def test_allowed_llm_path_accepts_python_uta_generated_tests():
    assert _allowed_llm_path(
        "tests/uta_generated/test_jobs_forecast.py",
        {"language": "python"},
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )
    assert _allowed_llm_path(
        ".coverage",
        {"language": "python"},
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )
    assert _allowed_llm_path(
        ".pytest_cache/v/cache/nodeids",
        {"language": "python"},
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )
    assert _allowed_llm_path(
        "mutants/src/jobs/forecast.py",
        {"language": "python"},
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )
    assert not _allowed_llm_path(
        "jobs/forecast.py",
        {"language": "python"},
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )


def test_ci_repair_no_changes_reuses_existing_pushed_commit(monkeypatch, tmp_path):
    from uta.ci_plugin.auto_push import AutoPushContext, AutoPushPolicyError, CiAutoPusher

    class FakeManager:
        def __init__(self):
            self.commits = []
            self.synced = []

        def get_task(self, task_id):
            return {"latest_commit": "abc123", "remote_ref": "abc123"}

        def record_commit(self, task_id, **kwargs):
            self.commits.append((task_id, kwargs))

        def sync_results(self, task_id, results, **kwargs):
            self.synced.append((task_id, results, kwargs))

    def no_changes(self, context):
        raise AutoPushPolicyError("CI repair auto-push found no test changes to commit")

    monkeypatch.setattr(CiAutoPusher, "commit_and_push", no_changes)

    manager = FakeManager()
    out = _commit_ci_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": None,
            "results": {"pkg.A": {"status": "PASS", "coverage": None}},
        },
        manager=manager,
        context=AutoPushContext(branch_name="feature/EXAMPLE-1"),
        class_fqns=["pkg.A"],
        results={"pkg.A": {"status": "PASS", "coverage": None}},
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert manager.commits[0][1]["commit_sha"] == "abc123"
    assert manager.synced[0][1]["pkg.A"]["status"] == "PASS"


def test_store_and_push_saves_report_without_committing_reports(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "push disabled"

    def fake_git_run(repo_path, *args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr("uta.graph.nodes._git_run", fake_git_run)
    monkeypatch.setattr("uta.graph.nodes._push_branch_with_rebase_retry", lambda *args, **kwargs: Result())

    store_and_push(
        {
            "repo_path": str(tmp_path),
            "results": {
                "pkg.A": {
                    "status": "PASS",
                    "coverage": 100.0,
                    "tests_pass": True,
                    "mutation_score": 0.0,
                    "surviving_mutants": 0,
                    "test_file_path": "src/test/java/pkg/ATest.java",
                }
            },
            "branch_name": "unit-code-gen",
            "module": "biz",
            "started_at": 0,
        }
    )

    assert list((tmp_path / ".uta_reports").glob("summary_biz_*.json"))
    assert not any(args[:2] == ("add", ".uta_reports/") for args in calls)
    assert not any(args and args[0] == "commit" and "uta: reports" in args for args in calls)


def test_llm_timeout_keeps_non_deepseek_budget(monkeypatch):
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_provider", "openai")
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_deepseek_timeout_multiplier", 2.0)

    assert _llm_timeout(240, "openai/gpt-5.4") == 240


def test_llm_timeout_doubles_deepseek_budget(monkeypatch):
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_provider", "deepseek")
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_deepseek_timeout_multiplier", 2.0)

    assert _llm_timeout(240, "deepseek/deepseek-v4-pro") == 480


def test_scan_and_select_uses_explicit_class_override():
    out = scan_and_select(
        {
            "repo_path": "/tmp/repo",
            "days": 30,
            "module": "biz",
            "max_files": 10,
            "select_all_files": False,
            "explicit_class_fqns": ["pkg.A", "pkg.B", "pkg.C"],
            "phase_timings": {},
        }  # type: ignore[arg-type]
    )

    assert out["candidates"] == ["pkg.A", "pkg.B", "pkg.C"]
    assert out["current_stage"] == "scan_candidates"


def test_scan_and_select_all_files_bypasses_git_history(monkeypatch):
    monkeypatch.setattr(
        "uta.graph.nodes.get_all_java_files",
        lambda repo_path, module: [
            ("biz/src/main/java/pkg/A.java", 1),
            ("biz/src/main/java/pkg/B.java", 1),
        ],
    )
    monkeypatch.setattr(
        "uta.graph.nodes.get_changed_java_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("git scan should not run")),
    )

    out = scan_and_select(
        {
            "repo_path": "/tmp/repo",
            "days": 30,
            "module": "biz",
            "max_files": 1,
            "select_all_files": True,
            "explicit_class_fqns": [],
            "phase_timings": {},
        }  # type: ignore[arg-type]
    )

    assert out["candidates"] == [
        "biz/src/main/java/pkg/A.java",
        "biz/src/main/java/pkg/B.java",
    ]


def _testability_graph(
    fqn: str,
    path: str,
    method_names: list[str],
    method_complexity: Optional[Dict[str, dict]] = None,
    method_annotations: Optional[Dict[str, list[str]]] = None,
):
    nodes = {
        fqn: SimpleNamespace(
            kind="class",
            file_path=path,
            metadata={"annotations": [], "modifiers": []},
        )
    }
    for method_name in method_names:
        nodes[f"{fqn}.{method_name}"] = SimpleNamespace(
            kind="method",
            fqn=f"{fqn}.{method_name}",
            metadata={
                "parent_fqn": fqn,
                "modifiers": ["public"],
                "complexity": (method_complexity or {}).get(method_name),
                "annotations": (method_annotations or {}).get(method_name, []),
            },
        )
    return SimpleNamespace(nodes=nodes)


def test_is_testable_class_allows_one_method_business_handler():
    fqn = "com.example.biz.handler.OrderFinishedHandler"
    graph = _testability_graph(
        fqn,
        "/repo/biz/src/main/java/com/example/biz/handler/OrderFinishedHandler.java",
        ["handle"],
    )

    assert _is_testable_class(fqn, graph) is True


def test_is_testable_class_rejects_accessor_only_data_class():
    fqn = "com.example.model.OrderMessage"
    graph = _testability_graph(
        fqn,
        "/repo/model/src/main/java/com/example/model/OrderMessage.java",
        ["getId", "setId", "toString"],
    )

    assert _is_testable_class(fqn, graph) is False


def test_parse_context_honors_explicit_class_even_when_low_signal(monkeypatch, tmp_path):
    fqn = "com.example.model.OrderMessage"
    graph = _testability_graph(
        fqn,
        str(tmp_path / "src/main/java/com/example/model/OrderMessage.java"),
        ["getId", "setId", "toString"],
    )

    class DummyParseResult:
        flows = []

        def __init__(self):
            self.graph = graph

        def contains_target(self, target_id):
            return target_id in graph.nodes

        def target_id_for_source_path(self, source_path):
            return fqn if source_path == fqn else None

        def is_testable_target(self, target_id):
            return True

        def target_selections(self, target_ids):
            return [{"language": "java", "target_id": target_id, "display_name": target_id, "granularity": "class"} for target_id in target_ids]

    class DummyParseProvider:
        def parse_project(self, _request):
            return DummyParseResult()

    class DummyContextBuilder:
        def __init__(self, *_args):
            pass

        def export_context_files(self):
            pass

    monkeypatch.setattr("uta.graph.nodes.make_parse_provider", lambda _language: DummyParseProvider())
    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *_args, **_kwargs: None)

    out = parse_context(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "candidates": [fqn],
            "explicit_class_fqns": [fqn],
            "phase_timings": {},
        }
    )

    assert out["candidates"] == [fqn]
    assert out["target_candidates"][0]["target_id"] == fqn


def test_is_testable_class_rejects_non_business_single_method_class():
    fqn = "com.example.misc.SmallHelper"
    graph = _testability_graph(
        fqn,
        "/repo/common/src/main/java/com/example/misc/SmallHelper.java",
        ["doIt"],
    )

    assert _is_testable_class(fqn, graph) is False


def test_is_testable_class_rejects_one_method_actor_wrapper():
    fqn = "com.example.biz.actor.inbound.InboundFinished4ContainerLdc"
    graph = _testability_graph(
        fqn,
        "/repo/biz/src/main/java/com/example/biz/actor/inbound/InboundFinished4ContainerLdc.java",
        ["onMessage"],
    )

    assert _is_testable_class(fqn, graph) is False


def test_is_testable_class_allows_complex_one_method_actor():
    fqn = "com.example.biz.actor.inbound.BranchHeavyActor"
    graph = _testability_graph(
        fqn,
        "/repo/biz/src/main/java/com/example/biz/actor/inbound/BranchHeavyActor.java",
        ["onMessage"],
        method_complexity={
            "onMessage": {
                "cyclomatic_approx": 5,
                "body_lines": 36,
                "branches": 3,
                "loops": 1,
            }
        },
    )

    assert _is_testable_class(fqn, graph) is True


def test_is_testable_class_rejects_accessor_backed_thin_delegator():
    fqn = "com.example.biz.ReceiptFinishedManager"
    graph = _testability_graph(
        fqn,
        "/repo/biz/src/main/java/com/example/biz/ReceiptFinishedManager.java",
        ["getHandler", "setHandler", "handler"],
        method_complexity={
            "handler": {
                "cyclomatic_approx": 1,
                "body_lines": 3,
                "branches": 0,
                "loops": 0,
            }
        },
    )

    assert _is_testable_class(fqn, graph) is False


def test_is_testable_class_rejects_data_path_even_with_helper_method():
    fqn = "com.example.model.ProductDateCalculateContextBuilder"
    graph = _testability_graph(
        fqn,
        "/repo/model/src/main/java/com/example/model/ProductDateCalculateContextBuilder.java",
        ["build", "getContext", "setContext"],
    )

    assert _is_testable_class(fqn, graph) is False


def test_is_testable_class_rejects_delegate_only_registration_wrapper():
    fqn = "com.example.provider.listener.MessageConsumerRegister"
    methods = ["receiptUpFlowListener", "batchReceiptUpFlowListener", "orderExpireListener"]
    graph = _testability_graph(
        fqn,
        "/repo/provider/src/main/java/com/example/provider/listener/MessageConsumerRegister.java",
        methods,
        method_complexity={
            method: {
                "cyclomatic_approx": 1,
                "body_lines": 3,
                "branches": 0,
                "loops": 0,
            }
            for method in methods
        },
        method_annotations={method: ["MessageConsumer"] for method in methods},
    )

    assert _is_testable_class(fqn, graph) is False


def test_accessor_detection_keeps_complex_get_named_business_method():
    graph = _testability_graph(
        "com.example.service.ReturnWriter",
        "/repo/service/src/main/java/com/example/service/ReturnWriter.java",
        ["getExtendInfo"],
        method_complexity={
            "getExtendInfo": {
                "cyclomatic_approx": 4,
                "body_lines": 26,
                "branches": 3,
                "loops": 0,
                "external_calls": 20,
            }
        },
    )
    method = graph.nodes["com.example.service.ReturnWriter.getExtendInfo"]

    assert _is_accessor_like_method(method) is False


def test_poll_with_continue_recovery_resends_once():
    class DummyClient:
        def __init__(self):
            self.sent = []
            self.events = [
                {"type": "stalled_after_recovery", "result": "", "reason": "no session progress"},
                {"type": "completed", "result": "ok"},
            ]

        def poll_completion(self, session_id, timeout=0, on_update=None, **kwargs):
            return self.events.pop(0)

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    updates = []
    out = _poll_with_continue_recovery(
        client=client,
        session_id="ses-1",
        timeout=300,
        phase="generate",
        batch=["pkg.A"],
        on_update=updates.append,
    )

    assert out["type"] == "completed"
    assert len(client.sent) == 1
    assert "Resume the interrupted generation work" in client.sent[0][1]
    assert any("guarded continue prompt" in line for line in updates)


def test_poll_with_continue_recovery_resends_once_for_stalled_no_progress():
    class DummyClient:
        def __init__(self):
            self.sent = []
            self.events = [
                {"type": "stalled_no_progress", "result": "", "reason": "no session progress"},
                {"type": "completed", "result": "ok"},
            ]

        def poll_completion(self, session_id, timeout=0, on_update=None, **kwargs):
            return self.events.pop(0)

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    updates = []
    out = _poll_with_continue_recovery(
        client=client,
        session_id="ses-1",
        timeout=300,
        phase="compile_fix",
        batch=["pkg.A"],
        on_update=updates.append,
    )

    assert out["type"] == "completed"
    assert len(client.sent) == 1
    assert "Resume the interrupted compile-fix work" in client.sent[0][1]
    assert any("guarded continue prompt" in line for line in updates)


def test_poll_with_continue_recovery_materializes_candidate_plan_for_plan_phase(tmp_path):
    repo = tmp_path / "repo"
    _write_generation_plan_candidate(
        str(repo),
        "session-123",
        ["com.example.Sample"],
        "## com.example.Sample\n\nPLANNED TESTS\n- testFoo",
        ["Need stronger estimated reach"],
    )

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.events = [
                {"type": "stalled_no_progress", "result": "", "reason": "no session progress"},
                {"type": "completed", "result": "ok"},
            ]

        def poll_completion(self, session_id, timeout=0, on_update=None, **kwargs):
            return self.events.pop(0)

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

    client = DummyClient()

    out = _poll_with_continue_recovery(
        client=client,
        session_id="ses-1",
        timeout=300,
        phase="plan",
        batch=["com.example.Sample"],
        repo_path=str(repo),
    )

    assert out["type"] == "completed"
    plan_path = repo / ".uta_cache" / "context" / "latest_generation_plan.md"
    assert plan_path.exists()
    plan_text = plan_path.read_text(encoding="utf-8")
    assert "# Latest Generation Plan" in plan_text
    assert "PLANNED TESTS" in plan_text
    assert "Need stronger estimated reach" not in plan_text
    assert len(client.sent) == 1
    assert "latest_generation_plan.md" in client.sent[0][1]


def test_poll_with_continue_recovery_forwards_stalled_no_progress_override():
    class DummyClient:
        def __init__(self):
            self.poll_kwargs = []
            self.sent = []
            self.events = [
                {"type": "stalled_no_progress", "result": "", "reason": "no session progress"},
                {"type": "completed", "result": "ok"},
            ]

        def poll_completion(self, session_id, timeout=0, on_update=None, **kwargs):
            self.poll_kwargs.append(kwargs)
            return self.events.pop(0)

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

    client = DummyClient()

    out = _poll_with_continue_recovery(
        client=client,
        session_id="ses-1",
        timeout=300,
        phase="coverage_fix",
        batch=["pkg.A"],
        stalled_no_progress_seconds=120,
    )

    assert out["type"] == "completed"
    assert client.poll_kwargs == [
        {"stalled_no_progress_seconds": 120},
        {"stalled_no_progress_seconds": 120},
    ]
    assert len(client.sent) == 1


def test_run_compile_fix_loop_uses_fresh_session(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "compile-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    compile_results = iter([(False, "boom"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._compile_test", lambda *args, **kwargs: next(compile_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "fixed"})

    client = DummyClient()
    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Sample"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        target_context_paths={"com.example.Sample": {"context_abs": "/tmp/context.md", "symbols_abs": "/tmp/symbols.md"}},
        max_fix_attempts=2,
    )

    assert ok is True
    assert repair_session_id == "compile-fix-session"
    assert client.events[0][0] == "create_session"
    assert client.events[1][0] == "send_message"
    assert client.events[1][1] == "compile-fix-session"
    assert "previous generation session: `generation-session`" in client.events[1][3]


def test_run_compile_fix_loop_uses_new_session_after_stalled_turn(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []
            self.created = 0

        def create_session(self, model_id=None):
            self.created += 1
            session_id = f"compile-fix-session-{self.created}"
            self.events.append(("create_session", model_id, session_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    compile_results = iter([(False, "boom"), (False, "boom again"), (True, "")])
    poll_events = iter([
        {"type": "stalled_no_progress", "result": "", "reason": "no session progress"},
        {"type": "completed", "result": "fixed"},
    ])
    monkeypatch.setattr("uta.graph.nodes._compile_test", lambda *args, **kwargs: next(compile_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))

    client = DummyClient()
    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Sample"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        target_context_paths={"com.example.Sample": {"context_abs": "/tmp/context.md", "symbols_abs": "/tmp/symbols.md"}},
        max_fix_attempts=3,
    )

    assert ok is True
    assert repair_session_id == "compile-fix-session-2"
    assert [event[2] for event in client.events if event[0] == "create_session"] == [
        "compile-fix-session-1",
        "compile-fix-session-2",
    ]


def test_run_generation_test_gate_uses_fresh_session(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "generation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    test_results = iter([(False, "failure"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "fixed"})

    client = DummyClient()
    ok, _, repair_session_id = _run_generation_test_gate(
        state={},
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Sample"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert repair_session_id == "generation-test-fix-session"
    assert client.events[0][0] == "create_session"
    assert client.events[1][0] == "send_message"
    assert client.events[1][1] == "generation-test-fix-session"
    assert "previous generation session: `generation-session`" in client.events[1][3]
    assert "--section fix_summary" in client.events[1][3]


def test_run_generation_test_gate_refreshes_full_surefire_failure_set(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "generation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    test_results = iter([(False, "raw tail"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr(
        "uta.graph.nodes.parse_surefire_results",
        lambda *args, **kwargs: {
            "AlphaTest": {"passed": False, "output": "pkg.AlphaTest.shouldA: failure\nalpha detail"},
            "BetaTest": {"passed": False, "output": "pkg.BetaTest.shouldB: error\nbeta detail"},
        },
    )
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "fixed"})

    client = DummyClient()
    ok, _, repair_session_id = _run_generation_test_gate(
        state={},
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Alpha", "com.example.Beta"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert repair_session_id == "generation-test-fix-session"
    prompt = client.events[1][3]
    assert "## AlphaTest" in prompt
    assert "alpha detail" in prompt
    assert "## BetaTest" in prompt
    assert "--section fix_summary" in prompt
    assert "beta detail" in prompt
    assert "Fix the full current failing suite below" in prompt


def test_run_generation_test_gate_does_not_consume_fix_budget_on_rate_limit(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "generation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    test_results = iter([(False, "failure"), (False, "still failing"), (True, "")])
    poll_events = iter([
        {"type": "rate_limited", "rate_limit": {"provider_id": "openai", "model_id": "gpt-5.4", "message": "busy", "retry_after_seconds": 30}},
        {"type": "completed", "result": "fixed"},
    ])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))

    client = DummyClient()
    ok, _, repair_session_id = _run_generation_test_gate(
        state={},
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Sample"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert repair_session_id == "generation-test-fix-session"
    assert [event[0] for event in client.events].count("send_message") == 2


def test_run_generation_test_gate_raises_after_repeated_rate_limits(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "generation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: (False, "failure"))
    monkeypatch.setattr(
        "uta.graph.nodes._poll_with_continue_recovery",
        lambda *args, **kwargs: {
            "type": "rate_limited",
            "rate_limit": {
                "provider_id": "openai",
                "model_id": "gpt-5.4",
                "message": "The usage limit has been reached",
                "retry_after_seconds": 300,
            },
        },
    )

    client = DummyClient()
    with pytest.raises(ProviderRateLimitError):
        _run_generation_test_gate(
            state={},
            repo_path="/tmp/repo",
            module="biz",
            batch=["com.example.Sample"],
            generation_session_id="generation-session",
            client=client,
            maven_module_flag=" -pl biz -am",
            max_fix_attempts=2,
        )


def test_run_generation_test_gate_continues_same_session_after_failed_rerun(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []
            self.patch_count = 0

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "generation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def get_messages(self, session_id):
            return [{"parts": [{"type": "patch"} for _ in range(self.patch_count)]}]

    test_results = iter(
        [
            (False, "initial failure"),
            (False, "still failing after first repair"),
            (True, ""),
        ]
    )

    def fake_poll(*args, **kwargs):
        client.patch_count += 1
        return {"type": "completed", "result": "fixed"}

    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", fake_poll)

    client = DummyClient()
    ok, _, repair_session_id = _run_generation_test_gate(
        state={},
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Sample"],
        generation_session_id="generation-session",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=1,
    )

    assert ok is True
    assert repair_session_id == "generation-test-fix-session"
    send_prompts = [event[3] for event in client.events if event[0] == "send_message"]
    assert len(send_prompts) == 2
    assert "The latest targeted rerun" in send_prompts[1]
    assert "REMAINING TEST FAILURES" in send_prompts[1]


def test_test_fix_prompt_uses_configured_model(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    (repo / ".uta_cache" / "context").mkdir(parents=True)
    (repo / ".uta_cache" / "context" / "project_summary.md").write_text("summary")
    (repo / ".uta_cache" / "context" / "test_generation_guidance.md").write_text("guidance")
    (repo / ".uta_cache" / "context" / "class_map.md").write_text("class map")
    (repo / ".uta_cache" / "context" / "dependency_map.md").write_text("dependency map")
    (repo / "biz" / "src" / "test" / "java" / "com" / "example").mkdir(parents=True, exist_ok=True)
    (repo / "biz" / "src" / "test" / "java" / "com" / "example" / "SampleTest.java").write_text("class SampleTest {}")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return repo / ".uta_cache" / "context"

        def get_class_source_path(self, class_fqn):
            return "biz/src/main/java/com/example/Sample.java"

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "plan ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(repo / ".uta_cache" / "context" / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    test_runs = iter([(False, "boom"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_runs))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SampleTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (False, 0.0, "", []))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 70,
        "current_batch": ["com.example.Sample"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    generate_and_validate(state)

    assert any(
        model_id == "openai/gpt-5.4" and "now compiles but the targeted test run still fails" in prompt
        for _, prompt, model_id in client.sent
    )


def test_generate_and_validate_uses_single_batch_jacoco_run(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")

    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "AlphaTest.java").write_text("class AlphaTest {}", encoding="utf-8")
    (test_root / "BetaTest.java").write_text("class BetaTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            simple = class_fqn.split(".")[-1]
            return f"biz/src/main/java/com/example/{simple}.java"

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    batch_calls = {"count": 0}
    single_calls = {"count": 0}

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: batch_calls.__setitem__("count", batch_calls["count"] + 1) or (True, "batch ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {
        "AlphaTest": {"passed": True, "output": ""},
        "BetaTest": {"passed": True, "output": ""},
    })
    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", lambda *args, **kwargs: single_calls.__setitem__("count", single_calls["count"] + 1) or (True, "single"))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 90.0 if class_fqn.endswith("Alpha") else 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Alpha", "com.example.Beta"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert batch_calls["count"] == 1
    assert single_calls["count"] == 0
    assert out["results"]["com.example.Alpha"]["status"] == "PASS"
    assert out["results"]["com.example.Beta"]["status"] == "PASS"


def test_delegated_quality_gate_uses_ci_command(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=1,
            stdout="test-enforcer check-coverage failed: diff line coverage 90.00% is below required 95.00%",
            stderr="",
        )

    result = _run_delegated_quality_gate_once(
        {
            "ci_context": {
                "enforcement": {
                    "command": [
                        "mvn",
                        "-U",
                        "-Dtest.enforcement.enabled=true",
                        "verify",
                        "-DtargetTests=com.example.SampleTest",
                    ]
                }
            }
        },
        str(tmp_path),
        run_command=fake_run,
    )

    assert calls[0][0][:4] == ["mvn", "-U", "-Dtest.enforcement.enabled=true", "verify"]
    assert result["passed"] is False
    assert result["summary"] == "test-enforcement failed"


def test_generate_and_validate_marks_delegated_gate_failed_when_enforcement_still_fails(monkeypatch, tmp_path):
    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")

    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SampleTest.java").write_text("class SampleTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return "biz/src/main/java/com/example/Sample.java"

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "batch ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SampleTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 100.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("builtin coverage fix should not run for delegated gates")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("builtin pit should not run for delegated gates")))
    monkeypatch.setattr("uta.graph.nodes._run_delegated_quality_gate_fix_loop", lambda **kwargs: (False, {"passed": False, "summary": "test-enforcement failed", "stdout": "diff coverage below gate", "stderr": ""}, 0.1, []))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": SimpleNamespace(nodes={}),
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 95,
        "mutation_gate": 100,
        "quality_mode": "ci_incremental",
        "quality_gate_backend": "maven_enforcer",
        "current_batch": ["com.example.Sample"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Sample"]["status"] == "FAIL"
    assert out["results"]["com.example.Sample"]["delegated_quality_gate"]["summary"] == "test-enforcement failed"
    assert "diff coverage below gate" in out["results"]["com.example.Sample"]["output"]
    assert "current_batch" not in out
    assert "current_class" not in out


def test_generate_and_validate_stops_before_jacoco_when_generation_test_gate_fails(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return "biz/src/main/java/com/example/Solo.java"

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    batch_calls = {"count": 0}

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (False, 12.5, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: batch_calls.__setitem__("count", batch_calls["count"] + 1) or (True, "batch ok"))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert batch_calls["count"] == 0
    assert out["results"]["com.example.Solo"]["status"] == "TEST_FAIL"


def test_run_focused_mutation_fix_round_uses_fresh_session(monkeypatch, tmp_path):
    repo = tmp_path
    test_file = repo / "SampleServiceTest.java"
    test_file.write_text("public class SampleServiceTest {}", encoding="utf-8")
    source_file = repo / "SampleService.java"
    source_file.write_text("public class SampleService {}", encoding="utf-8")
    report = repo / "mutations.xml"
    report.write_text(
        """<mutations>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <lineNumber>42</lineNumber>
    <description>changed conditional boundary</description>
  </mutation>
</mutations>""",
        encoding="utf-8",
    )

    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "focused-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def get_messages(self, session_id):
            return [
                {
                    "parts": [
                        {"type": "patch", "path": "SampleServiceTest.java"},
                    ]
                }
            ]

        def delete_session(self, session_id):
            self.events.append(("delete_session", session_id))

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})

    result = _run_focused_mutation_fix_round(
        repo_path=str(repo),
        module="biz",
        class_fqn="com.example.service.SampleService",
        session_client=client,
        source_file_abs=source_file,
        test_file_abs=test_file,
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=82.8,
        mutation_gate_score=70,
        attempt=1,
        mutation_score=45.1,
        mutation_stats={"total": 10, "killed": 4, "survived": 6},
        report_path=str(report),
    )

    assert result["session_id"] == "focused-session"
    assert result["patched"] is True
    assert client.events[0][0] == "create_session"
    assert client.events[1][0] == "send_message"
    assert client.events[1][1] == "focused-session"
    assert "SCRIPT-EXTRACTED MUTATION FAMILIES" in client.events[1][3]
    assert "Mutation family summary" in client.events[1][3]
    assert ("delete_session", "focused-session") not in client.events


def test_run_focused_mutation_fix_round_marks_diagnosis_only_when_no_patch(monkeypatch, tmp_path):
    repo = tmp_path
    test_file = repo / "SampleServiceTest.java"
    test_file.write_text("public class SampleServiceTest {}", encoding="utf-8")
    source_file = repo / "SampleService.java"
    source_file.write_text("public class SampleService {}", encoding="utf-8")
    report = repo / "mutations.xml"
    report.write_text(
        """<mutations>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>metricsCounter</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.VoidMethodCallMutator</mutator>
    <lineNumber>42</lineNumber>
    <description>removed call to io.micrometer.core.instrument.Counter::increment</description>
  </mutation>
</mutations>""",
        encoding="utf-8",
    )

    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "focused-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def get_messages(self, session_id):
            return [{"parts": [{"type": "text", "text": "diagnosis only"}]}]

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})

    result = _run_focused_mutation_fix_round(
        repo_path=str(repo),
        module="biz",
        class_fqn="com.example.service.SampleService",
        session_client=client,
        source_file_abs=source_file,
        test_file_abs=test_file,
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=82.8,
        mutation_gate_score=70,
        attempt=1,
        mutation_score=45.1,
        mutation_stats={"total": 10, "killed": 4, "survived": 6},
        report_path=str(report),
    )

    assert result["patched"] is False
    assert result["ranked_methods"] == ["metricsCounter"]


def test_run_focused_mutation_fix_round_logs_timeout(monkeypatch, tmp_path, caplog):
    repo = tmp_path
    test_file = repo / "SampleServiceTest.java"
    test_file.write_text("public class SampleServiceTest {}", encoding="utf-8")
    source_file = repo / "SampleService.java"
    source_file.write_text("public class SampleService {}", encoding="utf-8")
    report = repo / "mutations.xml"
    report.write_text(
        """<mutations>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>metricsCounter</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.VoidMethodCallMutator</mutator>
    <lineNumber>42</lineNumber>
    <description>removed call</description>
  </mutation>
</mutations>""",
        encoding="utf-8",
    )

    class DummyClient:
        def create_session(self, model_id=None):
            return "focused-session"

        def send_message(self, session_id, prompt, model_id=None):
            pass

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def get_messages(self, session_id):
            return []

    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "timeout", "result": ""})
    caplog.set_level("WARNING")

    result = _run_focused_mutation_fix_round(
        repo_path=str(repo),
        module="biz",
        class_fqn="com.example.service.SampleService",
        session_client=DummyClient(),
        source_file_abs=source_file,
        test_file_abs=test_file,
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=82.8,
        mutation_gate_score=70,
        attempt=1,
        mutation_score=45.1,
        mutation_stats={"total": 10, "killed": 4, "survived": 6},
        report_path=str(report),
    )

    assert result["event_type"] == "timeout"
    assert "Focused mutation-fix session focused-session ended with timeout" in caplog.text


def test_run_focused_coverage_fix_round_uses_fresh_session(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "coverage-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def delete_session(self, session_id):
            self.events.append(("delete_session", session_id))

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    captured = {}

    def fake_poll(*args, **kwargs):
        captured["kwargs"] = kwargs
        return {"type": "completed", "result": "ok"}

    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", fake_poll)

    focused_session_id = _run_focused_coverage_fix_round(
        class_fqn="com.example.service.SampleService",
        session_client=client,
        source_path="/tmp/SampleService.java",
        test_file_path="/tmp/SampleServiceTest.java",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=58.1,
        coverage_gate=80,
        test_class_name="SampleServiceTest",
        maven_module_flag=" -pl biz -am",
        attempt=1,
        uncovered_summary_md="Methods with missed coverage:\n- `run`\n",
    )

    assert focused_session_id == "coverage-session"
    assert client.events[0][0] == "create_session"
    assert client.events[1][0] == "send_message"
    assert client.events[1][1] == "coverage-session"
    assert "SCRIPT-EXTRACTED UNCOVERED CLUSTERS" in client.events[1][3]
    assert "Target context" in client.events[1][3]
    assert "Do not inspect JaCoCo" in client.events[1][3]
    assert captured["kwargs"]["stalled_no_progress_seconds"] == 900
    assert ("delete_session", "coverage-session") not in client.events


def test_run_focused_coverage_fix_round_logs_timeout(monkeypatch, caplog):
    class DummyClient:
        def create_session(self, model_id=None):
            return "coverage-session"

        def send_message(self, session_id, prompt, model_id=None):
            pass

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "timeout", "result": ""})
    caplog.set_level("WARNING")

    focused_session_id = _run_focused_coverage_fix_round(
        class_fqn="com.example.service.SampleService",
        session_client=DummyClient(),
        source_path="/tmp/SampleService.java",
        test_file_path="/tmp/SampleServiceTest.java",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=58.1,
        coverage_gate=80,
        test_class_name="SampleServiceTest",
        maven_module_flag=" -pl biz -am",
        attempt=1,
        uncovered_summary_md="Methods with missed coverage:\n- `run`\n",
    )

    assert focused_session_id == "coverage-session"
    assert "Focused coverage-fix session coverage-session ended with timeout" in caplog.text


def test_run_focused_coverage_fix_round_raises_on_rate_limit(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "coverage-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def delete_session(self, session_id):
            self.events.append(("delete_session", session_id))

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr(
        "uta.graph.nodes._poll_with_continue_recovery",
        lambda *args, **kwargs: {
            "type": "rate_limited",
            "rate_limit": {
                "provider_id": "openai",
                "model_id": "gpt-5.4",
                "message": "The usage limit has been reached",
                "retry_after_seconds": 300,
            },
        },
    )

    with pytest.raises(ProviderRateLimitError) as excinfo:
        _run_focused_coverage_fix_round(
            class_fqn="com.example.service.SampleService",
            session_client=client,
            source_path="/tmp/SampleService.java",
            test_file_path="/tmp/SampleServiceTest.java",
            target_context_abs="/tmp/SampleService.context.md",
            target_symbols_abs="/tmp/SampleService.symbols.md",
            current_coverage=58.1,
            coverage_gate=80,
            test_class_name="SampleServiceTest",
            maven_module_flag=" -pl biz -am",
            attempt=1,
            uncovered_summary_md="Methods with missed coverage:\n- `run`\n",
        )

    assert "retry after 300s" in excinfo.value.output
    assert ("delete_session", "coverage-session") not in client.events


def test_run_focused_mutation_fix_round_raises_on_rate_limit(monkeypatch, tmp_path):
    report = tmp_path / "mutations.xml"
    report.write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<mutations>
  <mutation detected='false' status='SURVIVED'>
    <sourceFile>SampleService.java</sourceFile>
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>run</mutatedMethod>
    <methodDescription>()V</methodDescription>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <index>0</index>
    <block>0</block>
    <lineNumber>42</lineNumber>
    <description>changed conditional boundary</description>
  </mutation>
</mutations>
""",
        encoding="utf-8",
    )
    source_file = tmp_path / "SampleService.java"
    source_file.write_text("public class SampleService {}", encoding="utf-8")
    test_file = tmp_path / "SampleServiceTest.java"
    test_file.write_text("public class SampleServiceTest {}", encoding="utf-8")

    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "mutation-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def delete_session(self, session_id):
            self.events.append(("delete_session", session_id))

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", True)
    monkeypatch.setattr(
        "uta.graph.nodes._poll_with_continue_recovery",
        lambda *args, **kwargs: {
            "type": "rate_limited",
            "rate_limit": {
                "provider_id": "openai",
                "model_id": "gpt-5.4",
                "message": "The usage limit has been reached",
            },
        },
    )

    with pytest.raises(ProviderRateLimitError):
        _run_focused_mutation_fix_round(
            repo_path=str(tmp_path),
            module="biz",
            class_fqn="com.example.service.SampleService",
            session_client=client,
            source_file_abs=source_file,
            test_file_abs=test_file,
            target_context_abs="/tmp/SampleService.context.md",
            target_symbols_abs="/tmp/SampleService.symbols.md",
            current_coverage=82.8,
            mutation_gate_score=70,
            attempt=1,
            mutation_score=45.1,
            mutation_stats={"total": 10, "killed": 4, "survived": 6},
            report_path=str(report),
        )

    assert ("delete_session", "mutation-session") not in client.events


def test_run_focused_coverage_fix_round_deletes_session_when_preserve_disabled(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.events = []

        def create_session(self, model_id=None):
            self.events.append(("create_session", model_id))
            return "coverage-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.events.append(("send_message", session_id, model_id, prompt))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def delete_session(self, session_id):
            self.events.append(("delete_session", session_id))

    client = DummyClient()
    monkeypatch.setattr("uta.graph.nodes.uta_settings.opencode_preserve_focused_sessions", False)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})

    focused_session_id = _run_focused_coverage_fix_round(
        class_fqn="com.example.service.SampleService",
        session_client=client,
        source_path="/tmp/SampleService.java",
        test_file_path="/tmp/SampleServiceTest.java",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        current_coverage=58.1,
        coverage_gate=80,
        test_class_name="SampleServiceTest",
        maven_module_flag=" -pl biz -am",
        attempt=1,
        uncovered_summary_md="Methods with missed coverage:\n- `run`\n",
    )

    assert focused_session_id == "coverage-session"
    assert client.events[-1] == ("delete_session", "coverage-session")


def test_generate_and_validate_stops_when_plan_times_out(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")

    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "HugeServiceTest.java").write_text("class HugeServiceTest {}", encoding="utf-8")
    (test_root / "HugeServiceTwoTest.java").write_text("class HugeServiceTwoTest {}", encoding="utf-8")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "HugeService.java").write_text(
        "\n".join(["public class HugeService {"] + [f"public void m{i}() {{}}" for i in range(12)] + ["}"]),
        encoding="utf-8",
    )
    (source_root / "HugeServiceTwo.java").write_text(
        "\n".join(["public class HugeServiceTwo {"] + [f"public void n{i}() {{}}" for i in range(10)] + ["}"]),
        encoding="utf-8",
    )

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            simple = class_fqn.split(".")[-1]
            return str(source_root / f"{simple}.java")

        def _public_methods(self, class_fqn):
            prefix = "m" if class_fqn.endswith("HugeService") else "n"
            return [
                {"name": f"{prefix}{i}", "line": i + 1, "caller_count": 0}
                for i in range(12 if prefix == "m" else 10)
            ]

        def export_generation_pack(self, class_fqn, *, method_names=None, plan_path=None, max_methods=8):
            out = ctx_dir / f"{class_fqn.split('.')[-1]}.generation_pack.md"
            out.write_text(f"pack for {class_fqn}: {','.join(method_names or [])}", encoding="utf-8")
            return str(out)

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "timeout", "result": ""},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {
        "HugeServiceTest": {"passed": True, "output": ""},
        "HugeServiceTwoTest": {"passed": True, "output": ""},
    })
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.HugeService", "com.example.HugeServiceTwo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert len(client.sent) == 1
    assert out["results"]["com.example.HugeService"]["status"] == "PLANNING_TIMEOUT"
    assert out["results"]["com.example.HugeServiceTwo"]["status"] == "PLANNING_TIMEOUT"
    assert "planning timed out" in out["results"]["com.example.HugeService"]["output"]


def test_generate_and_validate_plans_for_single_class(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert len(client.sent) == 2
    assert client.sent[0][0] == "created-1"
    assert client.sent[1][0] == "created-2"
    assert "PLANNED TESTS" in client.sent[0][1]
    assert "APPROVED TEST PLAN" in client.sent[1][1]
    assert "single-class plan" in client.sent[1][1]
    assert out["results"]["com.example.Solo"]["session_ids"] == ["created-1", "created-2"]
    assert out["results"]["com.example.Solo"]["status"] == "PASS"


def test_generate_and_validate_records_split_phase_session_ids(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, "compile-fix-session"))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.5, "test-fix-session"))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "plan-session",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    result = out["results"]["com.example.Solo"]
    assert result["status"] == "PASS"
    assert result["session_id"] == "test-fix-session"
    assert result["session_ids"] == [
        "created-1",
        "created-2",
        "compile-fix-session",
        "test-fix-session",
    ]


def test_generate_and_validate_injects_stub_catalog_and_skeleton_without_prior_hints(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")
    (ctx_dir / "Solo.context.md").write_text(
        "# Target Test Context\n\n"
        "## Imports\n- `org.springframework.jdbc.core.JdbcTemplate`\n\n"
        "## Dependency Types\n- `JdbcTemplate` — `src/main/java/org/springframework/jdbc/core/JdbcTemplate.java`\n",
        encoding="utf-8",
    )
    (ctx_dir / "Solo.symbols.md").write_text("## Imported Symbols\n", encoding="utf-8")
    learning_dir = repo / ".uta_cache" / "learning"
    learning_dir.mkdir(parents=True, exist_ok=True)
    (learning_dir / "com.example.Solo.jsonl").write_text(
        '{"kind":"compile_fix_iterations","class_fqn":"com.example.Solo","iterations":2}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "uta.learning.load_project_summary",
        lambda repo_path: {"top_resolved_symbols": [{"short_name": "JdbcTemplate", "fqns": ["org.springframework.jdbc.core.JdbcTemplate"], "frequency": 3}]},
    )
    monkeypatch.setattr(
        "uta.learning.summary.load_project_summary",
        lambda repo_path: {"top_resolved_symbols": [{"short_name": "JdbcTemplate", "fqns": ["org.springframework.jdbc.core.JdbcTemplate"], "frequency": 3}]},
    )
    monkeypatch.setattr("uta.graph.nodes.uta_settings.inject_stub_catalog_in_generation", True)
    monkeypatch.setattr("uta.graph.nodes.uta_settings.inject_test_skeleton_in_generation", True)
    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

        def export_target_context_files(self, class_fqn, module=None, test_file_rel=None):
            return {
                "context_abs": str(ctx_dir / "Solo.context.md"),
                "symbols_abs": str(ctx_dir / "Solo.symbols.md"),
            }

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, "compile-fix-session"))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.5, "test-fix-session"))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_phase_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "plan-session",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "phase_token_usage": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PASS"
    planning_prompt = client.sent[0][1]
    generation_prompt = client.sent[1][1]
    assert "PRIOR-RUN HINTS" not in planning_prompt
    assert "PRIOR-RUN HINTS" not in generation_prompt
    assert "compile-fix iterations" not in generation_prompt
    assert "DETERMINISTIC STARTER SKELETONS" in generation_prompt
    assert "JdbcTemplate" in generation_prompt
    assert "MockitoJUnitRunner" in generation_prompt


def test_generate_and_validate_skips_stub_catalog_and_skeleton_by_default(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")
    (ctx_dir / "Solo.context.md").write_text(
        "# Target Test Context\n\n"
        "## Imports\n- `org.springframework.jdbc.core.JdbcTemplate`\n\n"
        "## Dependency Types\n- `JdbcTemplate` — `src/main/java/org/springframework/jdbc/core/JdbcTemplate.java`\n",
        encoding="utf-8",
    )
    (ctx_dir / "Solo.symbols.md").write_text("## Imported Symbols\n", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

        def export_target_context_files(self, class_fqn, module=None, test_file_rel=None):
            return {
                "context_abs": str(ctx_dir / "Solo.context.md"),
                "symbols_abs": str(ctx_dir / "Solo.symbols.md"),
            }

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, "compile-fix-session"))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.5, "test-fix-session"))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_phase_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "plan-session",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "phase_token_usage": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PASS"
    generation_prompt = client.sent[1][1]
    assert "DETERMINISTIC STARTER SKELETONS" not in generation_prompt
    assert "JdbcTemplate" not in generation_prompt
    assert "### PROJECT CONTEXT" not in generation_prompt
    assert "### CONTEXT FILES" not in generation_prompt
    assert "### COMMON PITFALLS TO AVOID" not in generation_prompt


def test_generate_and_validate_denies_todowrite_for_generation_session(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None, permission=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id, permission))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, "compile-fix-session"))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.5, "test-fix-session"))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda report, class_fqn: {"line": 85.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("coverage fix should not run")))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_phase_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "plan-session",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "phase_token_usage": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PASS"
    assert client.created[1][2] == [{"permission": "todowrite", "action": "deny", "pattern": "*"}]


def test_coverage_test_fix_loop_refreshes_full_surefire_failure_set(monkeypatch):
    from uta.graph.nodes import _run_coverage_test_fix_loop

    class DummyClient:
        def __init__(self):
            self.sent = []

        def create_session(self, model_id=None):
            return "coverage-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

    test_results = iter([(False, "raw tail"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr(
        "uta.graph.nodes.parse_surefire_results",
        lambda *args, **kwargs: {
            "SoloTest": {
                "passed": False,
                "output": "pkg.SoloTest.shouldCoverA: failure\nshared seam detail\n\npkg.SoloTest.shouldCoverB: error\nsecond detail",
            }
        },
    )
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "fixed"})

    client = DummyClient()
    ok, output, repair_session_id = _run_coverage_test_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        class_fqn="com.example.Solo",
        test_class_name="SoloTest",
        current_output="raw tail",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert output == ""
    assert repair_session_id == "coverage-test-fix-session"
    prompt = client.sent[0][1]
    assert "## SoloTest" in prompt
    assert "shared seam detail" in prompt
    assert "second detail" in prompt
    assert "Fix the full current failing suite below" in prompt


def test_mutation_test_fix_loop_refreshes_full_surefire_failure_set(monkeypatch):
    from uta.graph.nodes import _run_mutation_test_fix_loop

    class DummyClient:
        def __init__(self):
            self.sent = []

        def create_session(self, model_id=None):
            return "mutation-test-fix-session"

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

    test_results = iter([(False, "raw tail"), (True, "")])
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: next(test_results))
    monkeypatch.setattr(
        "uta.graph.nodes.parse_surefire_results",
        lambda *args, **kwargs: {
            "SoloTest": {
                "passed": False,
                "output": "pkg.SoloTest.shouldMutateA: failure\nnumeric detail\n\npkg.SoloTest.shouldMutateB: error\nshared seam detail",
            }
        },
    )
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "fixed"})

    client = DummyClient()
    ok, output, repair_session_id = _run_mutation_test_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        class_fqn="com.example.Solo",
        test_class_name="SoloTest",
        current_output="raw tail",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert output == ""
    assert repair_session_id == "mutation-test-fix-session"
    prompt = client.sent[0][1]
    assert "## SoloTest" in prompt
    assert "numeric detail" in prompt
    assert "shared seam detail" in prompt
    assert "Fix the full current failing suite below" in prompt


def test_capture_phase_token_usage_aggregates_by_phase():
    class DummyClient:
        def analyze_session_tokens(self, session_id):
            values = {
                "plan-1": {"input": 10, "output": 2, "reasoning": 0, "cache_read": 5, "cache_write": 0, "total": 17},
                "compile-1": {"input": 7, "output": 1, "reasoning": 0, "cache_read": 3, "cache_write": 0, "total": 11},
                "compile-2": {"input": 8, "output": 2, "reasoning": 0, "cache_read": 4, "cache_write": 0, "total": 14},
            }
            return {
                "assistant_messages": 1,
                "main_model_tokens": {},
                "small_model_tokens": {},
                "other_model_tokens": {},
                "total_tokens": values[session_id],
                "by_model": {},
            }

    observed = _capture_phase_token_usage(
        state={"phase_token_usage": {}},
        client=DummyClient(),
        phase_session_ids={
            "plan": ["plan-1"],
            "compile_fix": ["compile-1", "compile-2"],
            "coverage_fix": [],
        },
    )

    assert observed["plan"]["total"] == 17
    assert observed["compile_fix"]["input"] == 15
    assert observed["compile_fix"]["total"] == 25


def test_generate_and_validate_handles_missing_files_without_compile_gate(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
        {"type": "completed", "result": "still missing"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: ["biz/src/test/java/com/example/SoloTest.java"])
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "INCOMPLETE_BATCH"


def test_provider_error_result_marks_batch_provider_error(monkeypatch, tmp_path):
    import uta.graph.nodes as nodes

    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})

    out = nodes._provider_error_result(
        state={"results": {}, "session_ids": []},
        batch=["com.example.Solo"],
        session_id="ses-err",
        event={"type": "error", "error": {"data": {"message": "Model not found: openai/gpt-5.4"}}},
        stage="plan_tests",
        generate_validate_seconds=1.0,
        repo_path=str(tmp_path),
        module="biz",
        client=object(),
    )

    result = out["results"]["com.example.Solo"]
    assert result["status"] == "PROVIDER_ERROR"
    assert "Model not found" in result["output"]
    assert out["stopped_early"] is True


def test_push_retry_aborts_stale_rebase_before_rebase(monkeypatch, tmp_path):
    import uta.graph.nodes as nodes
    from types import SimpleNamespace

    repo = tmp_path
    (repo / ".git" / "rebase-apply").mkdir(parents=True)
    calls = []

    def fake_git_run(repo_path, *args, **kwargs):
        calls.append(args)
        if args[:1] == ("push",) and len([c for c in calls if c[:1] == ("push",)]) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="rejected non-fast-forward")
        if args == ("rev-parse", "--git-dir"):
            return SimpleNamespace(returncode=0, stdout=".git\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(nodes, "_git_run", fake_git_run)

    result = nodes._push_branch_with_rebase_retry(str(repo), "uta/test")

    assert result.returncode == 0
    assert ("rebase", "--abort") in calls
    assert calls.index(("rebase", "--abort")) < calls.index(("pull", "--rebase", "origin", "uta/test"))


def test_generate_and_validate_single_class_plan_rate_limited(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def detect_rate_limit_issue(self, session_id):
            return {
                "provider_id": "openai",
                "model_id": "gpt-5.4",
                "message": "The usage limit has been reached",
                "retry_after_seconds": 300,
                "raw_type": "usage_limit_reached",
            }

    client = DummyClient()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {
        "type": "rate_limited",
        "result": "",
        "rate_limit": {
            "provider_id": "openai",
            "model_id": "gpt-5.4",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 300,
        },
    })
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PROVIDER_RATE_LIMITED"
    assert "retry after 300s" in out["results"]["com.example.Solo"]["output"]
    assert out["stopped_early"] is True
    assert out["finished"] is True
    assert out["current_batch"] == []


def test_generate_and_validate_single_class_plan_timeout(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def detect_rate_limit_issue(self, session_id):
            return None

    client = DummyClient()

    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {
        "type": "timeout",
        "result": "",
    })
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("plan should not be written after planning timeout")))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    result = out["results"]["com.example.Solo"]
    assert result["status"] == "PLANNING_TIMEOUT"
    assert "planning timed out" in result["output"]
    assert out["current_stage"] == "plan_tests"
    assert client.created == [("created-1", "openai/gpt-5.4")]


def test_generate_and_validate_classifies_provider_quota_before_missing_files(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def detect_rate_limit_issue(self, session_id):
            return {
                "provider_id": "openrouter",
                "model_id": "z-ai/glm-5.1",
                "message": "This request requires more credits, or fewer max_tokens",
                "status_code": 402,
                "raw_type": "insufficient_credits",
            }

    client = DummyClient()
    poll_events = iter([
        {"type": "completed", "result": "single-class plan"},
        {"type": "completed", "result": "generated"},
    ])

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: (_ for _ in ()).throw(AssertionError("missing-file prompt should not run after provider quota detection")))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PROVIDER_RATE_LIMITED"
    assert "requires more credits" in out["results"]["com.example.Solo"]["output"]
    assert out["stopped_early"] is True
    assert out["finished"] is True
    assert out["current_batch"] == []


def test_generate_and_validate_classifies_provider_quota_during_planning_without_generation(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

    class DummyClient:
        def __init__(self):
            self.sent = []
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, session_id, prompt, model_id=None):
            self.sent.append((session_id, prompt, model_id))

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            self.send_message(session_id, stable + volatile, model_id=model_id)

        def detect_rate_limit_issue(self, session_id):
            return {
                "provider_id": "tencent",
                "model_id": "glm-5",
                "message": "endpoint is inactive: FREE_QUOTA_EXHAUSTED",
                "status_code": 402,
                "raw_type": "insufficient_credits",
            }

    client = DummyClient()
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": ""})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": None,
        "flows": [],
        "session_id": "ses-1",
        "coverage_gate": 80,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PROVIDER_RATE_LIMITED"
    assert "FREE_QUOTA_EXHAUSTED" in out["results"]["com.example.Solo"]["output"]
    assert client.created == [("created-1", "openai/gpt-5.4")]
    assert out["stopped_early"] is True
    assert out["finished"] is True
    assert out["current_batch"] == []


def test_generate_and_validate_marks_provider_limited_when_focused_coverage_fix_hits_quota(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "class_map.md").write_text("class map")
    (ctx_dir / "dependency_map.md").write_text("dependency map")
    source_root = repo / "biz" / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "Solo.java").write_text("public class Solo { public void run() {} }", encoding="utf-8")
    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    (test_root / "SoloTest.java").write_text("class SoloTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return str(source_root / "Solo.java")

        def export_target_context_files(self, class_fqn, module=None, test_file_rel=None):
            return {
                "context_abs": str(ctx_dir / "Solo.context.md"),
                "symbols_abs": str(ctx_dir / "Solo.symbols.md"),
            }

    class DummyClient:
        def __init__(self):
            self.created = []

        def create_session(self, model_id=None):
            session_id = f"created-{len(self.created) + 1}"
            self.created.append((session_id, model_id))
            return session_id

        def send_message(self, *args, **kwargs):
            return None

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            return None

        def detect_rate_limit_issue(self, session_id):
            if session_id != "focused-coverage-session":
                return None
            return {
                "provider_id": "openai",
                "model_id": "gpt-5.4",
                "message": "The usage limit has been reached",
                "retry_after_seconds": 300,
                "raw_type": "usage_limit_reached",
            }

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: DummyClient())
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })
    poll_events = iter([
        {"type": "completed", "result": "plan"},
        {"type": "completed", "result": "generated"},
    ])
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: next(poll_events))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 0.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SoloTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {"line": 52.6})
    monkeypatch.setattr(
        "uta.graph.nodes.run_coverage_fix_loop",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ProviderRateLimitError(
                session_id="focused-coverage-session",
                phase="coverage_fix",
                rate_limit={
                    "provider_id": "openai",
                    "model_id": "gpt-5.4",
                    "message": "The usage limit has been reached",
                    "retry_after_seconds": 300,
                },
            )
        ),
    )

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-main",
        "coverage_gate": 60,
        "mutation_gate": 0,
        "current_batch": ["com.example.Solo"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert out["results"]["com.example.Solo"]["status"] == "PROVIDER_RATE_LIMITED"
    assert out["results"]["com.example.Solo"]["coverage"] == 52.6
    assert out["results"]["com.example.Solo"]["session_id"] == "focused-coverage-session"
    assert "retry after 300s" in out["results"]["com.example.Solo"]["output"]
    assert out["stopped_early"] is True
    assert out["finished"] is True
    assert out["current_batch"] == []


def test_plan_needs_stricter_replan_for_narrow_complex_plan():
    strict_classes = [{"class_fqn": "com.example.big.HugeService", "line_count": 600, "public_method_count": 12}]
    plan = """
**HugeService**
1. PUBLIC METHODS
- High-value public methods first
4. COVERAGE RISKS
- do not chase class-wide completeness
    """
    assert _plan_needs_stricter_replan(plan, strict_classes) is True


def test_continue_prompt_for_plan_reuses_existing_plan_document():
    from uta.graph.nodes import _continue_prompt_for_phase

    prompt = _continue_prompt_for_phase("plan")

    assert "Continue the planning document only" in prompt
    assert "latest_generation_plan.md" in prompt
    assert "Do NOT restart broad exploration from scratch" in prompt


def test_plan_breadth_under_requests_replan():
    breadth = BreadthResult(
        verdict=BreadthVerdict.UNDER,
        planned_methods=2,
        known_methods=10,
        coverage_ratio=0.2,
        missing_methods=["bar"],
        extra_methods=[],
        message="Plan covers 2/10 methods.",
    )

    assert _plan_breadth_replan_reason("com.example.HugeService", breadth) == (
        "[com.example.HugeService] Plan covers 2/10 methods."
    )


def test_plan_breadth_over_is_non_fatal():
    breadth = BreadthResult(
        verdict=BreadthVerdict.OVER,
        planned_methods=100,
        known_methods=51,
        coverage_ratio=0.86,
        missing_methods=[],
        extra_methods=["helper"],
        message="Plan lists 100 methods but only 51 are known.",
    )

    assert _plan_breadth_replan_reason("com.example.HugeService", breadth) is None


def test_candidate_plan_is_preserved_only_until_final_plan(tmp_path):
    repo = tmp_path / "repo"

    candidate_path = Path(
        _write_generation_plan_candidate(
            str(repo),
            "ses-candidate",
            ["com.example.Sample"],
            "candidate body",
            ["Plan feasibility validator found the gate-method mix too weak"],
        )
    )

    assert candidate_path.exists()
    assert "candidate body" in candidate_path.read_text()

    _write_generation_plan(str(repo), "ses-final", ["com.example.Sample"], "final body")

    assert not candidate_path.exists()
    assert "final body" in (repo / ".uta_cache" / "context" / "latest_generation_plan.md").read_text()


def test_load_generation_plan_for_resume_falls_back_to_candidate_artifact(tmp_path):
    from uta.graph.nodes import _load_generation_plan_for_resume

    repo = tmp_path / "repo"
    _write_generation_plan_candidate(
        str(repo),
        "ses-candidate",
        ["com.example.Sample"],
        "## com.example.Sample\n\nPUBLIC METHODS\n- foo",
        ["Need broader branch reach"],
    )

    recovered = _load_generation_plan_for_resume(str(repo), ["com.example.Sample"])

    assert "PUBLIC METHODS" in recovered
    assert "Need broader branch reach" not in recovered


def test_load_generation_plan_for_resume_prefers_final_plan_artifact(tmp_path):
    from uta.graph.nodes import _load_generation_plan_for_resume

    repo = tmp_path / "repo"
    _write_generation_plan_candidate(
        str(repo),
        "ses-candidate",
        ["com.example.Sample"],
        "candidate body",
        ["Need broader branch reach"],
    )
    _write_generation_plan(
        str(repo),
        "ses-final",
        ["com.example.Sample"],
        "final body",
    )

    recovered = _load_generation_plan_for_resume(str(repo), ["com.example.Sample"])

    assert recovered == "final body"


def test_load_generation_plan_for_resume_skips_mismatched_final_and_uses_candidate(tmp_path):
    from uta.graph.nodes import _load_generation_plan_for_resume

    repo = tmp_path / "repo"
    plan_path = repo / ".uta_cache" / "context" / "latest_generation_plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "\n".join(
            [
                "# Latest Generation Plan",
                "",
                "- session_id: `ses-final`",
                "- classes: `com.example.Other`",
                "",
                "wrong final body",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_generation_plan_candidate(
        str(repo),
        "ses-candidate",
        ["com.example.Sample"],
        "candidate body",
        ["Need broader branch reach"],
    )

    recovered = _load_generation_plan_for_resume(str(repo), ["com.example.Sample"])

    assert recovered == "candidate body"


def test_recover_plan_text_ignores_stale_session_artifact(tmp_path):
    repo = tmp_path / "repo"
    _write_generation_plan(str(repo), "old-session", ["com.example.Sample"], "old plan")

    class DummyClient:
        def get_messages(self, session_id):
            return [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "patch",
                            "files": [str(repo / ".uta_cache" / "context" / "latest_generation_plan.md")],
                        }
                    ],
                }
            ]

    recovered = _recover_plan_text_from_session_artifact(
        repo_path=str(repo),
        session_id="new-session",
        client=DummyClient(),
    )

    assert recovered == ""


def test_recover_plan_text_from_session_artifact_reads_candidate_plan(tmp_path):
    repo = tmp_path / "repo"
    candidate_path = Path(
        _write_generation_plan_candidate(
            str(repo),
            "session-123",
            ["com.example.Sample"],
            "## com.example.Sample\n\nPLANNED TESTS\n- testFoo",
            ["Need stronger estimated reach"],
        )
    )

    class DummyClient:
        def get_messages(self, session_id):
            assert session_id == "session-123"
            return [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "patch",
                            "files": [str(candidate_path)],
                        }
                    ],
                }
            ]

    recovered = _recover_plan_text_from_session_artifact(
        repo_path=str(repo),
        session_id="session-123",
        client=DummyClient(),
    )

    assert "PLANNED TESTS" in recovered
    assert "Need stronger estimated reach" not in recovered


def test_recover_plan_text_from_session_artifact_falls_back_to_candidate_when_final_is_stale(tmp_path):
    repo = tmp_path / "repo"
    plan_path = repo / ".uta_cache" / "context" / "latest_generation_plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "\n".join(
            [
                "# Latest Generation Plan",
                "",
                "- session_id: `old-session`",
                "- classes: `com.example.Sample`",
                "",
                "stale final body",
                "",
            ]
        ),
        encoding="utf-8",
    )
    candidate_path = Path(
        _write_generation_plan_candidate(
            str(repo),
            "session-123",
            ["com.example.Sample"],
            "candidate body",
            ["Need stronger estimated reach"],
        )
    )

    class DummyClient:
        def get_messages(self, session_id):
            assert session_id == "session-123"
            return [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {
                            "type": "patch",
                            "files": [str(candidate_path)],
                        }
                    ],
                }
            ]

    recovered = _recover_plan_text_from_session_artifact(
        repo_path=str(repo),
        session_id="session-123",
        client=DummyClient(),
    )

    assert recovered == "candidate body"


def test_clear_generation_plan_removes_prior_run_artifact(tmp_path):
    repo = tmp_path / "repo"
    plan_path = Path(_write_generation_plan(str(repo), "old-session", ["com.example.Sample"], "old plan"))

    _clear_generation_plan(str(repo))

    assert not plan_path.exists()


def test_generate_and_validate_runs_mutation_even_when_coverage_retry_still_below_gate(monkeypatch, tmp_path):
    from uta.graph.nodes import generate_and_validate

    repo = tmp_path
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "project_summary.md").write_text("summary")
    (ctx_dir / "test_generation_guidance.md").write_text("guidance")
    (ctx_dir / "latest_generation_plan.md").write_text("plan")

    test_root = repo / "biz" / "src" / "test" / "java" / "com" / "example"
    test_root.mkdir(parents=True, exist_ok=True)
    test_file = test_root / "SampleTest.java"
    test_file.write_text("class SampleTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda *args, **kwargs: None)

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return "biz/src/main/java/com/example/Sample.java"

        def export_target_context_files(self, class_fqn, module=None, test_file_rel=None):
            return {
                "context_abs": str(ctx_dir / "Sample.context.md"),
                "symbols_abs": str(ctx_dir / "Sample.symbols.md"),
            }

    class DummyClient:
        def __init__(self):
            self.created = []

        def create_session(self, model_id=None):
            sid = f"created-{len(self.created)+1}"
            self.created.append((sid, model_id))
            return sid

        def send_message(self, *args, **kwargs):
            return None

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            return None

    mutation_calls = {"count": 0}

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: DummyClient())
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SampleTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {"line": 74.2})
    monkeypatch.setattr(
        "uta.graph.nodes.run_coverage_fix_loop",
        lambda *args, **kwargs: (False, 74.2, "still low", ["focused-coverage-session"]),
    )
    monkeypatch.setattr("uta.graph.nodes.run_pitest", lambda *args, **kwargs: mutation_calls.__setitem__("count", mutation_calls["count"] + 1) or (False, "pitest failed"))
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes.compute_mutation_stats", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-main",
        "coverage_gate": 80,
        "mutation_gate": 70,
        "current_batch": ["com.example.Sample"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert mutation_calls["count"] == 1
    assert out["results"]["com.example.Sample"]["status"] == "FAIL"
    assert "focused-coverage-session" in out["results"]["com.example.Sample"]["session_ids"]


def test_generate_and_validate_routes_pitest_green_suite_failure_into_mutation_test_fix(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx_dir = repo / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "Sample.context.md").write_text("ctx", encoding="utf-8")
    (ctx_dir / "Sample.symbols.md").write_text("symbols", encoding="utf-8")
    test_file = repo / "biz" / "src" / "test" / "java" / "com" / "example" / "SampleTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class SampleTest {}", encoding="utf-8")

    class DummyContextBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def export_context_files(self):
            return ctx_dir

        def get_class_source_path(self, class_fqn):
            return "biz/src/main/java/com/example/Sample.java"

        def export_target_context_files(self, class_fqn, module=None, test_file_rel=None):
            return {
                "context_abs": str(ctx_dir / "Sample.context.md"),
                "symbols_abs": str(ctx_dir / "Sample.symbols.md"),
            }

    class DummyClient:
        def __init__(self):
            self.created = []

        def create_session(self, model_id=None):
            sid = f"created-{len(self.created)+1}"
            self.created.append((sid, model_id))
            return sid

        def send_message(self, *args, **kwargs):
            return None

        def send_message_split(self, session_id, stable, volatile, model_id=None):
            return None

    mutation_calls = {"count": 0}
    mutation_test_fix_calls = {"count": 0}

    def fake_run_pitest(*args, **kwargs):
        mutation_calls["count"] += 1
        if mutation_calls["count"] == 1:
            return False, (
                "Description [testClass=com.example.SampleTest, name=shouldStayGreen]\n"
                "java.lang.AssertionError: expected:<5.000000> but was:<5>\n"
                "1 tests did not pass without mutation when calculating line coverage. Mutation testing requires a green suite.\n"
            )
        return True, ""

    def fake_mutation_test_fix(**kwargs):
        mutation_test_fix_calls["count"] += 1
        return True, "", "mutation-test-fix-session"

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: DummyClient())
    monkeypatch.setattr("uta.graph.nodes._poll_with_continue_recovery", lambda *args, **kwargs: {"type": "completed", "result": "ok"})
    monkeypatch.setattr("uta.graph.nodes._write_generation_plan", lambda *args, **kwargs: str(ctx_dir / "latest_generation_plan.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.graph.nodes.parse_surefire_results", lambda *args, **kwargs: {"SampleTest": {"passed": True, "output": ""}})
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {"line": 81.0})
    monkeypatch.setattr("uta.graph.nodes.run_coverage_fix_loop", lambda *args, **kwargs: (True, 81.0, "", []))
    monkeypatch.setattr("uta.graph.nodes.run_pitest", fake_run_pitest)
    monkeypatch.setattr("uta.graph.nodes._run_mutation_test_fix_loop", fake_mutation_test_fix)
    monkeypatch.setattr("uta.graph.nodes.find_latest_pitest_report", lambda *args, **kwargs: "pit.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_pitest_report", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "uta.graph.nodes.compute_mutation_stats",
        lambda *args, **kwargs: {
            "score": 71.0,
            "survived": 0,
            "total": 10,
            "killed": 10,
            "no_coverage": 0,
            "timed_out": 0,
            "non_viable": 0,
            "memory_error": 0,
            "run_error": 0,
            "status_counts": {},
        },
    )
    monkeypatch.setattr("uta.graph.nodes._capture_session_retrospect", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._capture_session_token_usage", lambda *args, **kwargs: {})
    monkeypatch.setattr("uta.graph.nodes._precheck_existing_tests", lambda **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda *args, **kwargs: None)
    monkeypatch.setattr("uta.graph.nodes.prompt_template_paths", lambda *args, **kwargs: {
        "repo_summary_exists": False,
        "context_summary_abs": "/tmp/project_summary.md",
        "test_guidance_abs": "/tmp/test_generation_guidance.md",
        "compile_facts_exists": False,
    })

    class DummyGraph:
        nodes = {}

    state = {
        "repo_path": str(repo),
        "module": "biz",
        "graph": DummyGraph(),
        "flows": [],
        "session_id": "ses-main",
        "coverage_gate": 80,
        "mutation_gate": 70,
        "current_batch": ["com.example.Sample"],
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
    }

    out = generate_and_validate(state)

    assert mutation_calls["count"] == 2
    assert mutation_test_fix_calls["count"] == 1
    assert out["results"]["com.example.Sample"]["status"] == "PASS"
    assert "mutation-test-fix-session" in out["results"]["com.example.Sample"]["session_ids"]


def test_plan_does_not_replan_when_strict_sections_exist():
    strict_classes = [{"class_fqn": "com.example.big.HugeService", "line_count": 600, "public_method_count": 12}]
    plan = """
**HugeService**
5. METHODS REQUIRED FOR GATE
- foo
6. ESTIMATED REACH
- plausible path to 80%
"""
    assert _plan_needs_stricter_replan(plan, strict_classes) is False


def test_session_progress_logger_includes_stage_and_session(caplog):
    logger_fn = _session_progress_logger(
        ["com.example.A", "com.example.B"],
        session_id="ses-123",
        stage="generate",
    )
    with caplog.at_level("INFO", logger="uta"):
        logger_fn("text: working")
    assert "stage=generate" in caplog.text
    assert "session=ses-123" in caplog.text
    assert "A,B" in caplog.text


def test_relax_surefire_skiptests_rewrites_hardcoded_true(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """
<project>
  <build>
    <plugins>
      <plugin>
        <artifactId>maven-surefire-plugin</artifactId>
        <configuration>
          <skipTests>true</skipTests>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
""".strip()
    )

    changed = _relax_surefire_skiptests(str(tmp_path))

    assert changed is True
    updated = pom.read_text()
    assert "<skipTests>${skipTests}</skipTests>" in updated


def test_relax_surefire_skiptests_noop_without_surefire(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project><build><plugins></plugins></build></project>")

    changed = _relax_surefire_skiptests(str(tmp_path))

    assert changed is False
    assert pom.read_text() == "<project><build><plugins></plugins></build></project>"


def test_upgrade_mockito_adds_bytebuddy_alignment(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """
<project>
  <properties>
    <mockito-core.version>2.28.2</mockito-core.version>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.mockito</groupId>
        <artifactId>mockito-core</artifactId>
        <version>2.28.2</version>
        <scope>test</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.mockito</groupId>
      <artifactId>mockito-core</artifactId>
    </dependency>
  </dependencies>
</project>
""".strip()
    )

    changed = _upgrade_mockito(str(tmp_path))

    assert changed is True
    updated = pom.read_text()
    assert "<byte-buddy.version>1.9.10</byte-buddy.version>" in updated
    assert "<artifactId>byte-buddy</artifactId>" in updated
    assert "<artifactId>byte-buddy-agent</artifactId>" in updated
    assert "${byte-buddy.version}" in updated


def test_mockito_api_guidance_preserves_committed_mockito_all(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """
<project>
  <dependencies>
    <dependency>
      <groupId>org.mockito</groupId>
      <artifactId>mockito-all</artifactId>
      <version>1.10.19</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
""".strip()
    )

    guidance = _mockito_api_guidance(str(tmp_path))

    assert "Mockito 1.x" in guidance
    assert "org.mockito.Matchers" in guidance
    assert "org.mockito.runners.MockitoJUnitRunner" in guidance
    assert "org.mockito.ArgumentMatchers" in guidance
