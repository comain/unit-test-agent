import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

from tests.fake_maven_metadata import with_resolved_enforcer
from uta.testgen.graph.workflow import build_workflow
from uta.language.java.generation import (
    select_next_class,
    _plan_needs_stricter_replan,
    _plan_breadth_replan_reason,
    _run_delegated_quality_gate_once,
    _write_generation_plan,
    _write_generation_plan_candidate,
    _recover_plan_text_from_session_artifact,
    _clear_generation_plan,
    _delegated_gate_batch_results,
    _delegated_gate_failure_matches_batch,
    _annotate_out_of_scope_gate_failure,
    _delegated_quality_gate_feedback,
    _delegated_gate_failure_stage,
    _sync_task_results_if_available,
)
from uta.language.java.candidates import parse_context, scan_and_select
from uta.language.java.baseline import (
    _mockito_api_guidance,
    _upgrade_mockito,
)
from uta.testgen.delivery import (
    _commit_rdc_repair_to_branch,
    commit_to_branch,
    store_and_push,
)
from uta.language.java.selection import (
    is_accessor_like_method as _is_accessor_like_method,
    is_testable_class as _is_testable_class,
)
from uta.testgen.workspace_guard import (
    allowed_llm_path as _allowed_llm_path,
)
from uta.language.java.workspace import (
    expected_ci_incremental_java_test_paths as _expected_ci_incremental_java_test_paths,
)
from uta.language.python.workspace import (
    cleanup_python_verifier_residue as _cleanup_python_verifier_residue,
)
from uta.testgen.validation import BreadthResult, BreadthVerdict


def _fake_agent_node(client, event, kwargs):
    """Record a neutral node call on the small runtime fakes used in this file."""
    from uta.shared.config import settings as test_settings

    sender = getattr(client, "send_message", None)
    if callable(sender):
        sender(
            kwargs["session_id"],
            kwargs["prompt"],
            model_id=kwargs.get("model_id") or test_settings.opencode_model,
        )
    return event


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
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_simple_batch_size", 3)

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
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_complex_line_threshold", 100)

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
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_batching_enabled", True)
    monkeypatch.setattr("uta.language.java.generation.uta_settings.smart_simple_batch_size", 3)

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








def test_delegated_quality_gate_feedback_sanitizes_pit_terminal_progress():
    pit_progress = "Mutating from /repo/target/classes " + ("|/-\\\b" * 30)
    result = {
        "status": "failed",
        "summary": "UTA test-enforcement failed",
        "command": ["mvn", "verify"],
        "stdout": (
            "\x1b[32m[INFO]\x1b[0m PIT >> INFO : Created 10 mutation test units\n"
            "[INFO] Found plugin : Logging calls filter\n"
            "[INFO] Available mutators : EXPERIMENTAL_ARGUMENT_PROPAGATION,FALSE_RETURNS,TRUE_RETURNS\n"
            "[INFO] Packaging webapp\n"
            "\tat org.apache.maven.surefire.booter.ForkedBooter.run(ForkedBooter.java:507)\n"
            f"{pit_progress}\n"
            ">> Generated 149 mutations Killed 147 (99%)\n"
            ">> Mutations with no coverage 0. Test strength 99%\n"
            "[ERROR] Test strength score of 99 is below threshold of 100\n"
        ),
        "stderr": "\x00hidden-control\x08-noise",
    }

    feedback = _delegated_quality_gate_feedback(result)

    assert "\x1b" not in feedback
    assert "\x08" not in feedback
    assert "\x00" not in feedback
    assert "|/-" not in feedback
    assert "Found plugin" not in feedback
    assert "Available mutators" not in feedback
    assert "Packaging webapp" not in feedback
    assert "ForkedBooter.run" not in feedback
    assert "Created 10 mutation test units" in feedback
    assert "Generated 149 mutations Killed 147" in feedback
    assert "Mutations with no coverage 0. Test strength 99%" in feedback
    assert "Test strength score of 99 is below threshold of 100" in feedback


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

    monkeypatch.setattr("uta.testgen.delivery._git_run", fake_git_run)

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

    monkeypatch.setattr("uta.testgen.delivery._git_run", fake_git_run)

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


