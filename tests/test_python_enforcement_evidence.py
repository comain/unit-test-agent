"""The Python enforcement lane must emit UTA's evidence contract.

Enforcement runs through a binding, and the binding does not get to invent a
payload shape: the report UI, the task database, the RDC gate and
`validate_python_enforcement_evidence` all read this envelope. This file
exercises the lane end to end against one real repository -- pytest and mutmut
actually run -- rather than comparing two mocks.

It began as the parity fixture ADR-014 required while a second, legacy lane
still existed, and its cross-lane comparisons went with that lane. What
remains is what those comparisons were protecting: the envelope keys UTA
consumers read, the four conditions enforcement must refuse before running
anything, and the CI cap profile.

Deliberately not asserted: the evidence id value (a content hash of everything
else), the wall-clock stamp, and elapsed times.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from uta.language.python.enforcement import (
    run_python_enforcement,
    validate_python_enforcement_evidence,
)


requires_mutmut = pytest.mark.skipif(
    subprocess.run(["which", "mutmut"], capture_output=True).returncode != 0,
    reason="mutmut is not installed",
)


@pytest.fixture(autouse=True)
def active_python_runtime(monkeypatch):
    """Run real enforcement with the interpreter that owns test dependencies."""
    monkeypatch.setenv("UTA_PYTHON_BIN", sys.executable)

#: Values that legitimately differ per run; see the module docstring.
_VOLATILE = {"evidenceId", "generatedAt"}


#: Frozen so two separately-built fixture repos are byte-identical and hash to
#: the same commits. That lets the two lanes each get a clean tree *and* still
#: be compared on `baseCommit`/`headCommit`, instead of trading one away.
_FIXED_DATE = "2026-01-01T00:00:00+00:00"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": _FIXED_DATE,
            "GIT_COMMITTER_DATE": _FIXED_DATE,
            "GIT_AUTHOR_NAME": "Parity",
            "GIT_AUTHOR_EMAIL": "parity@example.test",
            "GIT_COMMITTER_NAME": "Parity",
            "GIT_COMMITTER_EMAIL": "parity@example.test",
        },
    )


@pytest.fixture
def make_repo(tmp_path: Path):
    """A *factory*, deliberately, not one shared repository.

    Running both lanes in one checkout is not a fair comparison: the first lane
    leaves `.uta_cache` artifacts and a populated coverage/mutation workspace
    that the second one then reads. That made `candidatePlan.omittedByOnePerLine`
    differ between the lanes when the difference was really between a clean
    tree and a used one -- the sort of result that sends you looking for a bug
    in the wrong implementation.
    """
    counter = {"n": 0}

    def _make() -> Path:
        counter["n"] += 1
        return _build_repo(tmp_path / f"repo{counter['n']}")

    return _make


def _build_repo(repo: Path) -> Path:
    """One changed, covered, mutable line -- the smallest honest end-to-end run."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Parity")
    _git(repo, "config", "user.email", "parity@example.test")
    (repo / "jobs").mkdir()
    (repo / "tests").mkdir()
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n    if value > 0:\n        return value * 2\n    return 0\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n"
        "    assert run(2) == 4\n    assert run(-1) == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n    if value > 0:\n        return value * 3\n    return 0\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n"
        "    assert run(2) == 6\n    assert run(-1) == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _build_stateful_mock_repo(repo: Path) -> Path:
    """A test module that fails if two pytest phases share imported state."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Parity")
    _git(repo, "config", "user.email", "parity@example.test")
    (repo / "jobs").mkdir()
    (repo / "tests").mkdir()
    source = repo / "jobs" / "forecast.py"
    source.write_text(
        "def callback():\n    pass\n\n\ndef run(value):\n"
        "    if value > 0:\n        callback()\n        return value * 2\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from unittest.mock import Mock\n"
        "import jobs.forecast as target\n\n"
        "target.callback = Mock()\n\n"
        "def test_positive():\n"
        "    assert target.run(2) == 6\n"
        "    target.callback.assert_called_once_with()\n\n"
        "def test_zero_does_not_call_callback_again():\n"
        "    assert target.run(0) == 0\n"
        "    target.callback.assert_called_once_with()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    source.write_text(source.read_text(encoding="utf-8").replace("value * 2", "value * 3"), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _enforce(repo: Path) -> dict:
    return run_python_enforcement(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=80.0,
        mutation_gate=70.0,
    )


def _enforce_raw(repo: Path, **overrides):
    kwargs = dict(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=80.0,
        mutation_gate=70.0,
    )
    kwargs.update(overrides)
    return run_python_enforcement(**kwargs)


@requires_mutmut
def test_mutmut_phases_isolate_stateful_target_mocks(tmp_path):
    repo = _build_stateful_mock_repo(tmp_path / "stateful")

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=80.0,
        mutation_gate=0.0,
    )

    target = evidence["targetResults"][0]
    assert target["reasonCode"] != "mutation_backend_failed", target["message"]
    assert target["mutation"]["notChecked"] == 0


@requires_mutmut
def test_the_envelope_carries_every_key_uta_consumers_read(make_repo):
    """A missing key is the quiet failure: a consumer reading `passed` gets
    `None` and never learns the run happened."""
    evidence = _enforce(make_repo())

    for key in (
        "schemaVersion", "backend", "language", "baseRef", "baseCommit",
        "headCommit", "status", "passed", "reasonCode", "summary",
        "changedProductionFiles", "changedLines", "targets", "targetResults",
        "coverage", "mutation", "commands", "artifacts", "setup",
        "evidenceId", "generatedAt", "utaVersion", "enforcementCoreVersion",
    ):
        assert key in evidence, key


@requires_mutmut
def test_command_evidence_keeps_the_field_names_consumers_read(make_repo):
    """`exit_code`, not `exitCode`. The binding spells it the other way
    internally and the projection translates; a consumer reading the envelope
    sees one spelling."""
    evidence = _enforce(make_repo())

    assert evidence["commands"], "a real run issues commands"
    for command in evidence["commands"]:
        assert "exit_code" in command, sorted(command)
        assert "exitCode" not in command, sorted(command)
        assert "name" in command


@requires_mutmut
def test_the_evidence_id_uses_the_uta_format(make_repo):
    """Two id algorithms existed; the surviving lane uses UTA's."""
    evidence = _enforce(make_repo())

    evidence_id = evidence.get("evidenceId") or ""
    prefix, _, digest = evidence_id.partition(":")
    assert prefix == "uta-python-enforcement", evidence_id
    assert len(digest) == 16, evidence_id


