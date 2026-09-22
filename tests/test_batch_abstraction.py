from pathlib import Path

from uta.testgen.batch import BatchGenerationRequest, BatchGenerationResult
from uta.language.java.batch import (
    JavaBatchGenerationRequest,
    JavaBatchGenerationResult,
    build_java_initial_state,
    run_java_batch_generation,
)
from uta.language.python.batch import PythonBatchGenerationResult, PythonBatchGenerator
from uta.language.python.generation_backend import build_python_initial_state
from uta.shared.targets import TargetIdentity
from uta.tasks.manager import TaskManager
from uta.testgen.runner import BatchGeneratorConfigurationError, run_batch_generation


class _FakeWorkflow:
    def __init__(self):
        self.seen_state = None

    def invoke(self, state):
        self.seen_state = state
        return {
            **state,
            "results": {
                "com.example.Service": {
                    "status": "PASS",
                    "language": "java",
                    "target_id": "com.example.Service",
                }
            },
            "session_ids": ["ses_java"],
            "session_token_usage": {"total_tokens": {"total": 12}},
            "session_retrospect": {"hints": ["java"]},
            "phase_token_usage": {"generate": {"total": 12}},
            "phase_timings": {"auth_probe_seconds": 0.1},
        }


class _FakeGenerator:
    def __init__(self, language):
        self.language = language
        self.seen_request = None

    def run(self, request):
        self.seen_request = request
        return BatchGenerationResult(results={"language": {"status": "PASS"}})


class _FakeAdapter:
    def __init__(self, language, generator):
        self.language = language
        self._generator = generator

    def batch_generator(self):
        return self._generator


class _FakeRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def adapter_for(self, language):
        assert language == self.adapter.language
        return self.adapter


def test_batch_generation_result_is_shared_by_language_results():
    python_result = PythonBatchGenerationResult(results={})
    java_result = JavaBatchGenerationResult(results={})

    assert isinstance(python_result, BatchGenerationResult)
    assert isinstance(java_result, BatchGenerationResult)


def test_python_batch_request_normalizes_targets():
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=Path("/repo"),
        targets=[
            {
                "language": "python",
                "target_id": "pysymbol:jobs/forecast.py::forecast",
                "display_name": "jobs/forecast.py::forecast",
                "source_path": "jobs/forecast.py",
                "symbol": "forecast",
                "granularity": "function",
            }
        ],
    )

    assert request.language == "python"
    assert request.targets[0].target_id == "pysymbol:jobs/forecast.py::forecast"
    assert request.targets[0].source_path == "jobs/forecast.py"


def test_java_batch_initial_state_uses_shared_target_identity():
    request = JavaBatchGenerationRequest.from_class_fqns(
        repo_path=Path("/repo"),
        class_fqns=["com.example.Service"],
        module="biz",
        coverage_gate=80,
        mutation_gate=70,
        model_id="token-pool/claude-opus-5",
        timeout_seconds=901,
        classes_per_run=2,
        session_id="ses_1",
        phase_timings={"auth_probe_seconds": 0.2},
    )

    assert request.targets == [TargetIdentity.java_class("com.example.Service")]
    state = build_java_initial_state(request)
    assert state["language"] == "java"
    assert state["explicit_class_fqns"] == ["com.example.Service"]
    assert state["classes_per_agent_run"] == 2
    assert state["current_batch"] == []
    assert state["session_ids"] == ["ses_1"]
    assert state["phase_timings"] == {"auth_probe_seconds": 0.2}
    assert state["model_id"] == "token-pool/claude-opus-5"
    assert state["timeout_seconds"] == 901
    assert state["planning_timeout_seconds"] == 600
    assert state["compile_fix_timeout_seconds"] == 600
    assert state["repair_timeout_seconds"] == 900


