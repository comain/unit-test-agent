import subprocess

from uta.language.java import baseline


def _isolate_baseline_setup(monkeypatch):
    monkeypatch.setattr(baseline, "_upgrade_mockito", lambda _repo: False)
    monkeypatch.setattr(baseline, "relax_surefire_skiptests", lambda _repo: False)
    monkeypatch.setattr(baseline, "_ensure_javafx_pair", lambda _repo: False)
    monkeypatch.setattr(baseline, "maybe_run_opencode_init_slash", lambda *_args: None)
    monkeypatch.setattr(baseline, "maybe_run_project_init_command", lambda *_args: None)


def test_baseline_compile_runs_the_root_maven_compile(monkeypatch, tmp_path):
    _isolate_baseline_setup(monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(baseline.subprocess, "run", run)
    monkeypatch.setattr(baseline.uta_settings, "maven_bin", "mvn-test")

    result = baseline.baseline_compile(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "quality_mode": "class_batch",
            "deterministic_change_paths": [],
        }
    )

    assert calls[0][0][:3] == ["mvn-test", "compile", "-DskipTests"]
    assert calls[0][1]["timeout"] == 600
    assert result["error"] is None
    assert result["current_stage"] == "baseline_compile"


def test_baseline_compile_retries_only_the_target_module(monkeypatch, tmp_path):
    _isolate_baseline_setup(monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command, output=b"", stderr=b"reactor failed")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(baseline.subprocess, "run", run)
    monkeypatch.setattr(baseline.uta_settings, "maven_bin", "mvn-test")

    result = baseline.baseline_compile(
        {
            "repo_path": str(tmp_path),
            "module": "service",
            "quality_mode": "class_batch",
            "deterministic_change_paths": [],
        }
    )

    assert calls[0][-3:] == ["-pl", "service", "-am"]
    assert calls[1][-2:] == ["-pl", "service"]
    assert result["error"] is None


def test_a_pom_without_mockito_gets_a_buildable_byte_buddy_version(tmp_path):
    """Found by replaying a production task on beta, where it broke the build.

    `_upgrade_mockito` adds mockito-core when a pom has none, and then injects
    ByteBuddy dependencyManagement entries pinned to `${byte-buddy.version}`.
    But both branches that *define* that property test the pom text as it was
    *before* mockito-core was added, so for a pom with no mockito at all they
    are both false. The property is never defined, the dependencies referencing
    it are added anyway, and Maven fails the build before a single test is
    generated:

        'dependencies.dependency.version' for net.bytebuddy:byte-buddy:jar
        must be a valid version but is '${byte-buddy.version}'

    The task fails at baseline compile with 0 classes done, which looks like a
    repository problem rather than something UTA did to the pom.
    """
    from uta.language.java.baseline import _upgrade_mockito

    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project>\n"
        "    <properties>\n"
        "        <java.version>8</java.version>\n"
        "    </properties>\n"
        "    <dependencyManagement>\n"
        "        <dependencies>\n"
        "        </dependencies>\n"
        "    </dependencyManagement>\n"
        "    <dependencies>\n"
        "    </dependencies>\n"
        "</project>\n",
        encoding="utf-8",
    )

    _upgrade_mockito(str(tmp_path))
    result = pom.read_text(encoding="utf-8")

    if "${byte-buddy.version}" in result:
        assert "<byte-buddy.version>" in result, (
            "byte-buddy is pinned to a property this pom never defines; "
            "Maven cannot build it"
        )


def test_an_existing_byte_buddy_property_is_not_duplicated(tmp_path):
    """The fix must not add a second definition when one already exists."""
    from uta.language.java.baseline import _upgrade_mockito

    pom = tmp_path / "pom.xml"
    pom.write_text(
        "<project>\n"
        "    <properties>\n"
        "        <byte-buddy.version>1.9.10</byte-buddy.version>\n"
        "    </properties>\n"
        "    <dependencies>\n"
        "        <dependency>\n"
        "            <groupId>org.mockito</groupId>\n"
        "            <artifactId>mockito-core</artifactId>\n"
        "            <version>2.28.2</version>\n"
        "        </dependency>\n"
        "    </dependencies>\n"
        "</project>\n",
        encoding="utf-8",
    )

    _upgrade_mockito(str(tmp_path))

    assert pom.read_text(encoding="utf-8").count("<byte-buddy.version>") == 1


def test_baseline_compile_routes_central_through_the_configured_mirror(monkeypatch, tmp_path):
    """The baseline compile builds its own Maven command, so it never picked up
    `maven_central_mirror_url` -- only Java *enforcement* did.

    On a host whose settings.xml has no `<mirror>`, that sends the compile
    straight to repo.maven.apache.org, where an internal artifact cannot exist,
    and the JDK truststore then fails the handshake -- surfacing as
    `PKIX path building failed` instead of a missing dependency. Observed on
    production task a6d0aadb resolving com.example.inf:wmq-api:2.1.5.
    """
    _isolate_baseline_setup(monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(baseline.subprocess, "run", run)
    monkeypatch.setattr(baseline.uta_settings, "maven_bin", "mvn-test")
    monkeypatch.setattr(
        baseline.uta_settings, "maven_central_mirror_url",
        "https://nexus.example.test/nexus/content/groups/public/",
    )

    baseline.baseline_compile(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "quality_mode": "class_batch",
            "deterministic_change_paths": [],
        }
    )

    assert calls, "expected a maven invocation"
    cmd = calls[0]
    assert "-gs" in cmd, f"baseline compile did not receive a global settings override: {cmd}"
    settings_path = cmd[cmd.index("-gs") + 1]
    written = (tmp_path / ".uta_cache" / "maven" / "central-mirror-global-settings.xml")
    assert str(written) == settings_path
    body = written.read_text(encoding="utf-8")
    assert "<mirrorOf>central</mirrorOf>" in body
    assert "https://nexus.example.test/nexus/content/groups/public/" in body


def test_baseline_compile_is_unchanged_when_no_mirror_is_configured(monkeypatch, tmp_path):
    """An unset mirror must leave the command exactly as it was."""
    _isolate_baseline_setup(monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(baseline.subprocess, "run", run)
    monkeypatch.setattr(baseline.uta_settings, "maven_bin", "mvn-test")
    monkeypatch.setattr(baseline.uta_settings, "maven_central_mirror_url", "")

    baseline.baseline_compile(
        {
            "repo_path": str(tmp_path),
            "module": None,
            "quality_mode": "class_batch",
            "deterministic_change_paths": [],
        }
    )

    assert calls
    assert "-gs" not in calls[0]
    assert not (tmp_path / ".uta_cache" / "maven").exists()