@requires_mutmut
def test_the_evidence_passes_utas_own_validator(make_repo):
    """The validator is what the RDC gate runs."""
    evidence = _enforce(make_repo())

    verdict = validate_python_enforcement_evidence(evidence)
    assert verdict.passed, f"{verdict.reason_code}: {verdict.message}"


@requires_mutmut
def test_every_target_payload_carries_what_reporting_reads(make_repo):
    """`testQuality` feeds four reporting surfaces and `candidateResults` is
    how `ci_evidence` explains a failed target. Both were missing once."""
    evidence = _enforce(make_repo())

    target = (evidence.get("targetResults") or [{}])[0]
    for key in (
        "target", "status", "reasonCode", "testsPass", "message", "coverage",
        "mutation", "commands", "artifacts", "setup", "testQuality",
        "candidateResults", "configuredTestPaths", "selectedTestPaths",
        "candidateTestPaths",
    ):
        assert key in target, key


@requires_mutmut
def test_the_mutation_artifacts_describe_how_mutants_were_generated(make_repo):
    """Strategy and scope are facts only the generating layer knows, and an
    auditor cannot tell a capped run from a batched one without them."""
    evidence = _enforce(make_repo())

    artifacts = evidence.get("artifacts") or {}
    assert sorted(artifacts) == [
        "import_compat",
        "mutation_batch_count",
        "mutation_generation_strategy",
        "mutation_scope",
        "mutmut_generation_policy",
    ], sorted(artifacts)
    for path_str in artifacts["mutmut_generation_policy"]:
        assert Path(path_str).exists()
    for path_str in artifacts["import_compat"]:
        assert (Path(path_str) / "sitecustomize.py").exists()


