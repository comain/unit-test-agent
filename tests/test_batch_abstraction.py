from pathlib import Path

from uta.engine.batch import BatchGenerationRequest, BatchGenerationResult
from uta.language.java.batch import (
    JavaBatchGenerationRequest,
    JavaBatchGenerationResult,
    build_java_initial_state,
    run_java_batch_generation,
)
from uta.language.python.batch import PythonBatchGenerationResult
from uta.tasks.targets import TargetIdentity


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


def test_java_batch_generation_facade_invokes_existing_workflow():
    workflow = _FakeWorkflow()
    request = JavaBatchGenerationRequest.from_class_fqns(
        repo_path=Path("/repo"),
        class_fqns=["com.example.Service"],
        module="biz",
        session_id="ses_1",
    )

    result = run_java_batch_generation(request, workflow_app=workflow)

    assert isinstance(result, BatchGenerationResult)
    assert result.results["com.example.Service"]["status"] == "PASS"
    assert result.session_ids == ["ses_java"]
    assert workflow.seen_state["repo_path"] == "/repo"
    assert workflow.seen_state["module"] == "biz"