# `git_status_paths` no longer parses porcelain output here -- it delegates to
# `agent_core.git.guard.changed_paths`, so patching `_git_run` no longer
# reaches it. The test that lived here fed a hand-written listing and asserted
# on unicode paths and a rename; both cases are now covered where the parsing
# actually is, against a real `git mv` and a real non-ASCII filename
# (`agent-core/tests/test_workspace_guard.py`), plus `test_workspace_rename_guard.py`
# in this repo. Its fixture had the rename fields reversed, which is how the
# parser came to have them reversed too -- a synthetic listing can only ever
# confirm what its author already believed.


def test_allowed_llm_path_accepts_selected_existing_python_test_only():
    state = {"language": "python", "selected_test_paths": ["tests/test_forecast_pure.py", "jobs/forecast.py"]}

    assert _allowed_llm_path(
        "tests/test_forecast_pure.py",
        state,
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )
    assert not _allowed_llm_path(
        "jobs/forecast.py",
        state,
        ["pysymbol:jobs/forecast.py::forecast_for_store"],
    )


def test_allowed_llm_path_accepts_python_non_code_temp_artifacts():
    state = {"language": "python"}

    assert _allowed_llm_path(
        ".tmp_train_server_test/logs/run_state/fine_tuning_run_99.json",
        state,
        ["pyfile:chat_robot/service/fine_tuning_service.py"],
    )
    assert _allowed_llm_path(
        ".tmp_train_server_test/cfg/train.yaml",
        state,
        ["pyfile:chat_robot/service/fine_tuning_service.py"],
    )
    assert not _allowed_llm_path(
        ".tmp_train_server_test/plugin.py",
        state,
        ["pyfile:chat_robot/service/fine_tuning_service.py"],
    )


def test_allowed_llm_path_accepts_python_pyproject_repair_config():
    state = {"language": "python"}

    assert _allowed_llm_path(
        "pyproject.toml",
        state,
        ["pyfile:pipecat/main.py"],
    )
    assert not _allowed_llm_path(
        "requirements.txt",
        state,
        ["pyfile:pipecat/main.py"],
    )


def test_python_verifier_residue_cleanup_restores_mutmut_overlays(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.mutmut]\n"
        "source_paths = ['pipecat/security/']\n"
        "pytest_add_cli_args_test_selection = ['tests/security/test_security_unit.py', '-x', '-q']\n",
        encoding="utf-8",
    )
    (repo / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")
    test_file = repo / "tests" / "uta_generated" / "test_pipecat_main.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_main():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "uta@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "UTA"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    (repo / "pyproject.toml").write_text(
        "[tool.mutmut]\n"
        "paths_to_mutate = ['pipecat/main.py']\n"
        "pytest_add_cli_args_test_selection = ['tests/uta_generated/test_pipecat_main.py', '-x', '-q']\n",
        encoding="utf-8",
    )
    (repo / "setup.cfg").write_text(
        "[metadata]\nname = demo\n\n"
        "[mutmut]\n"
        "paths_to_mutate = pipecat/main.py\n"
        "runner = UTA_MUTMUT_TARGET_REL=pipecat/main.py python -m pytest tests/uta_generated/test_pipecat_main.py\n",
        encoding="utf-8",
    )
    (repo / "mutants").mkdir()
    (repo / "mutants" / "cache.txt").write_text("runtime", encoding="utf-8")
    (repo / ".coverage").write_text("runtime", encoding="utf-8")
    test_file.write_text("def test_main():\n    assert 1 == 1\n", encoding="utf-8")

    _cleanup_python_verifier_residue(str(repo))

    assert not (repo / "mutants").exists()
    assert not (repo / ".coverage").exists()
    assert "pipecat/security/" in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert (repo / "setup.cfg").read_text(encoding="utf-8") == "[metadata]\nname = demo\n"
    assert "assert 1 == 1" in test_file.read_text(encoding="utf-8")


def test_rdc_repair_no_changes_reuses_existing_pushed_commit(monkeypatch, tmp_path):
    from uta.app.delivery import PushPolicyError, RdcRepairPublisher
    from uta.tasks.rdc_delivery import RdcDeliveryContext

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
        raise PushPolicyError("RDC repair delivery found no test changes to commit")

    monkeypatch.setattr(RdcRepairPublisher, "publish", no_changes)

    manager = FakeManager()
    out = _commit_rdc_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": None,
            "results": {"pkg.A": {"status": "PASS", "coverage": None}},
        },
        manager=manager,
        context=RdcDeliveryContext(branch_name="feature/TASK-1"),
        class_fqns=["pkg.A"],
        results={"pkg.A": {"status": "PASS", "coverage": None}},
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert manager.commits[0][1]["commit_sha"] == "abc123"
    assert manager.synced[0][1]["pkg.A"]["status"] == "PASS"