# -- fail-closed pre-flight ---------------------------------------------------
#
# Four conditions are refused before anything runs. Certifying a change because
# the baseline could not be found is the worst thing this system can do, and it
# is silent.


@requires_mutmut
def test_a_base_ref_that_does_not_resolve_is_refused(make_repo):
    """An unfetched or misspelled base ref makes `git diff` return nothing, so
    every file looks unchanged and the gate would pass on an empty diff."""
    evidence = _enforce_raw(make_repo(), base_ref="origin/does-not-exist")

    assert evidence["passed"] is False, "certified a change it could not diff"
    assert evidence["reasonCode"] == "missing_base_ref"


@requires_mutmut
def test_a_run_with_no_test_paths_is_refused(make_repo):
    evidence = _enforce_raw(make_repo(), test_paths=[])

    assert evidence["passed"] is False, "passed a run configured with no tests"
    assert evidence["reasonCode"] == "missing_test_paths"


@requires_mutmut
def test_a_clean_tree_passes_its_own_validator(make_repo):
    """This lane once emitted reasonCode='no_targets', which its own validator
    then rejected -- evidence that says passed and a validator that says
    failed, on one payload."""
    repo = make_repo()
    (repo / "README.md").write_text("docs only\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")

    evidence = _enforce_raw(repo, target_values=[], base_ref="HEAD~1")

    assert evidence["passed"] is True
    assert evidence["reasonCode"] == "no_changed_python_targets"
    verdict = validate_python_enforcement_evidence(evidence)
    assert verdict.passed, f"{verdict.reason_code}: {verdict.message}"


@requires_mutmut
def test_a_failing_target_keeps_its_diagnosis(make_repo):
    """`quality_gate_failed` for a test that did not even run tells nobody
    anything. The per-target reason code exists; it must reach the envelope."""
    repo = make_repo()
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n    assert run(2) == 999\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "break the test")

    evidence = _enforce(repo)

    assert evidence["passed"] is False
    assert evidence["reasonCode"] not in ("", "quality_gate_failed", "failed")


# -- CI cap profile -----------------------------------------------------------


@requires_mutmut
def test_ci_sampling_reaches_the_lane(make_repo):
    """The flag used to reach the lane and stop there.

    `_run_canonical_python_enforcement` accepted `enable_ci_mutation_sampling`
    and never read it, while the binding advertised `accepts_sampling_policy`
    and never read `context.sampling_policy` -- so a CI run did full mutation
    generation with cost control off. Not a wrong verdict, an unbounded bill,
    which is why nothing caught it: the evidence looked fine.

    `capProfile` means "which profile was in force", not "did the caps bite",
    so a small diff under CI caps is still a capped run.
    """
    capped = _cap_profile(
        run_python_enforcement(
            repo_path=make_repo(),
            target_values=["jobs/forecast.py"],
            test_paths=["tests/test_forecast.py"],
            base_ref="origin/master",
            coverage_gate=80.0,
            mutation_gate=70.0,
            enable_ci_mutation_sampling=True,
        )
    )
    uncapped = _cap_profile(_enforce(make_repo()))

    assert capped == "ci"
    assert uncapped == "full"


def _cap_profile(evidence: dict) -> str:
    policy_paths = (evidence.get("artifacts") or {}).get("mutmut_generation_policy") or []
    for path_str in policy_paths:
        path = Path(path_str)
        if path.exists():
            return str(json.loads(path.read_text(encoding="utf-8")).get("capProfile") or "")
    return ""
