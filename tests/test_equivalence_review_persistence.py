"""A unit's equivalent-mutant review survives completion and reaches the task DB."""

from __future__ import annotations

from types import SimpleNamespace

from uta.enforcement.equivalent_mutants import REVIEW_EVIDENCE_KEY, REVIEW_PHASE
from uta.tasks.manager import TaskManager
from uta.tasks.render import build_status_payload

REVIEW = {
    "outcome": "all_equivalent",
    "reason": "",
    "survivors": {"language": "python", "mutants": [], "unreviewed_scoring_failures": 0,
                  "source_fingerprints": {"pkg/text.py": "sha"}, "mutation_rate": 90.0, "mutation_gate": 95.0},
    "verdicts": [],
    "verdicts_sha256": "abc",
    "agent_session_ref": {},
    "model_id": "pool/model",
}


def _phase_results(**extra):
    return {REVIEW_PHASE: {"phase_outcome": "failed", "evidence": {REVIEW_EVIDENCE_KEY: REVIEW}}, **extra}


def test_python_completion_carries_the_review(tmp_path):
    from uta.language.python.phases import complete_generation

    (tmp_path / "tests").mkdir()
    state = {
        "repo_path": str(tmp_path),
        "target": {"language": "python", "target_id": "pyfile:pkg/text.py", "source_path": "pkg/text.py"},
        "generated_test_path": "tests/test_text.py",
        "phase_results": _phase_results(),
    }

    result = complete_generation(state)["results"]["pyfile:pkg/text.py"]

    assert result[REVIEW_EVIDENCE_KEY] == REVIEW
    del state["phase_results"][REVIEW_PHASE]
    assert complete_generation(state)["results"]["pyfile:pkg/text.py"][REVIEW_EVIDENCE_KEY] is None


def _java_ports():
    return SimpleNamespace(
        status_with_mutation_gate=lambda *args: "FAIL",
        delegated_gate_batch_results=lambda **kwargs: {
            fqn: {"status": kwargs["status"]} for fqn in kwargs["batch"]
        },
    )


def test_java_per_class_completion_carries_the_review(tmp_path):
    from uta.language.java.phases.completion import complete_generation

    state = {"repo_path": str(tmp_path), "batch": ["pkg.A", "pkg.B"], "phase_results": _phase_results()}

    results = complete_generation(state, ports=_java_ports())["results"]

    assert results["pkg.A"][REVIEW_EVIDENCE_KEY] == REVIEW
    assert results["pkg.B"][REVIEW_EVIDENCE_KEY] == REVIEW


def test_java_delegated_completion_carries_the_review(tmp_path):
    from uta.language.java.phases.completion import complete_generation

    delegated = {"evidence": {"quality_gate_result": {"passed": False}}}
    state = {
        "repo_path": str(tmp_path),
        "batch": ["pkg.A"],
        "phase_results": _phase_results(delegated_quality_gate_verify=delegated),
    }

    results = complete_generation(state, ports=_java_ports())["results"]

    assert results["pkg.A"][REVIEW_EVIDENCE_KEY] == REVIEW


def test_sync_results_persists_and_status_payload_exposes_the_review(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    manager.mark_running(task_id)

    manager.sync_results(
        task_id,
        {
            "pkg.A": {"status": "FAIL", REVIEW_EVIDENCE_KEY: REVIEW},
            "pkg.B": {"status": "FAIL", REVIEW_EVIDENCE_KEY: None},
        },
    )

    import json

    rows = {row["class_fqn"]: row for row in manager.list_class_tasks(task_id)}
    assert json.loads(rows["pkg.A"]["equivalence_review_json"]) == REVIEW
    assert rows["pkg.B"]["equivalence_review_json"] is None
    classes = build_status_payload(manager.db, task_id)["classes"]
    assert all("equivalence_review_json" not in row for row in classes)


def test_all_equivalent_reviews_complete_repair_task_for_fresh_gate_rerun(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    manager.mark_running(task_id)

    manager.sync_results(
        task_id,
        {
            "pkg.A": {"status": "MUTATION_FAIL", REVIEW_EVIDENCE_KEY: REVIEW},
            "pkg.B": {"status": "PASS"},
        },
    )

    task = manager.get_task(task_id)
    assert task["status"] == "COMPLETED"
    assert task["current_detail"] == "Equivalent-mutant reviews ready for fresh gate rerun"


def test_unreviewed_mutation_failure_still_fails_repair_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id)

    manager.sync_results(task_id, {"pkg.A": {"status": "MUTATION_FAIL"}})

    assert manager.get_task(task_id)["status"] == "FAILED"
