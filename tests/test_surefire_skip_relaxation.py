"""A pinned Surefire skip flag must not survive into a measurement.

Beta task 42 is the case. `baseline_compile` relaxed
`<skipTests>true</skipTests>` to `${skipTests}` at 16:19, coverage was measured
twice (23.4%, then 38.7%), and by 18:02 the pom was back to `true` -- the root
pom was clean in git while the module pom the agent edited was still modified,
so something restored it selectively mid-run.

The third measurement then ran the identical Maven command and Surefire
answered `Tests are skipped.` The build succeeded, no jacoco.exec was written,
and the task failed at **0% coverage** on a target measured at 38.7% minutes
earlier. Every layer behaved: Maven honoured the pom, jacoco had nothing to
report, and UTA correctly refused to invent a number. The setup was simply
stale by the time it mattered.

So the relaxation moved from a one-time baseline step to something applied
immediately before each Maven test invocation.
"""

from __future__ import annotations

from pathlib import Path

from uta.language.java.maven.surefire import relax_surefire_skiptests

PINNED = """<project>
  <build><plugins><plugin>
    <artifactId>maven-surefire-plugin</artifactId>
    <version>2.5</version>
    <configuration>
      <skipTests>true</skipTests>
    </configuration>
  </plugin></plugins></build>
</project>
"""


def _repo(tmp_path: Path, content: str = PINNED) -> Path:
    (tmp_path / "pom.xml").write_text(content, encoding="utf-8")
    return tmp_path


def test_a_pinned_flag_becomes_overridable(tmp_path):
    """Plugin configuration beats `-DskipTests=false`, so the literal has to
    go; `${skipTests}` keeps the repository's default and lets the CLI win."""
    repo = _repo(tmp_path)

    assert relax_surefire_skiptests(repo) is True
    assert "${skipTests}" in (repo / "pom.xml").read_text()
    assert "<skipTests>true</skipTests>" not in (repo / "pom.xml").read_text()


def test_applying_it_twice_changes_nothing_the_second_time(tmp_path):
    """It now runs before every Maven invocation, so it has to be cheap and
    idempotent rather than rewriting the tree on each measurement."""
    repo = _repo(tmp_path)
    relax_surefire_skiptests(repo)
    before = (repo / "pom.xml").read_text()

    assert relax_surefire_skiptests(repo) is False
    assert (repo / "pom.xml").read_text() == before


def test_a_restored_pom_is_relaxed_again(tmp_path):
    """The task-42 shape: relaxed, reverted by something mid-run, and the next
    measurement must not inherit the pin."""
    repo = _repo(tmp_path)
    relax_surefire_skiptests(repo)
    (repo / "pom.xml").write_text(PINNED, encoding="utf-8")  # reverted

    assert relax_surefire_skiptests(repo) is True
    assert "${skipTests}" in (repo / "pom.xml").read_text()


def test_nested_module_poms_are_relaxed_too(tmp_path):
    module = tmp_path / "biz"
    module.mkdir()
    (module / "pom.xml").write_text(PINNED, encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")

    assert relax_surefire_skiptests(tmp_path) is True
    assert "${skipTests}" in (module / "pom.xml").read_text()


def test_a_pom_without_surefire_is_untouched(tmp_path):
    repo = _repo(tmp_path, "<project><build/></project>\n")

    assert relax_surefire_skiptests(repo) is False
    assert (repo / "pom.xml").read_text() == "<project><build/></project>\n"


def test_an_already_parameterised_flag_is_left_alone(tmp_path):
    """A repository that already made it overridable is not ours to rewrite."""
    content = PINNED.replace("<skipTests>true</skipTests>", "<skipTests>${skipTests}</skipTests>")
    repo = _repo(tmp_path, content)

    assert relax_surefire_skiptests(repo) is False


def test_the_jacoco_runner_relaxes_before_running(monkeypatch, tmp_path):
    """The wiring, not the helper -- the failure this fixes was a stale setup,
    so proving the rewrite works says nothing about when it happens."""
    from uta.language.java.maven import jacoco

    repo = _repo(tmp_path)
    seen = {}

    class _Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **kwargs):
        seen["pom_at_run_time"] = (repo / "pom.xml").read_text()
        return _Result()

    monkeypatch.setattr(jacoco.subprocess, "run", fake_run)
    jacoco.run_test_with_jacoco(str(repo), "SomeTest", module=None)

    assert "${skipTests}" in seen["pom_at_run_time"], (
        "maven ran against a pom that still pins skipTests"
    )
