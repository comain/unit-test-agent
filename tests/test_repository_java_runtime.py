import os

import pytest

from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.language.java.ci import JavaCiLanguageHandler
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.language.java.runtime import RepositoryJavaRuntimeResolver


def _jdk(tmp_path, name):
    home = tmp_path / name
    (home / "bin").mkdir(parents=True)
    java = home / "bin" / "java"
    java.write_text("#!/bin/sh\n", encoding="utf-8")
    java.chmod(0o755)
    return home


def test_resolver_uses_exact_repository_mapping_and_jdk8_fallback(tmp_path):
    jdk8 = _jdk(tmp_path, "jdk8")
    jdk25 = _jdk(tmp_path, "jdk25")
    resolver = RepositoryJavaRuntimeResolver(
        default_java_home=str(jdk8),
        repository_java_homes={"fd_wmonitor_default_store": str(jdk25)},
    )

    assert resolver.resolve("fd_wmonitor_default_store") == str(jdk25)
    assert resolver.resolve("fd_wmonitor_default_store_copy") == str(jdk8)
    assert resolver.resolve("another_repo") == str(jdk8)


def test_resolver_builds_isolated_java_environment(tmp_path):
    jdk8 = _jdk(tmp_path, "jdk8")
    jdk25 = _jdk(tmp_path, "jdk25")
    resolver = RepositoryJavaRuntimeResolver(str(jdk8), {"target": str(jdk25)})

    env = resolver.environment_for(
        "target",
        environ={"PATH": os.pathsep.join([str(jdk8 / "bin"), "/usr/bin"]), "KEEP": "yes"},
    )

    assert env["JAVA_HOME"] == str(jdk25)
    assert env["PATH"].split(os.pathsep) == [str(jdk25 / "bin"), "/usr/bin"]
    assert env["KEEP"] == "yes"


def test_resolver_rejects_invalid_explicit_java_home(tmp_path):
    jdk8 = _jdk(tmp_path, "jdk8")
    missing = tmp_path / "missing-jdk25"
    resolver = RepositoryJavaRuntimeResolver(str(jdk8), {"target": str(missing)})

    with pytest.raises(RuntimeError, match="target.*bin/java"):
        resolver.environment_for("target")


def test_maven_runner_copy_selects_java_home_without_mutating_shared_runner(tmp_path):
    original = MavenEnforcementRunner(command="mvn verify", java_home=str(tmp_path / "jdk8"))

    selected = original.with_java_home(str(tmp_path / "jdk25"))

    assert selected is not original
    assert selected.java_home == str(tmp_path / "jdk25")
    assert original.java_home == str(tmp_path / "jdk8")
    assert selected.command == original.command


def _record(app_name="fd_wmonitor_default_store"):
    return CiTaskRecord(
        task_id="ci-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            appName=app_name,
            gitUrl="git@git.example.com:fd/fd_wmonitor_default_store.git",
            branch="main",
        ),
    )


def test_java_ci_handler_selects_repository_runner_without_mutating_default(tmp_path):
    jdk8 = _jdk(tmp_path, "jdk8")
    jdk25 = _jdk(tmp_path, "jdk25")
    default_runner = MavenEnforcementRunner(command="mvn verify", java_home=str(jdk8))
    handler = JavaCiLanguageHandler(
        default_runner,
        RepositoryJavaRuntimeResolver(str(jdk8), {"fd_wmonitor_default_store": str(jdk25)}),
    )

    selected = handler.runner_for(_record())

    assert selected.java_home == str(jdk25)
    assert default_runner.java_home == str(jdk8)


def test_java_repair_task_snapshots_selected_java_home(tmp_path):
    jdk8 = _jdk(tmp_path, "jdk8")
    jdk25 = _jdk(tmp_path, "jdk25")
    captured = {}

    class FakeTaskManager:
        def create_task(self, **kwargs):
            captured.update(kwargs)
            return 7

    handler = JavaCiLanguageHandler(
        MavenEnforcementRunner(command="mvn verify", java_home=str(jdk8)),
        RepositoryJavaRuntimeResolver(str(jdk8), {"fd_wmonitor_default_store": str(jdk25)}),
    )

    task_id = handler.create_repair_task(
        task_manager=FakeTaskManager(),
        record=_record(),
        request=CreateFixSessionRequest(targetIds=["class:com.example.Target"]),
        repo_path=tmp_path,
        priority=1,
        base_ref="origin/main",
        coverage_gate=95,
        mutation_gate=100,
        rdc_context={},
        rdc_context_path=None,
    )

    assert task_id == 7
    snapshot = captured["config_snapshot"]
    assert snapshot["java_home"] == str(jdk25)
    # java_home is layered onto the OpenCode selection rather than replacing
    # it. Asserting the whole dict used to be equivalent, and it silently
    # encoded that a Java repair task carried no model selection at all.
    assert snapshot["opencode_selected_model"]


def test_the_composition_actually_wires_the_resolver(monkeypatch, tmp_path):
    """The gap the tests above cannot see.

    Every test in this file hands `JavaCiLanguageHandler` a resolver directly,
    so they pass whether or not anything in the product ever builds one. On
    this branch nothing did: `repository_java_homes` parsed, the resolver class
    existed, `runner_for` and the config snapshot were both in place -- and the
    handler was constructed as `JavaCiLanguageHandler(java_runner)`, so
    `runtime_resolver` was None and every repository silently ran on the
    daemon default.

    Production sets this variable today, pinning one repository to JDK 25, so
    the failure would have been a real repository built on the wrong JDK with
    nothing reported.
    """
    from uta.app.service_composition import build_language_handler_registry
    from uta.shared.config import Settings

    jdk25 = _jdk(tmp_path, "composed-jdk25")
    monkeypatch.setenv(
        "UTA_REPOSITORY_JAVA_HOMES", '{"fd_wmonitor_default_store": "%s"}' % jdk25
    )
    monkeypatch.setenv("UTA_DAEMON_JAVA_HOME", str(_jdk(tmp_path, "composed-jdk8")))

    registry = build_language_handler_registry(config=Settings(_env_file=None))
    java = next(h for h in registry.handlers if h.language == "java")

    assert java.runtime_resolver is not None, "the setting has no effect"
    assert java.runtime_resolver.resolve("fd_wmonitor_default_store") == str(jdk25)
    assert java.runtime_resolver.resolve("any_other_repo") != str(jdk25)
