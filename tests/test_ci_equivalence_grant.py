"""A fix session's fresh rerun may be excused only by the reviews saved before it."""

from __future__ import annotations

import json

import httpx

from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcCallbackClient, RdcProtocol
from uta.app.service import ApiTriggerService
from uta.enforcement.ci import BaseCiLanguageHandler
from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.enforcement.equivalent_mutants import (
    MutantIdentity,
    MutantVerdict,
    ReviewDecision,
    ScoringSurvivors,
)
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest, utc_now
from uta.tasks.manager import TaskManager

MUTATION_ONLY = {"tests_passed": True, "coverage_passed": True, "mutation_only_failure": True}


def _request() -> CiTriggerRequest:
    return CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
            "jiraId": "TASK-82767",
            "taskId": "rdc-task-1",
            "recordId": "record-1",
            "parentId": "parent-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
            "operator": "dev-user",
        }
    )


def _survivors(*keys, fingerprint="sha-a"):
    return ScoringSurvivors(
        language="java",
        mutants=tuple(MutantIdentity(key, "src/main/java/pkg/A.java", 7, "RETURN_VALS", "a") for key in keys),
        unreviewed_scoring_failures=0,
        source_fingerprints={"src/main/java/pkg/A.java": fingerprint},
        mutation_rate=66.67,
        mutation_gate=100.0,
    )


def _review(survivors, outcome="all_equivalent"):
    verdicts = tuple(
        MutantVerdict(m.key, "equivalent", "the whole region", "same result for every input", (7,))
        for m in survivors.mutants
    )
    return ReviewDecision(outcome, "", survivors, verdicts, "sha", {"session_id": "ses_1"}, "pool/model").to_dict()


class _Runner:
    def __init__(self):
        self.calls = 0

    def run(self, repo_path):
        self.calls += 1
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=["mvn"],
            summary="UTA test-enforcement failed: diff mutation score 66.67% below gate",
        )


class _Handler(BaseCiLanguageHandler):
    language = "java"
    quality_gate_backend = "maven_enforcer"

    def __init__(self, runner, fresh, flags=MUTATION_ONLY):
        super().__init__(runner)
        self.fresh = fresh
        self.flags = flags

    def scoring_survivors(self, *, record, result, repo_path):
        if isinstance(self.fresh, Exception):
            raise self.fresh
        return self.fresh

    def gate_failure_flags(self, *, record, result):
        return self.flags


def _service(tmp_path, *, review, fresh, flags=MUTATION_ONLY, rows=None):
    bodies = []

    def capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read().decode("utf-8")))
        return httpx.Response(200, text="ok")

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    manager = TaskManager(tmp_path / "tasks.db")
    rows = rows or {"pkg.A": {"status": "FAIL", "equivalence_review": review}}
    repo_task_id = manager.create_task(repo_path=str(repo), class_fqns=sorted(rows))
    manager.mark_running(repo_task_id)
    manager.sync_results(repo_task_id, rows)
    manager.mark_completed(repo_task_id, message="repair finished")

    runner = _Runner()
    service = ApiTriggerService(
        task_manager=manager,
        protocols=ProtocolRegistry([
            RdcProtocol(
                callback_client=RdcCallbackClient(
                    ack_url="http://rdc/plugin/ack", transport=httpx.MockTransport(capture), sleep=lambda _: None,
                )
            )
        ]),
        language_handlers=[_Handler(runner, fresh, flags)],
    )
    now = utc_now().isoformat()
    record = CiTaskRecord(
        task_id="ci-1",
        status=CiTaskStatus.failed,
        request=_request(),
        protocol="rdc",
        workspace_path=str(repo),
        enforcement_result={"status": "failed", "passed": False, "language": "java"},
        fix_sessions=[{
            "sessionId": "s1", "status": "repair_task_created", "repoTaskId": repo_task_id,
            "selectedTargets": [], "messages": [], "retryCount": 0, "createdAt": now, "updatedAt": now,
        }],
    )
    service._tasks[record.task_id] = record
    return service, record, runner, bodies