def test_java_batch_generation_facade_invokes_existing_workflow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    workflow = _FakeWorkflow()
    request = JavaBatchGenerationRequest.from_class_fqns(
        repo_path=repo,
        class_fqns=["com.example.Service"],
        module="biz",
        session_id="ses_1",
    )

    result = run_java_batch_generation(request, workflow_app=workflow)

    assert isinstance(result, BatchGenerationResult)
    assert result.results["com.example.Service"]["status"] == "PASS"
    assert result.session_ids == ["ses_java"]
    assert workflow.seen_state["repo_path"] == str(repo)
    assert workflow.seen_state["module"] == "biz"
    scope = workflow.seen_state["backend_context"]["prompt_artifact_scope"]
    assert scope.managed is True
    assert scope.ephemeral_root is not None
    assert len(scope.run_id) == 32
    assert list((runner_home / "standalone-generation").glob("*")) == []


def test_java_managed_batch_defers_prompt_scope_until_batch_identity_exists(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    workflow = _FakeWorkflow()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(repo), class_fqns=["com.example.Service"]
    )
    request = JavaBatchGenerationRequest.from_class_fqns(
        repo_path=repo,
        class_fqns=["com.example.Service"],
        task_id=task_id,
        task_db_path=manager.db_path,
    )

    run_java_batch_generation(request, workflow_app=workflow)

    assert workflow.seen_state["backend_context"]["prompt_artifact_scope"] is None
    assert not (runner_home / "workflow-state" / "prompts").exists()




def test_shared_batch_runner_resolves_backend_through_language_adapter():
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=Path("/repo"),
        targets=[],
    )
    generator = _FakeGenerator("python")
    registry = _FakeRegistry(_FakeAdapter("python", generator))

    result = run_batch_generation(request, registry=registry)

    assert result.results["language"]["status"] == "PASS"
    assert generator.seen_request is request


def test_shared_batch_runner_rejects_mismatched_generator_language():
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=Path("/repo"),
        targets=[],
    )
    registry = _FakeRegistry(_FakeAdapter("python", _FakeGenerator("java")))

    try:
        run_batch_generation(request, registry=registry)
    except BatchGeneratorConfigurationError as exc:
        assert "returned a batch generator" in str(exc)
        assert "java" in str(exc)
    else:
        raise AssertionError("Expected mismatched batch generator language to fail")


def test_python_batch_generator_invokes_shared_workflow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    workflow = _FakeWorkflow()
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=repo,
        targets=[],
    )

    result = PythonBatchGenerator(workflow_app=workflow).run(request)

    assert isinstance(result, PythonBatchGenerationResult)
    assert workflow.seen_state["language"] == "python"
    internal_request = workflow.seen_state["backend_context"]["request"]
    assert internal_request.task_id is not None
    assert internal_request.task_db_path is not None
    scope = workflow.seen_state["backend_context"]["prompt_artifact_scope"]
    assert scope.managed is True
    assert scope.ephemeral_root is not None
    assert list((runner_home / "standalone-generation").glob("*")) == []


def test_python_initial_state_uses_normalized_targets():
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=Path("/repo"),
        targets=[
            {
                "language": "python",
                "target_id": "pysymbol:jobs/run.py::run",
                "display_name": "jobs/run.py::run",
                "source_path": "jobs/run.py",
                "symbol": "run",
                "granularity": "function",
            }
        ],
        model_id="token-pool/claude-opus-5",
        coverage_gate=95.0,
        mutation_gate=100.0,
        timeout_seconds=902,
        quality_mode="ci_incremental",
    )

    state = build_python_initial_state(request)

    assert state["candidates"] == ["pysymbol:jobs/run.py::run"]
    assert state["target_candidates"][0]["source_path"] == "jobs/run.py"
    assert state["current_target_batch"] == []
    assert state["model_id"] == "token-pool/claude-opus-5"
    assert state["coverage_gate"] == 95.0
    assert state["mutation_gate"] == 100.0
    assert state["timeout_seconds"] == 902
    assert state["quality_mode"] == "ci_incremental"
