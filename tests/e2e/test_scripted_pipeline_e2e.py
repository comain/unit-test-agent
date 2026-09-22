from __future__ import annotations

import subprocess
from pathlib import Path

from scripted_agent import scripted_agent_factory
from uta.language.java.batch import JavaBatchGenerationRequest, run_java_batch_generation
from uta.language.java.verification.runner import JavaCoverageSummary, JavaMutationSummary, JavaVerificationResult
from uta.tasks.manager import TaskManager
from uta.shared.targets import TargetIdentity


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def test_scripted_agent_supports_generation_and_repair_responses() -> None:
    factory = scripted_agent_factory(
        "```python\ndef test_generated():\n    assert True\n```",
        {"type": "completed", "result": "```python\ndef test_repaired():\n    assert True\n```"},
    )
    client = factory("/tmp/repo")

    first_session = client.create_session(model_id="test/model", provider_id="test-provider")
    client.send_message_split(first_session, "stable", "volatile", model_id="test/model")
    assert client.poll_completion(first_session)["result"].startswith("```python")

    second_session = client.create_session(model_id="test/model")
    client.send_message(second_session, "repair", model_id="test/model")
    assert "test_repaired" in client.poll_completion(second_session)["result"]

    assert [message["type"] for message in client.messages] == [
        "create_session",
        "send_message_split",
        "create_session",
        "send_message",
    ]


def test_scripted_agent_reports_tokens_and_retrospect() -> None:
    factory = scripted_agent_factory(
        "done",
        token_usage={
            "main_model_tokens": {"input": 1, "output": 2, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 3},
            "small_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "total_tokens": {"input": 1, "output": 2, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 3},
        },
        retrospect={"hints": ["use a narrower target"]},
    )
    client = factory("/tmp/repo")
    session = client.create_session()

    assert client.analyze_session_tokens(session)["total_tokens"]["total"] == 3
    assert client.analyze_session_retrospect(session)["hints"] == ["use a narrower target"]


def test_java_hermetic_scripted_pipeline_generates_repairs_and_completes(tmp_path: Path) -> None:
    repo = tmp_path / "java_project"
    source = repo / "src" / "main" / "java" / "com" / "example" / "orders" / "OrderService.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package com.example.orders;\n\n"
        "public class OrderService {\n"
        "    public int normalize(int quantity) { return Math.max(0, quantity); }\n"
        "}\n",
        encoding="utf-8",
    )
    _init_git_repo(repo)

    class_fqn = "com.example.orders.OrderService"
    target = TargetIdentity.java_class(class_fqn)
    db_path = tmp_path / "java-tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task_targets(repo_path=str(repo), targets=[target], language="java")
    manager.mark_running(task_id, stage="startup")
    workflow = _ScriptedJavaWorkflow(repo=repo, manager=manager, task_id=task_id)
    request = JavaBatchGenerationRequest.from_class_fqns(
        repo_path=repo,
        class_fqns=[class_fqn],
        task_id=task_id,
        task_db_path=db_path,
        coverage_gate=95.0,
        mutation_gate=95.0,
        session_id="scripted-java-session",
    )

    result = run_java_batch_generation(request, workflow_app=workflow)

    generated = repo / "src" / "test" / "java" / "com" / "example" / "orders" / "OrderServiceTest.java"
    assert generated.exists()
    assert "class OrderServiceTest" in generated.read_text(encoding="utf-8")
    assert [attempt.reason_code for attempt in workflow.verification_attempts] == [
        "coverage_gate_failed",
        "passed",
    ]
    assert result.results[class_fqn]["status"] == "PASS"
    assert result.results[class_fqn]["test_file_path"] == "src/test/java/com/example/orders/OrderServiceTest.java"
    assert result.session_ids == ["scripted-java-session", "scripted-java-repair-session"]

    task = manager.get_task(task_id)
    target_row = manager.list_class_tasks(task_id)[0]
    assert task["status"] == "COMPLETED"
    assert target_row["status"] == "PASS"
    assert target_row["coverage_line"] == 100.0
    assert target_row["mutation_score"] == 100.0


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "uta@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "UTA Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)


class _ScriptedJavaWorkflow:
    def __init__(self, *, repo: Path, manager: TaskManager, task_id: int) -> None:
        self.repo = repo
        self.manager = manager
        self.task_id = task_id
        self.verification_attempts: list[JavaVerificationResult] = []

    def invoke(self, state):
        class_fqn = state["explicit_class_fqns"][0]
        target = TargetIdentity.java_class(class_fqn)
        self.manager.record_stage_for_targets(self.task_id, "generate", targets=[target])
        generated = self.repo / "src" / "test" / "java" / "com" / "example" / "orders" / "OrderServiceTest.java"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(
            "package com.example.orders;\n\n"
            "import org.junit.jupiter.api.Test;\n\n"
            "class OrderServiceTest {\n"
            "    @Test void normalizesNegativeQuantity() {\n"
            "        assert new OrderService().normalize(-1) == 0;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        first = JavaVerificationResult(
            status="failed",
            reason_code="coverage_gate_failed",
            tests_pass=True,
            coverage=JavaCoverageSummary(
                line_rate=50.0,
                gate=95.0,
                passed=False,
                xml_path="target/site/jacoco/jacoco.xml",
            ),
        )
        second = JavaVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=JavaCoverageSummary(
                line_rate=100.0,
                gate=95.0,
                passed=True,
                xml_path="target/site/jacoco/jacoco.xml",
            ),
            mutation=JavaMutationSummary(
                generated=2,
                killed=2,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=95.0,
                passed=True,
                report_path="target/pit-reports/mutations.xml",
            ),
        )
        self.verification_attempts.extend([first, second])
        rel_test = generated.relative_to(self.repo).as_posix()
        result = {
            **second.as_result_fields(),
            "language": "java",
            "target_id": class_fqn,
            "display_name": class_fqn,
            "target_granularity": "class",
            "test_file_path": rel_test,
            "session_ids": ["scripted-java-session", "scripted-java-repair-session"],
        }
        self.manager.sync_target_results(self.task_id, {class_fqn: result}, targets=[target], final_error=None)
        return {
            **state,
            "results": {class_fqn: result},
            "session_ids": ["scripted-java-session", "scripted-java-repair-session"],
            "session_token_usage": {"total_tokens": {"total": 18}},
            "session_retrospect": {"hints": ["scripted Java workflow repaired coverage"]},
            "phase_token_usage": {"generate": {"total": 9}, "fix_coverage": {"total": 9}},
            "phase_timings": {"scripted_seconds": 0.01},
            "error": None,
            "finished": True,
        }
