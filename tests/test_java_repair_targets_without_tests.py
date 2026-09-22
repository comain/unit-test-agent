"""A changed class with no test at all must still be repairable.

Beta: both target tests were removed, enforcement failed as it should
(`coverage.passed` false, `mutation.passed` false), and the fix session then
died with

    repair_task_create_failed
    Java CI incremental repair requires explicit or Maven-filtered target
    classes; no safe repair target classes were found in enforcement evidence

`_repair_class_fqns` had six sources and every one of them needs enforcement to
have *reached* the tests: a PIT baseline failure, a compile failure, failed
classes, the Maven-filtered target list, PIT output. With no tests, enforcement
stops before naming any target, so all six came back empty -- and the case that
most needs a repair was the one case repair refused.

The evidence named the classes the whole time, under `changedClasses`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uta.language.java.ci import JavaCiLanguageHandler
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.shared.fix_sessions import CreateFixSessionRequest


CHANGED = [
    "com.example.cvs.ugc.user.biz.impl.CommentCustomerServiceDecision",
    "com.example.cvs.ugc.user.biz.impl.OrderCommentViewBizImpl",
]


def _record(evidence: dict) -> CiTaskRecord:
    return CiTaskRecord(
        task_id="t1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="cvs_ugc_user",
            git_url="git@git.example.com:cvs/cvs-ugc-user.git",
            branch="demo",
            language="java",
        ),
        enforcement_result={"status": "failed", "passed": False, "evidence": evidence},
    )


def _resolve(evidence: dict, target_ids=()) -> list[str]:
    return JavaCiLanguageHandler._repair_class_fqns(
        _record(evidence),
        CreateFixSessionRequest(target_ids=list(target_ids)),
        repo_path=Path("/repo"),
        base_ref="origin/master",
    )


def test_a_class_with_no_test_is_still_a_repair_target():
    """The beta shape: enforcement ran nothing, so only `changedClasses` is
    populated."""
    resolved = _resolve({"changedClasses": CHANGED, "targetTests": []})

    assert resolved == CHANGED


def test_named_targets_still_win():
    """The fallback must not widen a scope somebody stated explicitly."""
    resolved = _resolve(
        {"changedClasses": CHANGED, "targetTests": []},
        target_ids=[f"class:{CHANGED[0]}"],
    )

    assert resolved == [CHANGED[0]]


def test_the_filtered_target_list_still_wins():
    """When enforcement did reach the tests, its own target list is better
    evidence than every changed class."""
    resolved = _resolve(
        {"changedClasses": CHANGED, "filteredTargetClasses": [CHANGED[1]]}
    )

    assert resolved == [CHANGED[1]]


def test_nothing_named_anywhere_still_yields_nothing():
    """The guard stays: an empty result must keep raising rather than invent a
    scope out of nothing."""
    assert _resolve({"targetTests": []}) == []
