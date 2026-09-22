"""An equivalent-mutant override is shown for what it is: a pass that killed nothing."""

from __future__ import annotations

from uta.app.reporting import CiReportRenderer
from uta.app.repair import RepairSessions
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest

VERDICT = {
    "key": "pkg.text.x_text_similarity__mutmut_3",
    "verdict": "equivalent",
    "divergence_region": "exactly one of a, b is empty",
    "why_indistinguishable": "SequenceMatcher and jaccard are both 0.0 there, so 0.0 is returned either way",
    "source_lines": [3, 4],
}


def _record(*, granted: bool) -> CiTaskRecord:
    session = {
        "sessionId": "s1",
        "status": "passed_with_equivalent_mutants" if granted else "rerun_failed",
        "retryCount": 0,
        "selectedTargets": [],
        "messages": [],
    }
    if granted:
        session["equivalenceOverride"] = {
            "decision": {"granted": True, "reason": ""},
            "reviews": [{"unitId": "pyfile:pkg/text.py", "verdicts": [VERDICT], "modelId": "pool/model"}],
            "freshSurvivorKeys": [VERDICT["key"]],
            "rawMutationRate": 90.0,
            "mutationGate": 95.0,
        }
    return CiTaskRecord(
        task_id="ci-1",
        status=CiTaskStatus.success if granted else CiTaskStatus.failed,
        request=CiTriggerRequest(app_name="demo", git_url="git@example.com:g/demo.git", branch="feature/x", language="python"),
        summary="Passed with equivalent-mutant override" if granted else "mutation below gate",
        enforcement_result={"status": "failed", "passed": False, "language": "python", "summary": "mutation below gate"},
        gate_override=(
            {"kind": "equivalent_mutants", "sessionId": "s1", "rawMutationRate": 90.0, "mutationGate": 95.0, "mutants": 1}
            if granted else None
        ),
        fix_sessions=[session],
    )


def test_report_shows_the_override_raw_score_and_reasoning():
    html = CiReportRenderer().report_html(_record(granted=True))

    assert "等价变异豁免" in html
    assert "90.0" in html and "95.0" in html
    assert VERDICT["key"] in html
    assert VERDICT["divergence_region"] in html
    assert VERDICT["why_indistinguishable"] in html
    assert 'class="ok">passed_with_equivalent_mutants' in html


def test_report_without_override_is_unchanged():
    html = CiReportRenderer().report_html(_record(granted=False))

    assert "等价变异豁免" not in html


def test_status_page_and_recent_jobs_name_the_override():
    record = _record(granted=True)

    assert "等价变异豁免" in CiReportRenderer().status_html(record)
    assert CiReportRenderer._recent_job_row(record)["gateOverride"]["mutants"] == 1
    assert CiReportRenderer._recent_job_row(_record(granted=False))["gateOverride"] is None


def test_progress_stages_show_the_review_and_a_done_rerun():
    stages = RepairSessions._repair_progress_stages(
        {"status": "passed_with_equivalent_mutants", "rerunEnforcement": {"passed": False}},
        {
            "task": {"status": "COMPLETED", "current_stage": "finished"},
            "latest_events": [
                {"event_type": "stage_started", "stage": "review_equivalent_mutants", "message": "review"},
            ],
        },
    )

    by_key = {stage["key"]: stage["status"] for stage in stages}
    assert by_key["equivalence_review"] == "done"
    assert by_key["rerun_enforcement"] == "done"