def test_rdc_repair_no_changes_syncs_pass_without_recorded_latest_commit(monkeypatch, tmp_path):
    from uta.app.delivery import PushPolicyError, RdcRepairPublisher
    from uta.tasks.rdc_delivery import RdcDeliveryContext

    class FakeManager:
        def __init__(self):
            self.commits = []
            self.synced = []

        def get_task(self, task_id):
            return {"latest_commit": None, "remote_ref": None}

        def record_commit(self, task_id, **kwargs):
            self.commits.append((task_id, kwargs))

        def sync_results(self, task_id, results, **kwargs):
            self.synced.append((task_id, results, kwargs))

    def no_changes(self, context):
        raise PushPolicyError("RDC repair delivery found no test changes to commit")

    monkeypatch.setattr(RdcRepairPublisher, "publish", no_changes)

    manager = FakeManager()
    out = _commit_rdc_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": None,
            "results": {"pkg.A": {"status": "PASS", "coverage": 100.0}},
        },
        manager=manager,
        context=RdcDeliveryContext(branch_name="feature/TASK-1"),
        class_fqns=["pkg.A"],
        results={"pkg.A": {"status": "PASS", "coverage": 100.0}},
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert manager.commits[0][1]["commit_sha"] is None
    assert manager.synced[0][1]["pkg.A"]["status"] == "PASS"


def test_rdc_repair_checkpoint_scopes_failed_terminal_target_paths(monkeypatch, tmp_path):
    from uta.tasks.rdc_delivery import RdcDeliveryContext

    captured = {}

    class FakeManager:
        def record_push_failed(self, *args, **kwargs):
            raise AssertionError("push should not fail")

    def fake_commit(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("uta.tasks.rdc_delivery.commit_rdc_repair_results", fake_commit)

    out = _commit_rdc_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": None,
            "deterministic_change_paths": ["pom.xml", "common/src/main/java/javafx/util/Pair.java"],
            "results": {
                "pkg.A": {
                    "status": "MUTATION_FAIL",
                    "coverage": 100.0,
                    "test_file_path": "biz/src/test/java/pkg/ATest.java",
                }
            },
        },
        manager=FakeManager(),
        context=RdcDeliveryContext(branch_name="feature/TASK-1"),
        class_fqns=["pkg.A"],
        results={
            "pkg.A": {
                "status": "MUTATION_FAIL",
                "coverage": 100.0,
                "test_file_path": "biz/src/test/java/pkg/ATest.java",
            }
        },
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert captured["target_ids"] == ["pkg.A"]
    assert captured["commit_paths"] == ["biz/src/test/java/pkg/ATest.java", "pom.xml"]


