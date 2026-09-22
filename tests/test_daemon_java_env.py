import os

import pytest

from uta.app.cli import _ensure_daemon_java_home
from uta.app.task_daemon import _daemon_child_environment


def test_daemon_java_home_prefers_configured_jdk8_over_inherited_java_home(tmp_path, monkeypatch):
    jdk8 = tmp_path / "jdk8"
    (jdk8 / "bin").mkdir(parents=True)
    (jdk8 / "bin" / "java").write_text("#!/bin/sh\n", encoding="utf-8")
    (jdk8 / "bin" / "java").chmod(0o755)
    inherited = tmp_path / "jdk21"
    (inherited / "bin").mkdir(parents=True)
    (inherited / "bin" / "java").write_text("#!/bin/sh\n", encoding="utf-8")
    (inherited / "bin" / "java").chmod(0o755)

    monkeypatch.setenv("JAVA_HOME", str(inherited))
    monkeypatch.setenv("PATH", os.pathsep.join([str(inherited / "bin"), "/usr/bin"]))
    monkeypatch.delenv("UTA_DAEMON_JAVA_HOME", raising=False)
    monkeypatch.setattr("uta.app.cli.settings.daemon_java_home", str(jdk8))

    assert _ensure_daemon_java_home() == str(jdk8)
    assert os.environ["JAVA_HOME"] == str(jdk8)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(jdk8 / "bin")


def test_daemon_java_home_rejects_invalid_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("UTA_DAEMON_JAVA_HOME", str(tmp_path / "missing"))
    monkeypatch.setenv("JAVA_HOME", "/some/other/jdk")

    with pytest.raises(Exception, match="UTA_DAEMON_JAVA_HOME"):
        _ensure_daemon_java_home()


def test_daemon_child_uses_snapshotted_java_home_for_java_task(tmp_path):
    """The runtime a task was created against is the runtime it should still
    run against, even if the mapping changed while it sat in the queue."""
    jdk25 = tmp_path / "jdk25"
    (jdk25 / "bin").mkdir(parents=True)
    (jdk25 / "bin" / "java").write_text("#!/bin/sh\n", encoding="utf-8")
    (jdk25 / "bin" / "java").chmod(0o755)

    env = _daemon_child_environment(
        {
            "language": "java",
            "repo_slug": "fd_wmonitor_default_store",
            "config_snapshot_json": '{"java_home":"%s"}' % jdk25,
        },
        environ={"PATH": "/usr/bin", "KEEP": "yes"},
    )

    assert env["JAVA_HOME"] == str(jdk25)
    assert env["PATH"].split(os.pathsep)[0] == str(jdk25 / "bin")
    assert env["KEEP"] == "yes"


def test_daemon_child_does_not_change_python_environment():
    """Only Java tasks pin a runtime; touching the environment of the others
    would make one language's configuration everyone's problem."""
    inherited = {"PATH": "/usr/bin", "JAVA_HOME": "/inherited"}

    assert _daemon_child_environment(
        {"language": "python", "repo_slug": "python_repo"},
        environ=inherited,
    ) == inherited


def test_a_java_home_without_an_executable_is_refused(tmp_path):
    """A mistyped path used to surface much later as a confusing Maven
    failure; failing here names the actual problem."""
    import pytest as _pytest

    empty = tmp_path / "not-a-jdk"
    empty.mkdir()

    with _pytest.raises(RuntimeError, match="bin/java"):
        _daemon_child_environment(
            {
                "language": "java",
                "repo_slug": "demo",
                "config_snapshot_json": '{"java_home":"%s"}' % empty,
            },
            environ={"PATH": "/usr/bin"},
        )