def test_matching_rerun_is_granted_once_and_stays_visible(tmp_path):
    reviewed = _survivors("m1", "m2")
    service, record, runner, bodies = _service(tmp_path, review=_review(reviewed), fresh=_survivors("m2", "m1"))

    for _ in range(3):
        service.get(record.task_id)
    service.repair._refresh_repair_session_summaries(record)

    session = record.fix_sessions[0]
    assert session["status"] == "passed_with_equivalent_mutants"
    assert record.status == CiTaskStatus.success
    assert record.enforcement_result["passed"] is False
    assert record.gate_override["kind"] == "equivalent_mutants"
    assert record.gate_override["rawMutationRate"] == 66.67
    assert record.gate_override["mutants"] == 2
    assert session["equivalenceOverride"]["reviews"][0]["verdicts"][0]["key"] in {"m1", "m2"}
    assert "equivalent-mutant override" in record.summary
    assert runner.calls == 1
    assert [body["state"] for body in bodies] == [0]


def test_changed_survivors_keep_the_normal_failure(tmp_path):
    service, record, _runner, bodies = _service(
        tmp_path, review=_review(_survivors("m1")), fresh=_survivors("m1", "m9"),
    )

    service.get(record.task_id)
    service.get(record.task_id)

    assert record.fix_sessions[0]["status"] == "rerun_failed"
    assert record.status == CiTaskStatus.failed
    assert record.gate_override is None
    assert [body["state"] for body in bodies] == [-1024]


def test_no_review_or_unknown_flags_keep_the_normal_failure(tmp_path):
    service, record, _runner, bodies = _service(tmp_path, review=None, fresh=_survivors("m1"))
    service.get(record.task_id)
    assert record.fix_sessions[0]["status"] == "rerun_failed"
    assert [body["state"] for body in bodies] == [-1024]

    service, record, _runner, bodies = _service(
        tmp_path / "unknown", review=_review(_survivors("m1")), fresh=_survivors("m1"), flags=None,
    )
    service.get(record.task_id)
    assert record.fix_sessions[0]["status"] == "rerun_failed"
    assert [body["state"] for body in bodies] == [-1024]


def test_raw_score_comes_from_the_fresh_rerun_not_the_review(tmp_path):
    reviewed = _survivors("m1")
    fresh = ScoringSurvivors(
        language="java", mutants=reviewed.mutants, unreviewed_scoring_failures=0,
        source_fingerprints=dict(reviewed.source_fingerprints), mutation_rate=50.0, mutation_gate=100.0,
    )
    service, record, _runner, bodies = _service(tmp_path, review=_review(reviewed), fresh=fresh)

    service.get(record.task_id)

    assert record.gate_override["rawMutationRate"] == 50.0
    assert "50.0%" in record.summary
    assert [body["state"] for body in bodies] == [0]


def test_passed_units_need_no_review_but_every_failing_one_does(tmp_path):
    reviewed = _survivors("m1")
    rows = {
        "pkg.A": {"status": "FAIL", "equivalence_review": _review(reviewed)},
        "pkg.B": {"status": "PASS", "equivalence_review": None},
    }
    service, record, _runner, bodies = _service(tmp_path, review=None, fresh=reviewed, rows=rows)
    service.get(record.task_id)
    assert record.fix_sessions[0]["status"] == "passed_with_equivalent_mutants"

    rows["pkg.C"] = {"status": "FAIL", "equivalence_review": None}
    service, record, _runner, bodies = _service(tmp_path / "unreviewed", review=None, fresh=reviewed, rows=rows)
    service.get(record.task_id)
    assert record.fix_sessions[0]["status"] == "rerun_failed"
    assert [body["state"] for body in bodies] == [-1024]


def test_an_error_while_granting_falls_back_to_the_normal_failure(tmp_path):
    service, record, _runner, bodies = _service(
        tmp_path, review=_review(_survivors("m1")), fresh=OSError("source vanished"),
    )

    service.get(record.task_id)

    assert record.fix_sessions[0]["status"] == "rerun_failed"
    assert record.gate_override is None
    assert [body["state"] for body in bodies] == [-1024]


def test_a_later_failing_result_clears_the_override(tmp_path):
    service, record, _runner, _bodies = _service(tmp_path, review=_review(_survivors("m1")), fresh=_survivors("m1"))
    service.get(record.task_id)
    assert record.gate_override is not None

    other = {"sessionId": "s2", "status": "rerun_running", "retryCount": 0, "selectedTargets": [], "messages": []}
    record.fix_sessions.append(other)
    failing = QualityGateResult(status=QualityGateStatus.failed, passed=False, command=["mvn"], summary="red")
    service.repair._apply_repair_enforcement_result(record, other, failing)

    assert record.status == CiTaskStatus.failed
    assert record.gate_override is None