def test_rdc_repair_checkpoint_includes_python_pyproject_config(monkeypatch, tmp_path):
    from uta.tasks.rdc_delivery import RdcDeliveryContext

    (tmp_path / "pyproject.toml").write_text("[tool.mutmut]\npaths_to_mutate='src/app.py'\n", encoding="utf-8")
    test_path = tmp_path / "tests" / "uta_generated" / "test_app.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_app():\n    assert True\n", encoding="utf-8")

    captured = {}

    class FakeManager:
        def record_push_failed(self, *args, **kwargs):
            raise AssertionError("push should not fail")

    def fake_commit(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(
            "uta.testgen.delivery._git_status_paths",
        lambda repo_path: {"pyproject.toml", "tests/uta_generated/test_app.py"},
    )
    monkeypatch.setattr("uta.tasks.rdc_delivery.commit_rdc_repair_results", fake_commit)

    out = _commit_rdc_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": None,
            "language": "python",
            "results": {
                "pyfile:src/app.py": {
                    "status": "PASS",
                    "coverage": 100.0,
                    "test_file_path": "tests/uta_generated/test_app.py",
                }
            },
        },
        manager=FakeManager(),
        context=RdcDeliveryContext(branch_name="feature/TASK-1"),
        class_fqns=["pyfile:src/app.py"],
        results={
            "pyfile:src/app.py": {
                "status": "PASS",
                "coverage": 100.0,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert captured["target_ids"] == ["pyfile:src/app.py"]
    assert captured["commit_paths"] == ["tests/uta_generated/test_app.py", "pyproject.toml"]


def test_rdc_repair_commit_discovers_existing_java_test_when_result_path_missing(monkeypatch, tmp_path):
    from uta.tasks.rdc_delivery import RdcDeliveryContext

    actual_rel = (
        "examine-service/src/test/java/com/example/idss/examine/service/excel/handler/"
        "NewFakeMakeExportHandlerTest.java"
    )
    actual_test = tmp_path / actual_rel
    actual_test.parent.mkdir(parents=True)
    actual_test.write_text("class NewFakeMakeExportHandlerTest {}", encoding="utf-8")
    (tmp_path / "examine-service" / "pom.xml").write_text(
        "<project><artifactId>examine-service</artifactId></project>\n", encoding="utf-8"
    )

    captured = {}

    class FakeManager:
        def record_push_failed(self, *args, **kwargs):
            raise AssertionError("push should not fail")

    def fake_commit(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("uta.tasks.rdc_delivery.commit_rdc_repair_results", fake_commit)
    class_fqn = "com.example.idss.ims.examine.service.excel.handler.NewFakeMakeExportHandler"

    out = _commit_rdc_repair_to_branch(
        {
            "repo_path": str(tmp_path),
            "task_id": 14,
            "module": "examine-service",
            "rdc_context": {
                "enforcement": {
                    "evidence": {
                        "targetTests": [
                            "com.example.idss.examine.service.excel.handler.NewFakeMakeExportHandlerTest"
                        ]
                    }
                }
            },
        },
        manager=FakeManager(),
        context=RdcDeliveryContext(branch_name="feature/TASK-1"),
        class_fqns=[class_fqn],
        results={"com.example.idss.ims.examine.service.excel.handler.NewFakeMakeExportHandler": {"status": "PASS"}},
    )

    assert out == {"current_stage": "commit_to_branch"}
    assert captured["target_ids"] == [class_fqn]
    assert captured["commit_paths"] == [actual_rel]


def test_store_and_push_saves_report_without_committing_reports(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "push disabled"

    def fake_git_run(repo_path, *args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr("uta.testgen.delivery._git_run", fake_git_run)
    monkeypatch.setattr("uta.testgen.delivery._push_branch_with_rebase_retry", lambda *args, **kwargs: Result())

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








def test_a_deployed_deepseek_multiplier_is_still_honoured():
    """Nodes set UTA_OPENCODE_DEEPSEEK_TIMEOUT_MULTIPLIER; it must not go dead."""
    from uta.shared.config import Settings

    settings = Settings(opencode_deepseek_timeout_multiplier=3.0)

    assert settings.opencode_provider_timeout_multipliers == "deepseek=3.0"


def test_an_explicit_provider_map_wins_over_the_deprecated_key():
    """If an operator wrote the general form, they meant it."""
    from uta.shared.config import Settings

    settings = Settings(
        opencode_deepseek_timeout_multiplier=3.0,
        opencode_provider_timeout_multipliers="openrouter=1.5",
    )

    assert settings.opencode_provider_timeout_multipliers == "openrouter=1.5"


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


def test_scan_and_select_refuses_ci_incremental_without_explicit_diff_targets(monkeypatch):
    monkeypatch.setattr(
        "uta.language.java.candidates.get_changed_source_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ci_incremental must not scan history")),
    )

    out = scan_and_select(
        {
            "repo_path": "/tmp/repo",
            "days": 30,
            "module": "biz",
            "max_files": 10,
            "select_all_files": False,
            "explicit_class_fqns": [],
            "quality_mode": "ci_incremental",
            "phase_timings": {},
        }  # type: ignore[arg-type]
    )

    assert "requires explicit diff target classes" in out["error"]
    assert out["current_stage"] == "scan_candidates"


def test_scan_and_select_all_files_bypasses_git_history(monkeypatch):
    monkeypatch.setattr(
        "uta.language.java.candidates.get_all_source_files",
        lambda language, repo_path, module: [
            ("biz/src/main/java/pkg/A.java", 1),
            ("biz/src/main/java/pkg/B.java", 1),
        ],
    )
    monkeypatch.setattr(
        "uta.language.java.candidates.get_changed_source_files",
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

    class DummyContextProvider:
        def __init__(self, *_args):
            pass

        def export_project_context(self):
            pass

    monkeypatch.setattr("uta.language.java.candidates.make_parse_provider", lambda _language: DummyParseProvider())
    monkeypatch.setattr("uta.language.java.candidates.JavaContextProvider", DummyContextProvider)
    monkeypatch.setattr("uta.language.java.candidates.sync_project_summaries", lambda *_args, **_kwargs: None)

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
    fqn = "com.example.provider.listener.WMQConsumerRegister"
    methods = ["receiptUpFlowListener", "batchReceiptUpFlowListener", "orderExpireListener"]
    graph = _testability_graph(
        fqn,
        "/repo/provider/src/main/java/com/example/provider/listener/WMQConsumerRegister.java",
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
        method_annotations={method: ["WMQConsumer"] for method in methods},
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






















def test_delegated_quality_gate_uses_rdc_command(tmp_path):
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=1,
            stdout="test-enforcer check-coverage failed: diff line coverage 90.00% is below required 95.00%",
            stderr="",
        )

    result = _run_delegated_quality_gate_once(
        {
            "rdc_context": {
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
    assert result["summary"] == (
        "UTA test-enforcement failed: Coverage gate failed: 90.00% < 95.00%"
    )


def test_delegated_gate_result_discovers_existing_java_test_with_non_mirrored_package(tmp_path):
    repo = tmp_path
    actual_test = repo / "examine-service" / "src" / "test" / "java" / "com" / "example" / "idss" / "examine" / "service" / "excel" / "handler" / "NewFakeMakeExportHandlerTest.java"
    actual_test.parent.mkdir(parents=True)
    actual_test.write_text(
        "package com.example.idss.examine.service.excel.handler;\nclass NewFakeMakeExportHandlerTest {}\n",
        encoding="utf-8",
    )
    wrong_mirrored = repo / "examine-service" / "src" / "test" / "java" / "com" / "example" / "idss" / "ims" / "examine" / "service" / "excel" / "handler" / "NewFakeMakeExportHandlerTest.java"
    assert not wrong_mirrored.exists()
    (repo / "examine-service" / "pom.xml").write_text(
        "<project><artifactId>examine-service</artifactId></project>\n", encoding="utf-8"
    )

    source = repo / "examine-service" / "src" / "main" / "java" / "com" / "example" / "idss" / "ims" / "examine" / "service" / "excel" / "handler" / "NewFakeMakeExportHandler.java"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example.idss.ims.examine.service.excel.handler;\nclass NewFakeMakeExportHandler {}\n", encoding="utf-8")

    class_fqn = "com.example.idss.ims.examine.service.excel.handler.NewFakeMakeExportHandler"
    state = {
        "repo_path": str(repo),
        "quality_mode": "ci_incremental",
        "quality_gate_backend": "maven_enforcer",
        "graph": SimpleNamespace(nodes={class_fqn: SimpleNamespace(file_path=str(source))}),
        "rdc_context": {
            "enforcement": {
                "evidence": {
                    "targetTests": ["com.example.idss.examine.service.excel.handler.NewFakeMakeExportHandlerTest"]
                }
            }
        },
    }

    paths = _expected_ci_incremental_java_test_paths(state, [class_fqn])
    actual_rel = "examine-service/src/test/java/com/example/idss/examine/service/excel/handler/NewFakeMakeExportHandlerTest.java"
    mirrored_rel = "examine-service/src/test/java/com/example/idss/ims/examine/service/excel/handler/NewFakeMakeExportHandlerTest.java"
    assert paths[0] == actual_rel
    assert actual_rel in paths
    assert mirrored_rel in paths

    results = _delegated_gate_batch_results(
        state=state,
        repo_path=str(repo),
        module="examine-service",
        batch=[class_fqn],
        gate_result={
            "passed": True,
            "status": "passed",
            "summary": "UTA test-enforcement passed",
            "stdout": "[INFO] BUILD SUCCESS",
            "stderr": "",
        },
        gate_seconds=3.0,
        status="PASS",
    )

    assert results[class_fqn]["test_file_path"] == actual_rel
    assert results[class_fqn]["candidate_test_file_paths"] == [actual_rel]
    assert "NewFakeMakeExportHandlerTest" in results[class_fqn]["test_file_content"]


def test_delegated_gate_allowed_paths_filter_rdc_target_tests_to_active_batch(tmp_path):
    repo = tmp_path
    class_fqn = "com.example.BeijingShopResolverImpl"
    source = repo / "svc" / "src" / "main" / "java" / "com" / "example" / "BeijingShopResolverImpl.java"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example;\nclass BeijingShopResolverImpl {}\n", encoding="utf-8")
    state = {
        "repo_path": str(repo),
        "quality_mode": "ci_incremental",
        "quality_gate_backend": "maven_enforcer",
        "graph": SimpleNamespace(nodes={class_fqn: SimpleNamespace(file_path=str(source))}),
        "rdc_context": {
            "enforcement": {
                "evidence": {
                    "targetTests": [
                        "com.example.BeijingShopResolverImplTest",
                        "com.example.ReportDedupBizImplTest",
                        "com.example.NoticeUtilTest",
                    ]
                }
            }
        },
    }

    paths = _expected_ci_incremental_java_test_paths(state, [class_fqn])

    assert "src/test/java/com/example/BeijingShopResolverImplTest.java" in paths
    assert "src/test/java/com/example/ReportDedupBizImplTest.java" not in paths
    assert "src/test/java/com/example/NoticeUtilTest.java" not in paths


def test_delegated_gate_failure_result_includes_specific_gate_reason(tmp_path):
    repo = tmp_path
    source = repo / "svc" / "src" / "main" / "java" / "com" / "example" / "Sample.java"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example;\nclass Sample {}\n", encoding="utf-8")
    class_fqn = "com.example.Sample"
    state = {
        "repo_path": str(repo),
        "quality_mode": "ci_incremental",
        "quality_gate_backend": "maven_enforcer",
        "graph": SimpleNamespace(nodes={class_fqn: SimpleNamespace(file_path=str(source))}),
        "results": {},
    }

    results = _delegated_gate_batch_results(
        state=state,
        repo_path=str(repo),
        module="svc",
        batch=[class_fqn],
        gate_result={
            "passed": False,
            "status": "failed",
            "summary": "UTA test-enforcement failed",
            "stdout": (
                "[ERROR] Tests run: 107, Failures: 0, Errors: 5, Skipped: 0\n"
                "[ERROR]   NoticeUtilTest.init:42 RuntimeException\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.12:check-coverage "
                "on project svc: test-enforcer check-coverage failed: diff line coverage "
                "91.00% is below required 95.00% (1031/1133)\n"
            ),
            "stderr": "",
        },
        gate_seconds=3.0,
        status="FAIL",
    )

    error = results[class_fqn]["error"]
    assert "Coverage gate failed" in error
    assert "91.00% < 95.00%" in error
    assert "1031/1133" in error
    assert "Selected test run failed" in error
    assert "NoticeUtilTest" in error


def test_delegated_gate_failure_stage_prefers_explicit_coverage_failure_over_pit_noise():
    stage = _delegated_gate_failure_stage(
        {
            "summary": "UTA test-enforcement failed",
            "stdout": (
                "[test-enforcer] pitest.targets=16 [com.example.Sample]\n"
                "PIT generated=40 killed=40 survived=0 test-strength=100.00%\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.12:check-coverage "
                "on project svc: test-enforcer check-coverage failed: diff line coverage "
                "88.62% is below required 95.00% (1005/1134)\n"
            ),
            "stderr": "",
        }
    )

    assert stage == "coverage_fix"










def test_delegated_gate_failure_ownership_rejects_other_module(tmp_path):
    state = {
        "repo_path": str(tmp_path),
        "graph": SimpleNamespace(nodes={
            "com.demo.NewFakeMakeExportHandler": SimpleNamespace(
                file_path=str(tmp_path / "examine-service" / "src" / "main" / "java" / "com" / "demo" / "NewFakeMakeExportHandler.java")
            )
        }),
    }
    result = {
        "passed": False,
        "summary": "UTA test-enforcement failed",
        "stdout": (
            "[INFO] examine-service .................................... SUCCESS\n"
            "[INFO] examine-biz ........................................ FAILURE\n"
            "[INFO] Running com.demo.NewFakeMakeExportHandlerTest\n"
            "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:check-coverage "
            "on project examine-biz: diff line coverage 16.67% is below required 95.00%\n"
            "[ERROR]   InstructionReachRuleBizTest.linkProcessPassesOpinionEntryToFlowOrderAdapter:47"
        ),
        "stderr": "",
    }

    assert _delegated_gate_failure_matches_batch(
        state,
        str(tmp_path),
        result,
        ["com.demo.NewFakeMakeExportHandler"],
    ) is False


def test_delegated_gate_annotates_other_module_failure(tmp_path):
    class_fqn = "com.demo.NewFakeMakeExportHandler"
    state = {
        "repo_path": str(tmp_path),
        "graph": SimpleNamespace(nodes={
            class_fqn: SimpleNamespace(
                file_path=str(tmp_path / "examine-service" / "src" / "main" / "java" / "com" / "demo" / "NewFakeMakeExportHandler.java")
            )
        }),
    }
    other_module_failure = {
        "passed": False,
        "status": "failed",
        "summary": "UTA test-enforcement failed",
        "stdout": (
            "[INFO] examine-service .................................... SUCCESS\n"
            "[INFO] examine-biz ........................................ FAILURE\n"
            "[ERROR] Failed to execute goal org.pitest:pitest-maven:mutationCoverage "
            "on project examine-biz: Test strength score of 75 is below threshold of 100\n"
        ),
        "stderr": "",
    }

    annotated = _annotate_out_of_scope_gate_failure(
        other_module_failure,
        state=state,
        repo_path=str(tmp_path),
        batch=[class_fqn],
    )

    assert annotated["passed"] is False
    assert annotated["status"] == "out_of_scope_gate_failure"
    assert annotated["outOfScopeGateFailure"]["failedModules"] == ["examine-biz"]


def test_delegated_gate_failure_ownership_matches_maven_artifact_id(tmp_path):
    module = tmp_path / "databus"
    module.mkdir()
    (module / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><artifactId>cvs-order-man.databus</artifactId></project>\n",
        encoding="utf-8",
    )
    source = module / "src" / "main" / "java" / "com" / "example" / "cvs" / "order" / "man" / "databus" / "biz" / "DataBusSyncBiz.java"
    source.parent.mkdir(parents=True)
    source.write_text("package com.example.cvs.order.man.databus.biz; class DataBusSyncBiz {}\n", encoding="utf-8")

    class_fqn = "com.example.cvs.order.man.databus.biz.DataBusSyncBiz"
    state = {
        "repo_path": str(tmp_path),
        "graph": SimpleNamespace(nodes={class_fqn: SimpleNamespace(file_path=str(source))}),
    }
    result = {
        "passed": False,
        "summary": "UTA test-enforcement failed",
        "stdout": (
            "[INFO] cvs-order-man.databus ......................... FAILURE\n"
            "[ERROR] Failed to execute goal org.pitest:pitest-maven:1.15.0:mutationCoverage "
            "on project cvs-order-man.databus: Test strength score of 49 is below threshold of 100\n"
        ),
        "stderr": "",
    }

    assert _delegated_gate_failure_matches_batch(state, str(tmp_path), result, [class_fqn]) is True


def test_out_of_scope_gate_annotation_is_json_serializable(tmp_path):
    class_fqn = "com.demo.Service"
    state = {
        "repo_path": str(tmp_path),
        "graph": SimpleNamespace(nodes={
            class_fqn: SimpleNamespace(
                file_path=str(tmp_path / "service" / "src" / "main" / "java" / "com" / "demo" / "Service.java")
            )
        }),
    }
    result = {
        "passed": False,
        "summary": "UTA test-enforcement failed",
        "stdout": "[ERROR] Failed to execute goal plugin on project other-service: diff coverage failed",
        "stderr": "",
    }

    annotated = _annotate_out_of_scope_gate_failure(result, state=state, repo_path=str(tmp_path), batch=[class_fqn])

    assert annotated["passed"] is False
    assert annotated["status"] == "out_of_scope_gate_failure"
    assert annotated["outOfScopeGateFailure"]["failedModules"] == ["other-service"]
    json.dumps(annotated)








































def test_push_retry_aborts_stale_rebase_before_rebase(monkeypatch, tmp_path):
    import uta.testgen.delivery as nodes
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


def test_workflow_git_run_uses_git_access_token_identity(monkeypatch, tmp_path):
    import subprocess
    import uta.language.java.generation as nodes

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("uta.shared.config.settings.ci_git_access_token", "secret-token")
    monkeypatch.setattr("uta.shared.config.settings.ci_git_ssh_key_path", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    nodes._git_run(str(tmp_path), "push", "-u", "origin", "feature/demo", capture_output=True)

    _cmd, kwargs = calls[0]
    env = kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in env["GIT_CONFIG_VALUE_0"]












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
    from uta.language.java.generation import _load_generation_plan_for_resume

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
    from uta.language.java.generation import _load_generation_plan_for_resume

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
    from uta.language.java.generation import _load_generation_plan_for_resume

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








def test_relax_surefire_skiptests_rewrites_hardcoded_true(tmp_path):
    # Moved to `uta.language.java.maven.surefire`: it is applied before every
    # Maven test run now, not once during baseline setup.
    from uta.language.java.maven.surefire import (
        relax_surefire_skiptests as _relax_surefire_skiptests,
    )

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
    from uta.language.java.maven.surefire import (
        relax_surefire_skiptests as _relax_surefire_skiptests,
    )

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


WEAK_JAVA_TEST_SOURCE = """
public class OrderServiceTest {
  @Test
  public void returnsResult() {
    assertNotNull(service.run());
  }
}
"""


def test_sync_task_results_attaches_java_test_quality(tmp_path):
    from uta.tasks.manager import TaskManager
    from uta.tasks.render import build_status_payload

    repo = tmp_path / "repo"
    test_rel = "svc/src/test/java/com/demo/OrderServiceTest.java"
    test_file = repo / test_rel
    test_file.parent.mkdir(parents=True)
    test_file.write_text(WEAK_JAVA_TEST_SOURCE, encoding="utf-8")

    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["com.demo.OrderService"], branch_name=None)
    manager.mark_running(task_id)

    _sync_task_results_if_available(
        {
            "task_id": task_id,
            "task_db_path": str(tmp_path / "tasks.db"),
            "repo_path": str(repo),
        },
        {
            "com.demo.OrderService": {
                "status": "PASS",
                "coverage": 100.0,
                "mutation_score": 100.0,
                "test_file_path": test_rel,
            }
        },
        ["com.demo.OrderService"],
    )

    payload = build_status_payload(manager.db, task_id)
    row = payload["classes"][0]
    assert row["test_quality"]["warningCount"] == 2
    assert "java-weak-not-null" in row["test_quality_warning"]


def test_run_java_batch_generation_attaches_test_quality(tmp_path):
    from uta.language.java.batch import JavaBatchGenerationRequest, run_java_batch_generation

    repo = tmp_path / "repo"
    test_rel = "svc/src/test/java/com/demo/OrderServiceTest.java"
    test_file = repo / test_rel
    test_file.parent.mkdir(parents=True)
    test_file.write_text(WEAK_JAVA_TEST_SOURCE, encoding="utf-8")

    final_state = {
        "results": {
            "com.demo.OrderService": {"status": "PASS", "test_file_path": test_rel},
            "com.demo.RateLimited": {
                "status": "PROVIDER_RATE_LIMITED",
                "test_file_path": "svc/src/test/java/com/demo/NeverCreatedTest.java",
            },
        },
        "session_ids": [],
        "error": None,
    }
    request = JavaBatchGenerationRequest.from_class_fqns(repo_path=repo, class_fqns=["com.demo.OrderService"])
    from uta.app.persistence import register_task_persistence

    register_task_persistence()
    result = run_java_batch_generation(request, workflow_app=SimpleNamespace(invoke=lambda state: final_state))

    assert result.results["com.demo.OrderService"]["testQuality"]["warningCount"] == 2
    assert "testQuality" not in result.results["com.demo.RateLimited"]


def test_delegated_gate_failure_ownership_rejects_other_test_compile_error_in_same_module(tmp_path):
    source = (
        tmp_path
        / "provider"
        / "src"
        / "main"
        / "java"
        / "com"
        / "demo"
        / "SignalCheckResultListener.java"
    )
    source.parent.mkdir(parents=True)
    source.write_text("package com.demo; class SignalCheckResultListener {}\n", encoding="utf-8")
    (tmp_path / "provider" / "pom.xml").write_text(
        "<project><artifactId>provider</artifactId></project>\n",
        encoding="utf-8",
    )
    class_fqn = "com.demo.SignalCheckResultListener"
    state = {
        "repo_path": str(tmp_path),
        "graph": SimpleNamespace(nodes={class_fqn: SimpleNamespace(file_path=str(source))}),
    }
    result = {
        "passed": False,
        "summary": "UTA test-enforcement failed because Maven build did not compile or resolve",
        "stdout": (
            "[ERROR] COMPILATION ERROR :\n"
            f"[ERROR] {tmp_path}/provider/src/test/java/com/demo/ChatRobotServiceTest.java:[56,37] "
            "error: cannot find symbol\n"
            "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.6.0:testCompile "
            "on project provider: Compilation failure\n"
        ),
        "stderr": "",
    }

    assert _delegated_gate_failure_matches_batch(state, str(tmp_path), result, [class_fqn]) is False
