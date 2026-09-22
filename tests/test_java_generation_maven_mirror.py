import subprocess
import shlex

from uta.language.java.generation import commands
from uta.language.java.phases.test_repair import JavaTestRepairPorts, render_fix_tests_prompt
from uta.language.java.phases.generation_turn import render_generate_tests_prompt
from uta.enforcement.evidence import evidence_detail


def test_targeted_java_verification_uses_configured_central_mirror(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands.uta_settings, "maven_bin", "mvn-test")
    monkeypatch.setattr(
        commands.uta_settings,
        "maven_central_mirror_url",
        "https://nexus.example.test/repository/public/",
    )

    assert commands._run_test_selector(str(tmp_path), "InspectionOrderTaskTest", module="provider")[0]
    assert commands._compile_test(str(tmp_path), module="provider")[0]

    settings_path = tmp_path / ".uta_cache/maven/central-mirror-global-settings.xml"
    assert settings_path.exists()
    assert "<mirrorOf>central</mirrorOf>" in settings_path.read_text(encoding="utf-8")
    for command in calls:
        assert command[command.index("-gs") + 1] == str(settings_path)
        module_index = command.index("-pl")
        assert command[module_index : module_index + 3] == ["-pl", "provider", "-am"]


def test_targeted_java_verification_does_not_ignore_dependency_resolution_errors(
    monkeypatch, tmp_path
):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            b"[ERROR] Could not resolve required dependency com.example:missing:jar:1.0",
            b"",
        )

    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands.uta_settings, "maven_central_mirror_url", "")

    passed, output = commands._run_test_selector(str(tmp_path), "ExampleTest")
    assert not passed
    assert "Could not resolve required dependency" in output


def test_targeted_java_verification_uses_gate_profile_and_preserves_missing_pom_warning(
    monkeypatch, tmp_path
):
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            b"[WARNING] The POM for com.example:legacy:jar:1 is missing, no dependency information available\n",
            b"",
        )

    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(commands.uta_settings, "maven_central_mirror_url", "https://nexus.example.test/public/")
    gate_command = "mvn -U -Plocal verify"

    passed, output = commands._run_test_selector(
        str(tmp_path), "InspectionOrderTaskTest", module="provider", quality_gate_command=gate_command
    )
    assert passed
    assert "POM for com.example:legacy" in output
    assert commands._compile_test(
        str(tmp_path), module="provider", quality_gate_command=gate_command
    )[0]
    for command in calls:
        assert "-U" in command
        assert "-Plocal" in command
        assert "-gs" in command
        assert "-o" not in command


def test_java_repair_prompt_uses_the_deterministic_maven_command(monkeypatch, tmp_path):
    monkeypatch.setattr(commands.uta_settings, "maven_central_mirror_url", "https://nexus.example.test/public/")
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(commands.subprocess, "run", run)
    state = {
        "repo_path": str(tmp_path),
        "module": "provider",
        "batch": ["com.example.InspectionOrderTask"],
        "quality_gate_command": "mvn -U -Plocal verify",
    }
    prompt = render_fix_tests_prompt(
        state,
        ports=JavaTestRepairPorts(index_query_command=lambda *_args, **_kwargs: "uta-query"),
    )

    assert "-U" in prompt
    assert "-Plocal" in prompt
    assert "-gs" in prompt
    assert "-DskipTests=false" in prompt
    command = prompt.split("### TARGETED COMMAND", 1)[1].split("`", 2)[1]
    assert "-o" not in shlex.split(command)
    assert commands._run_test_selector(
        str(tmp_path), "InspectionOrderTaskTest", module="provider",
        quality_gate_command=state["quality_gate_command"],
    )[0]
    assert shlex.split(command) == calls[0]


def test_missing_pom_is_an_advisory_in_java_report_evidence():
    warning = (
        "[WARNING] The POM for com.example:legacy:jar:1 is missing, "
        "no dependency information available"
    )
    detail = evidence_detail(
        {
            "language": "java",
            "status": "failed",
            "stdout": warning + "\n[ERROR] diff line coverage 80.00% is below required 95.00% (8/10)",
            "evidence": {"coverage": {"covered": 8, "total": 10, "rate": 80.0}},
        }
    )

    assert detail["dependencyWarnings"] == [warning]
    assert detail["coverage"]["rate"] == 80.0


def test_java_generation_prompt_uses_the_targeted_verifier_command(monkeypatch, tmp_path):
    monkeypatch.setattr(commands.uta_settings, "maven_central_mirror_url", "https://nexus.example.test/public/")
    captured = {}

    def render(_name, **kwargs):
        captured.update(kwargs)
        return "", kwargs["targeted_test_command"]

    monkeypatch.setattr("uta.language.java.phases.generation_turn.render_prompt_split", render)

    class Ports:
        @staticmethod
        def mockito_api_guidance(_repo):
            return ""

        @staticmethod
        def compress_plan(_plan):
            return ""

    prompt = render_generate_tests_prompt(
        {
            "repo_path": str(tmp_path),
            "module": "provider",
            "batch": ["com.example.InspectionOrderTask"],
            "quality_gate_command": "mvn -U -Plocal verify",
        },
        ports=Ports(),
    )

    assert captured["targeted_test_command"] in prompt
    assert "-U" in shlex.split(captured["targeted_test_command"])
    assert "-Plocal" in shlex.split(captured["targeted_test_command"])
    assert "-gs" in shlex.split(captured["targeted_test_command"])
    assert "-o" not in shlex.split(captured["targeted_test_command"])
