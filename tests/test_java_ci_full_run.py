"""Java CI enforcement runs the plugin and takes its verdict.

Phase 1 of docs/plan-java-ci-full-run.md. The CI gate stops running a
filter-diff preflight and re-deriving scope in Python: it invokes Maven once
over the changed modules and reads what `test-enforcer` reports.

Module selection stays -- it comes from the git diff and never needed the
filter-diff preflight. A metadata-only effective-POM call validates versions first.
`-am` keeps the root in the reactor, which matters because only the
root project emits `pitest.targets`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

from uta.enforcement.enforcement import QualityGateStatus
from uta.language.java.enforcement_runner import MavenEnforcementRunner

_PLUGIN_OUTPUT = (
    "[INFO] --- test-enforcer:1.0.16:filter-diff (filter-diff) @ demo ---\n"
    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
    "pitest.targets=1 [com.demo.service.OrderService*]\n"
    "[info] [test-enforcer] no coverable changed lines for common\n"
    "Diff coverage: 100%\n"
    "PIT generated=4 killed=4 survived=0 test-strength=100%\n"
    + DIFF_MUTATION_OK
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (repo / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    for module in ("common", "service"):
        (repo / module).mkdir()
        (repo / module / "pom.xml").write_text(
            "<project><artifactId>%s</artifactId></project>\n" % module, encoding="utf-8"
        )
    bean = repo / "common/src/main/java/com/demo/common/OrderBo.java"
    prod = repo / "service/src/main/java/com/demo/service/OrderService.java"
    test = repo / "service/src/test/java/com/demo/service/OrderServiceTest.java"
    for path in (bean, prod, test):
        path.parent.mkdir(parents=True, exist_ok=True)
    bean.write_text("package com.demo.common; class OrderBo {}\n", encoding="utf-8")
    prod.write_text("package com.demo.service; class OrderService {}\n", encoding="utf-8")
    test.write_text(
        "package com.demo.service;\n"
        "import org.junit.Test;\n"
        "import static org.junit.Assert.assertEquals;\n"
        "public class OrderServiceTest {\n"
        "  @Test public void computesTheTotal() { assertEquals(1, 1); }\n"
        "}\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    bean.write_text("package com.demo.common; class OrderBo { int v; }\n", encoding="utf-8")
    prod.write_text("package com.demo.service; class OrderService { int v; }\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "change")
    return repo


def _runner(calls, *, output=_PLUGIN_OUTPUT, returncode=0, **kwargs):
    @with_resolved_enforcer
    def fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout=output, stderr="")

    return MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
        full_run=True,
        **kwargs,
    )


# T1.1 -----------------------------------------------------------------

def test_full_run_makes_one_enforcement_call_with_no_diff_preflight(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    _runner(calls).run(repo)

    assert len(calls) == 1, "the filter-diff preflight must not run: %r" % (calls,)
    cmd = calls[0]
    assert "initialize" not in cmd
    assert "-N" not in cmd


def test_full_run_lets_the_plugin_decide_targets(tmp_path):
    """Full suite test selection does not override the plugin's production scope."""
    repo = _repo(tmp_path)
    calls = []

    _runner(calls).run(repo)

    joined = " ".join(calls[0])
    assert "-DtargetTests=*" in joined
    assert "-Dtest.enforcement.targetSources" not in joined


def test_full_run_keeps_git_derived_module_scoping(tmp_path):
    """Module selection comes from the diff, not the preflight, and -am keeps
    the root in the reactor so pitest.targets is emitted."""
    repo = _repo(tmp_path)
    calls = []

    _runner(calls).run(repo)

    cmd = calls[0]
    assert "-pl" in cmd
    modules = cmd[cmd.index("-pl") + 1].split(",")
    assert sorted(modules) == ["common", "service"]
    assert "-am" in cmd


# T1.3 -----------------------------------------------------------------

def test_a_run_without_the_plugin_marker_is_not_a_pass(tmp_path):
    """The vacuous-pass guard. Maven exiting 0 without test-enforcer having run
    proves nothing was enforced."""
    repo = _repo(tmp_path)
    calls = []

    result = _runner(calls, output="[INFO] BUILD SUCCESS\n").run(repo)

    assert result.status is not QualityGateStatus.passed
    assert result.status is QualityGateStatus.missing_evidence


