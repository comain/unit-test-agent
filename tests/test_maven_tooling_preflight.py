"""Version checks must describe the activated reactor, not raw profile declarations."""

import subprocess
from pathlib import Path

import pytest
from tests.fake_maven_metadata import with_completion

from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.language.java.maven_project import test_enforcement_tooling_status as tooling_status


def effective_project(version="1.0.16"):
    return f"""<project><modelVersion>4.0.0</modelVersion><artifactId>biz</artifactId>
    <build><plugins><plugin><groupId>com.example.build.maven-plugins</groupId>
    <artifactId>test-enforcer</artifactId><version>{version}</version>
    </plugin></plugins></build></project>"""


@pytest.mark.parametrize("full_run", [False, True])
@pytest.mark.parametrize("version", ["1.0.15", "1.0.16"])
def test_profile_plugin_is_resolved_before_enforcement_with_existing_tests(tmp_path, monkeypatch, full_run, version):
    (tmp_path / "pom.xml").write_text("""<project><profiles><profile><id>gate</id>
    <properties><test-enforcer.version>1.0.13</test-enforcer.version></properties>
    <build><plugins><plugin><artifactId>test-enforcer</artifactId>
    <version>${test-enforcer.version}</version></plugin></plugins></build>
    </profile></profiles></project>""")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "help:effective-pom" in cmd:
            output = next(arg.removeprefix("-Doutput=") for arg in cmd if arg.startswith("-Doutput="))
            Path(output).write_text(effective_project(version))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return with_completion(cmd, subprocess.CompletedProcess(cmd, 0,
            "[test-enforcer] Diff coverage: 100%\n"
            "[test-enforcer] diff mutation score 100.00% passed for fixture "
            "(2/2 detected; 0 survived; 0 no coverage excluded)", ""))

    runner = MavenEnforcementRunner(command="mvn -Pgate -Dtest.enforcement.enabled=true verify", run_command=run, full_run=full_run)
    monkeypatch.setattr(runner, "_changed_java_files", lambda _: ["src/main/java/example/Logic.java"])
    monkeypatch.setattr(runner, "_pitest_target_tests", lambda *args: ["example.LogicTest"])
    result = runner.run(tmp_path)
    assert "help:effective-pom" in calls[0]
    assert "-Pgate" in calls[0]
    assert result.evidence["tooling"]["version"] == version
    assert result.evidence["tooling"]["requiredVersion"] == "1.0.16"
    assert result.passed == (version == "1.0.16")
    if version == "1.0.15":
        assert len(calls) == 1
        assert "1.0.15" in result.summary and "1.0.16" in result.summary


def test_effective_resolution_failure_does_not_fall_back_to_raw_pom(tmp_path):
    (tmp_path / "pom.xml").write_text(effective_project())
    result = tooling_status(tmp_path, run_maven_command=lambda *args: subprocess.CompletedProcess([], 1, "", "failed"))
    assert not result.available
    assert "effective" in result.reason.lower()


def test_one_current_module_does_not_hide_old_active_module(tmp_path):
    def run(cmd, repo):
        output = next(arg.removeprefix("-Doutput=") for arg in cmd if arg.startswith("-Doutput="))
        Path(output).write_text("<projects>" + effective_project() + effective_project("1.0.13") + "</projects>")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    status = tooling_status(tmp_path, run_maven_command=run)
    assert not status.available
    assert status.version == "1.0.13"


def test_resolver_uses_project_local_properties(tmp_path):
    roots = "<projects>" + "".join(
        effective_project("${gate.version}").replace("<build>", f"<properties><gate.version>{v}</gate.version></properties><build>")
        for v in ("1.0.15", "1.0.16")
    ) + "</projects>"
    def run(cmd, repo):
        Path(next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("-Doutput="))).write_text(roots)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    assert tooling_status(tmp_path, run_maven_command=run).version == "1.0.15"


def test_metadata_keeps_reactor_and_profile_context(tmp_path):
    calls = []
    def run(cmd, repo):
        calls.append(cmd)
        Path(next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("-Doutput="))).write_text(effective_project())
        return subprocess.CompletedProcess(cmd, 0, "", "")
    status = tooling_status(tmp_path, run_maven_command=run, profile_source_cmd=[
        "mvn", "-f", "reactor/pom.xml", "-pl", "biz", "-am", "-Pgate", "-s", "settings.xml", "verify"])
    assert status.available
    for arg in ["-f", "reactor/pom.xml", "-pl", "biz", "-am", "-Pgate", "-s", "settings.xml"]:
        assert arg in calls[0]
