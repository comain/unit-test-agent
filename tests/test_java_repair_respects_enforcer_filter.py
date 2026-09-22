"""Repair targets must respect the enforcer's own exclusions.

The Maven enforcer decides which changed sources carry a coverage obligation:
it drops pure wrappers, beans and other non-executable sources, publishing what
survives as `filteredChangedProductionFiles`.

When enforcement stops before naming any target -- the "no related targetTests"
case -- repair falls back to the changed classes. That fallback read the
*unfiltered* `changedClasses`, so a fix session generated tests for beans and
wrappers the enforcer had deliberately excluded.
"""

from __future__ import annotations

from uta.language.java.ci import _unfiltered_fallback_classes


def test_the_enforcer_filter_decides_the_fallback_scope():
    evidence = {
        "changedProductionFiles": [
            "svc/src/main/java/com/x/RealService.java",
            "model/src/main/java/com/x/ThingBo.java",
            "model/src/main/java/com/x/ThingVo.java",
        ],
        "filteredChangedProductionFiles": [
            "svc/src/main/java/com/x/RealService.java",
        ],
        "changedClasses": ["com.x.RealService", "com.x.ThingBo", "com.x.ThingVo"],
    }

    assert _unfiltered_fallback_classes(evidence) == ["com.x.RealService"]


def test_an_empty_filter_result_means_no_repair_target():
    """Everything changed was excluded: that is an answer, not a missing one.

    Falling through to the unfiltered list here is what produced tests for
    beans on a diff that needed no tests at all.
    """
    evidence = {
        "filteredChangedProductionFiles": [],
        "changedClasses": ["com.x.ThingBo"],
    }

    assert _unfiltered_fallback_classes(evidence) == []


def test_evidence_without_the_filter_key_still_falls_back():
    """A plugin too old to publish the filtered list must keep working."""
    evidence = {"changedClasses": ["com.x.Legacy", "com.x.Other"]}

    assert _unfiltered_fallback_classes(evidence) == ["com.x.Legacy", "com.x.Other"]


def test_the_snake_case_spelling_is_accepted():
    evidence = {
        "filtered_changed_production_files": ["svc/src/main/java/com/x/RealService.java"],
        "changedClasses": ["com.x.RealService", "com.x.ThingBo"],
    }

    assert _unfiltered_fallback_classes(evidence) == ["com.x.RealService"]


def _record(evidence: dict):
    from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest

    return CiTaskRecord(
        task_id="task-enforcer-filter",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="java-app",
            git_url="git@example.com:group/java-app.git",
            branch="feature/TASK-1",
            language="java",
        ),
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "summary": (
                "UTA test-enforcement cannot run PIT safely because no related "
                "targetTests were found for changed production Java files"
            ),
            "evidence": evidence,
        },
    )


def test_the_handler_does_not_target_excluded_beans(tmp_path):
    """End to end through the selection the fix session actually calls.

    Asserting the helper alone would pass even if nothing called it.
    """
    from uta.language.java.ci import JavaCiLanguageHandler
    from uta.shared.fix_sessions import CreateFixSessionRequest

    record = _record(
        {
            "changedProductionFiles": [
                "svc/src/main/java/com/x/RealService.java",
                "model/src/main/java/com/x/ThingBo.java",
            ],
            "filteredChangedProductionFiles": ["svc/src/main/java/com/x/RealService.java"],
            "changedClasses": ["com.x.RealService", "com.x.ThingBo"],
            "targetTests": [],
        }
    )

    targets = JavaCiLanguageHandler(runner=None).repair_target_ids(
        record=record,
        request=CreateFixSessionRequest(),
        repo_path=tmp_path,
        base_ref="origin/master",
    )

    assert "com.x.RealService" in targets
    assert "com.x.ThingBo" not in targets, "repair targeted a source the enforcer excluded"
