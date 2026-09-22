"""Shared invocation guards must run in CI, repair and sparse local installs."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from uta.language.java.maven_compat.launcher import prepare_command, validate_completion


def test_launcher_preserves_classpath_and_selects_tests(tmp_path):
    cmd = prepare_command(["mvn", "verify", "-Dmaven.ext.class.path=/other.jar"], tmp_path)
    value = next(x.split("=", 1)[1] for x in cmd if x.startswith("-Dmaven.ext.class.path="))
    assert value.split(os.pathsep)[0] == "/other.jar"
    assert "-DtargetTests=*" in cmd
    assert "-Duta.pit.compat.intent=diff-enforcement" in cmd
    assert cmd != prepare_command(["mvn", "verify"], tmp_path)


def test_existing_selected_tests_are_preserved(tmp_path):
    cmd = prepare_command(["mvn", "verify", "-DtargetTests=example.FooTest"], tmp_path)
    assert "-DtargetTests=*" not in cmd
    assert "-DtargetTests=example.FooTest" in cmd


def test_metadata_is_not_wrapped(tmp_path):
    cmd = ["mvn", "help:effective-pom"]
    assert prepare_command(cmd, tmp_path) == cmd


def test_metadata_drops_inherited_invocation_but_keeps_other_extensions(tmp_path):
    original = prepare_command(["mvn", "verify", "-Dmaven.ext.class.path=/other.jar"], tmp_path)
    metadata = ["help:effective-pom" if arg == "verify" else arg for arg in original]
    clean = prepare_command(metadata, tmp_path)
    assert not any(arg.startswith("-Duta.pit.compat.") for arg in clean)
    assert "-Dmaven.ext.class.path=/other.jar" in clean
    assert validate_completion(clean) == {}


def test_corrupt_artifact_is_rejected(tmp_path):
    (tmp_path / "pit-runtime-compat.jar").write_bytes(b"corrupt")
    (tmp_path / "manifest.json").write_text(json.dumps({"sha256": "0" * 64}))
    from uta.language.java.maven_compat.launcher import PitCompatibilityError
    with pytest.raises(PitCompatibilityError, match="digest"):
        prepare_command(["mvn", "verify"], tmp_path, artifact_dir=tmp_path)


def test_missing_and_stale_completion_fail_closed(tmp_path):
    cmd = prepare_command(["mvn", "verify"], tmp_path)
    with pytest.raises(OSError, match="completion"):
        validate_completion(cmd)
    evidence = Path(next(x.split("=", 1)[1] for x in cmd if x.startswith("-Duta.pit.compat.evidence=")))
    ET.ElementTree(ET.Element("pitCompatibility", invocation="stale")).write(evidence)
    with pytest.raises(OSError, match="invocation"):
        validate_completion(cmd)


def test_jvm_extension_configuration_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVEN_OPTS", "-Dmaven.ext.class.path=/from-jvm.jar")
    with pytest.raises(OSError, match="extension"):
        prepare_command(["mvn", "verify"], tmp_path)


def test_nonzero_compatibility_failure_is_terminal():
    from uta.language.java.maven_compat.launcher import reject_compatibility_failure, PitCompatibilityError
    with pytest.raises(PitCompatibilityError):
        reject_compatibility_failure("Diff coverage: 100%\nTest strength 100%\n[ERROR] [uta-pit-compat] Missing fresh mutation evidence for biz")
    reject_compatibility_failure("[uta-pit-compat] verified module=biz skip=false targets=2")


@pytest.mark.parametrize("returncode", [0, 1])
def test_green_metrics_cannot_mask_missing_completion_in_runner(tmp_path, returncode):
    import subprocess
    from uta.language.java.enforcement_runner import MavenEnforcementRunner
    def run(cmd, **kwargs):
        if "help:effective-pom" in cmd:
            output = Path(next(x.split("=", 1)[1] for x in cmd if x.startswith("-Doutput=")))
            output.write_text("<project><build><plugins><plugin><artifactId>test-enforcer</artifactId><version>1.0.16</version></plugin></plugins></build></project>")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, returncode, "Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%", "")
    result = MavenEnforcementRunner("mvn verify -Dtest.enforcement.enabled=true", run_command=run).run(tmp_path)
    assert not result.passed


def test_tooling_failure_is_not_application_repairable():
    from types import SimpleNamespace
    from uta.shared.fix_sessions import can_create_fix_session
    record = SimpleNamespace(status=SimpleNamespace(value="failed"), enforcement_result={"evidence": {"repairEligible": False}})
    assert not can_create_fix_session(record)


def test_stop_group_kills_descendants_even_after_parent_exits(tmp_path):
    import signal
    import subprocess
    import sys
    import time
    from uta.language.java.maven_compat.launcher import _stop_group
    marker = tmp_path / "child"
    child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    parent = f"import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c',{child!r}]);pathlib.Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        _stop_group(process)
        pid = int(marker.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(.02)
        assert not state or state.startswith("Z")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _compat_failure(output, returncode=0):
    """The error `_run_command` raises once completion evidence comes up short."""
    import subprocess as sp
    from uta.language.java.maven_compat.launcher import PitCompatibilityError

    error = PitCompatibilityError(
        "PIT compatibility did not verify every obligated module: demo:biz:jar:1"
    )
    error.modules = [
        {"id": "demo:common:jar:1", "state": "skipped"},
        {"id": "demo:biz:jar:1", "state": "missing"},
    ]
    error.completed = sp.CompletedProcess(["mvn"], returncode, stdout=output, stderr="")
    return error


def _runner(**kwargs):
    from uta.language.java.enforcement_runner import MavenEnforcementRunner

    return MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify", **kwargs
    )


def test_incomplete_completion_reports_the_gate_that_actually_missed(tmp_path):
    """The real gate miss is the failure reason; PIT bookkeeping is the note."""
    error = _compat_failure(
        "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
        "pitest.targets=1 [com.demo.InvoiceBiz*]\n"
        "[ERROR] test-enforcer check-coverage failed: diff line coverage 91.57% "
        "is below required 95.00% (76/83)\n",
        returncode=1,
    )

    result = _runner()._pit_compatibility_result(error, ["mvn"], tmp_path, {})

    assert result.passed is False
    assert "Coverage gate failed: 91.57% < 95.00% (76/83)" in result.summary
    assert "PIT completion evidence incomplete" in result.summary
    assert result.evidence["failureKind"] == "pit_compatibility_failed"
    # A real gate miss has repair targets; only the bookkeeping case has none.
    assert result.evidence.get("repairEligible") is not False


def test_incomplete_completion_says_so_when_every_gate_passed(tmp_path):
    """A green run must not be reported as a gate miss it never had."""
    error = _compat_failure(
        "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
        "pitest.targets=1 [com.demo.InvoiceBiz*]\n"
        "[info] [test-enforcer] diff line coverage 96.77% passed for demo-biz (30/31)\n"
        "PIT generated=24 killed=24 survived=0 test-strength=100.00%\n"
        "[info] [test-enforcer] diff mutation score 100.00% passed for demo-biz "
        "(24/24 detected; 0 survived; 0 no coverage excluded)\n"
        "[INFO] BUILD SUCCESS\n"
    )

    result = _runner(mutation_gate=100)._pit_compatibility_result(error, ["mvn"], tmp_path, {})

    assert result.passed is False
    assert "gates passed" in result.summary
    assert "demo:biz:jar:1" in result.summary
    assert result.evidence["repairEligible"] is False
    assert [m["id"] for m in result.evidence["pitCompatibilityModules"]] == [
        "demo:common:jar:1",
        "demo:biz:jar:1",
    ]


def test_wide_reactor_gate_misses_stay_within_the_summary_bound():
    """One sentence per module must not write an unbounded task summary."""
    from uta.language.java.enforcement_runner.classification import _gate_failure_summary
    from uta.language.java.enforcement_runner.parsing import GATE_MISS_SUMMARY_MAX_CHARS

    output = "\n".join(
        "[ERROR] test-enforcer check-coverage failed: diff line coverage "
        "%d.00%% is below required 95.00%% (%d/100)" % (rate, rate)
        for rate in range(40, 80)
    )

    summary = _gate_failure_summary({}, output, 100.0)

    assert summary.startswith("UTA test-enforcement failed: Coverage gate failed: 40.00% < 95.00%")
    assert summary.endswith("more")
    assert len(summary) <= GATE_MISS_SUMMARY_MAX_CHARS + len("UTA test-enforcement failed: ")


def _evidence_cmd(path):
    return ["mvn", "verify",
            "-Duta.pit.compat.invocation=abc123",
            "-Duta.pit.compat.evidence=" + str(path)]


def test_deleted_evidence_directory_is_named_as_such(tmp_path):
    """A wiped workspace and a Maven run that wrote nothing are different bugs."""
    from uta.language.java.maven_compat.launcher import validate_completion, PitCompatibilityError

    with pytest.raises(PitCompatibilityError) as caught:
        validate_completion(_evidence_cmd(tmp_path / "gone" / "completion.xml"))

    assert "directory-missing" in str(caught.value)
    assert caught.value.diagnostics["evidenceState"] == "directory-missing"


def test_present_directory_without_evidence_is_named_as_such(tmp_path):
    from uta.language.java.maven_compat.launcher import validate_completion, PitCompatibilityError

    (tmp_path / "pit-7").mkdir()
    with pytest.raises(PitCompatibilityError) as caught:
        validate_completion(_evidence_cmd(tmp_path / "completion.xml"))

    assert "file-missing" in str(caught.value)
    assert caught.value.diagnostics["directoryEntries"] == ["pit-7"]


def test_incomplete_evidence_carries_the_module_states_it_read(tmp_path):
    from uta.language.java.maven_compat.launcher import validate_completion, PitCompatibilityError

    path = tmp_path / "completion.xml"
    path.write_text(
        '<pitCompatibility complete="false" invocation="abc123">'
        '<module id="demo:biz:jar:1" state="missing"/></pitCompatibility>'
    )
    with pytest.raises(PitCompatibilityError) as caught:
        validate_completion(_evidence_cmd(path))

    assert caught.value.diagnostics["evidenceState"] == "parsed"
    assert caught.value.diagnostics["modules"] == [{"id": "demo:biz:jar:1", "state": "missing"}]


def test_compat_failure_on_a_red_build_still_reports_the_gate(tmp_path):
    """A [uta-pit-compat] line is not the verdict when a gate already failed."""
    import subprocess as sp
    from uta.language.java.enforcement_runner import MavenEnforcementRunner
    from uta.language.java.maven_compat.launcher import PitCompatibilityError

    output = (
        "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
        "pitest.targets=1 [com.demo.InvoiceBiz*]\n"
        "[ERROR] test-enforcer check-coverage failed: diff line coverage 88.00% "
        "is below required 95.00% (44/50)\n"
        "[ERROR] Failed to execute goal org.pitest:pitest-maven:1.15.0:mutationCoverage "
        "(pitest) on project demo-biz: [uta-pit-compat] PIT produced no mutation report for demo-biz\n"
    )
    error = PitCompatibilityError("[uta-pit-compat] PIT produced no mutation report for demo-biz")
    error.completed = sp.CompletedProcess(["mvn"], 1, stdout=output, stderr="")

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify")
    result = runner._pit_compatibility_result(error, ["mvn"], tmp_path, {})

    assert result.passed is False
    assert "Coverage gate failed: 88.00% < 95.00% (44/50)" in result.summary
    assert "PIT produced no mutation report" in result.summary


def test_injected_runner_tuple_args_do_not_validate_a_stale_nonce(tmp_path, monkeypatch):
    """The verdict must read this run's evidence, not the configured command's.

    uta_enforce_core's CommandResult normalises args to a tuple, so a list-only
    check left `cmd` as the configured command. For a repair task that command is
    the CI record's stored one, still carrying a past invocation's nonce, and
    validate_completion then judged this green run against another run's
    evidence path.
    """
    from uta.language.java import enforcement_runner as module
    from uta.language.java.enforcement_runner import MavenEnforcementRunner

    output = (
        "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
        "pitest.targets=1 [com.demo.InvoiceBiz*]\n"
        "[info] [test-enforcer] diff line coverage 100.00% passed for demo-biz (5/5)\n"
        "PIT generated=30 killed=30 survived=0 test-strength=100.00%\n"
        "[info] [test-enforcer] diff mutation score 100.00% passed for demo-biz "
        "(30/30 detected; 0 survived; 0 no coverage excluded)\n"
        "[INFO] BUILD SUCCESS\n"
    )

    class _TupleArgs:
        """What an injected uta_enforce_core runner hands back."""

        def __init__(self, args):
            self.args = tuple(args)
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def fake_run(cmd, **_):
        # Maven's side of the contract: write this invocation's completion file.
        evidence = next(
            (a.split("=", 1)[1] for a in cmd if a.startswith("-Duta.pit.compat.evidence=")), None
        )
        if evidence:
            invocation = next(
                a.split("=", 1)[1] for a in cmd if a.startswith("-Duta.pit.compat.invocation=")
            )
            Path(evidence).write_text(
                '<pitCompatibility complete="true" invocation="%s">'
                '<module id="demo:biz:jar:1" state="completed"/></pitCompatibility>' % invocation
            )
        return _TupleArgs(cmd)

    monkeypatch.setattr(
        module, "test_enforcement_tooling_status",
        lambda *a, **k: SimpleNamespace(available=True, version="1.0.16", reason=""),
    )
    monkeypatch.setattr(MavenEnforcementRunner, "_changed_java_files", lambda self, repo: None)

    stale_nonce = "staleaaaabbbbccccddddeeeeffff0000"
    stale = tmp_path / (".uta_cache/pit-compat/%s/completion.xml" % stale_nonce)
    configured = (
        "mvn -Dtest.enforcement.enabled=true verify "
        "-Duta.pit.compat.intent=diff-enforcement "
        "-Duta.pit.compat.invocation=%s "
        "-Duta.pit.compat.evidence=%s" % (stale_nonce, stale)
    )

    runner = MavenEnforcementRunner(command=configured, run_command=fake_run)
    result = runner.run(tmp_path)

    assert not stale.exists(), "the stale nonce directory was never this run's"
    assert result.passed is True, result.summary
    assert "cannot be trusted" not in (result.summary or "")


def test_compat_result_reads_the_executed_argv_from_a_tuple(tmp_path):
    """_pit_compatibility_result carried the same list-only check as _run did."""
    import subprocess as sp
    from uta.language.java.enforcement_runner import MavenEnforcementRunner
    from uta.language.java.maven_compat.launcher import PitCompatibilityError, prepare_command

    prepared = prepare_command(["mvn", "-Dtest.enforcement.enabled=true", "verify"], tmp_path)
    evidence = Path(next(
        x.split("=", 1)[1] for x in prepared if x.startswith("-Duta.pit.compat.evidence=")
    ))
    invocation = next(
        x.split("=", 1)[1] for x in prepared if x.startswith("-Duta.pit.compat.invocation=")
    )
    evidence.write_text(
        '<pitCompatibility complete="true" invocation="%s">'
        '<module id="demo:biz:jar:1" state="completed"/></pitCompatibility>' % invocation
    )

    class _TupleArgs(sp.CompletedProcess):
        pass

    error = PitCompatibilityError("PIT compatibility did not verify every obligated module")
    error.completed = _TupleArgs(tuple(prepared), 1, "[ERROR] test-enforcer check-coverage failed: "
                                 "diff line coverage 80.00% is below required 95.00% (4/5)\n", "")

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify")
    result = runner._pit_compatibility_result(error, ["mvn", "verify"], tmp_path, {})

    # The executed argv, not the caller's, is what the verdict is recorded against.
    assert result.command == list(prepared)
    assert "Coverage gate failed: 80.00% < 95.00% (4/5)" in result.summary


def test_repair_does_not_inherit_the_ci_run_pit_credentials():
    """A one-time invocation nonce must not be handed to the next run."""
    from uta.language.java.ci import JavaCiLanguageHandler
    from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest

    record = CiTaskRecord(
        task_id="t1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo", git_url="git@example.invalid:g/demo.git", branch="b"
        ),
        enforcement_result={
            "command": [
                "mvn", "-Dtest.enforcement.enabled=true", "verify", "-pl", "biz", "-am",
                "-Dmaven.ext.class.path=/opt/uta/pit-runtime-compat.jar",
                "-Duta.pit.compat.intent=diff-enforcement",
                "-Duta.pit.compat.invocation=deadbeefdeadbeefdeadbeefdeadbeef",
                "-Duta.pit.compat.evidence=/ws/.uta_cache/pit-compat/deadbeef/completion.xml",
                "-DtargetTests=com.demo.AlreadyPinnedTest",
            ]
        },
    )

    command = JavaCiLanguageHandler(runner=None)._quality_gate_command(record)

    assert "uta.pit.compat" not in command
    assert "deadbeef" not in command
    assert "-DtargetTests=" not in command
    # Everything that is genuinely configuration survives.
    assert "-pl biz" in command and "-Dtest.enforcement.enabled=true" in command


def test_launcher_requests_skip_failing_tests(tmp_path):
    """PIT's descriptor leaves skipFailingTests unreachable; the extension binds it."""
    cmd = prepare_command(["mvn", "verify"], tmp_path)
    assert "-DskipFailingTests=true" in cmd


def test_explicit_skip_failing_tests_is_preserved(tmp_path):
    cmd = prepare_command(["mvn", "verify", "-DskipFailingTests=false"], tmp_path)
    assert "-DskipFailingTests=true" not in cmd
    assert "-DskipFailingTests=false" in cmd