def test_a_run_with_the_plugin_marker_is_evaluated_normally(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    result = _runner(calls).run(repo)

    assert result.status is QualityGateStatus.passed


# T1.5 -----------------------------------------------------------------

def test_evidence_carries_the_plugin_target_list(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    result = _runner(calls).run(repo)
    evidence = result.evidence or {}

    assert evidence["filteredTargetClasses"] == ["com.demo.service.OrderService"]
    assert evidence["filteredChangedProductionFiles"] == [
        "service/src/main/java/com/demo/service/OrderService.java"
    ]
    assert "common/src/main/java/com/demo/common/OrderBo.java" not in (
        evidence["filteredChangedProductionFiles"]
    )


def test_evidence_still_names_the_target_tests(tmp_path):
    """app/context.py and ci_evidence.py read targetTests; it is now derived
    from the plugin's filtered list rather than the preflight's."""
    repo = _repo(tmp_path)
    calls = []

    result = _runner(calls).run(repo)

    assert result.evidence["targetTests"] == ["com.demo.service.OrderServiceTest"]


# T2.2 -----------------------------------------------------------------

def test_explicit_target_scope_skips_the_preflight_too(tmp_path):
    """Repair already knows its target: it was chosen from the plugin's list
    and baked into the command. Running filter-diff again to re-derive a scope
    the caller supplied costs a whole Maven invocation and answers nothing."""
    repo = _repo(tmp_path)
    calls = []

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true -DtargetTests=com.demo.service.OrderServiceTest verify",
        run_command=with_resolved_enforcer(lambda cmd, *a, **kw: (
            calls.append(list(cmd)),
            subprocess.CompletedProcess(cmd, 0, stdout=_PLUGIN_OUTPUT, stderr=""),
        )[1]),
        preserve_explicit_target_scope=True,
    ).run(repo)

    assert len(calls) == 1, "the preflight must not run for an explicit scope: %r" % (calls,)
    assert "initialize" not in calls[0]
    assert result.status is QualityGateStatus.passed


def test_explicit_target_scope_keeps_every_changed_source_in_scope(tmp_path):
    """Nothing is dropped: the caller's scope is the scope."""
    repo = _repo(tmp_path)
    calls = []

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=with_resolved_enforcer(lambda cmd, *a, **kw: (
            calls.append(list(cmd)),
            subprocess.CompletedProcess(cmd, 0, stdout=_PLUGIN_OUTPUT, stderr=""),
        )[1]),
        preserve_explicit_target_scope=True,
    ).run(repo)

    assert sorted(result.evidence["filteredChangedProductionFiles"]) == [
        "common/src/main/java/com/demo/common/OrderBo.java",
        "service/src/main/java/com/demo/service/OrderService.java",
    ]


# T2.1 -----------------------------------------------------------------

def _repair_targets(evidence):
    from uta.language.java.ci import JavaCiLanguageHandler
    from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
    from uta.shared.fix_sessions import CreateFixSessionRequest

    record = CiTaskRecord(
        task_id="t1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="w_idss_ice_ddp",
            git_url="git@git.example.com:IDSS/ice-ddp.git",
            branch="demo",
            language="java",
        ),
        enforcement_result={"status": "failed", "passed": False, "evidence": evidence},
    )
    return JavaCiLanguageHandler._repair_class_fqns(
        record,
        CreateFixSessionRequest(target_ids=[]),
        repo_path=Path("/repo"),
        base_ref="origin/master",
    )


def test_repair_targets_are_the_classes_the_plugin_named(tmp_path):
    """The ice-ddp case: ten changed files, five of them beans and adapters the
    plugin excluded. Repair must work on the five the plugin kept, or the fix
    session writes tests for beans again."""
    evidence = {
        "changedClasses": [
            "com.example.idss.ice.model.bo.DecreaseFeqConfigBo",
            "com.example.idss.ice.service.HotConfigCleanPlanService",
        ],
        "filteredTargetClasses": ["com.example.idss.ice.service.HotConfigCleanPlanService"],
        "filteredChangedProductionFiles": [
            "ddp-service/src/main/java/com/example/idss/ice/service/HotConfigCleanPlanService.java"
        ],
    }

    assert _repair_targets(evidence) == ["com.example.idss.ice.service.HotConfigCleanPlanService"]


def test_an_empty_filtered_list_is_an_answer_not_a_missing_one(tmp_path):
    """Present-but-empty means nothing carries an obligation. Falling back to
    the unfiltered classes here is what sent repair at the beans."""
    evidence = {
        "changedClasses": ["com.example.idss.ice.model.bo.DecreaseFeqConfigBo"],
        "filteredTargetClasses": [],
        "filteredChangedProductionFiles": [],
    }

    assert _repair_targets(evidence) == []


def test_evidence_without_the_key_still_falls_back(tmp_path):
    """A plugin too old to publish the list is a missing answer, not an empty
    one -- repair falls back to the changed classes rather than doing nothing."""
    evidence = {"changedClasses": ["com.example.idss.ice.service.HotConfigCleanPlanService"]}

    assert _repair_targets(evidence) == ["com.example.idss.ice.service.HotConfigCleanPlanService"]


def test_full_run_evidence_carries_test_quality(tmp_path):
    """The report's test-quality panel reads `testQuality`, which is derived
    from `targetTests`. In full mode the plugin names those only after the run,
    so scanning before it would have quietly produced nothing."""
    repo = _repo(tmp_path)
    calls = []

    result = _runner(calls).run(repo)

    assert "testQuality" in (result.evidence or {})
