"""The two lanes ask for strict test candidates through different doors.

Legacy selects in UTA (`_target_evidence_test_paths` ->
`discover_strict_python_test_candidates`) and hands the chosen paths to the
verification runner. The canonical lane selects inside the binding
(`uta_py_enforce.api` -> `strict_test_candidates`) as part of enforcing.

That difference is why the CLI tests cannot be ported by stubbing: a binding
stub replaces canonical's selection entirely, so those tests would assert
nothing about it. The saving grace is that both doors open onto the same
module -- but they are *separate implementations* in it, one returning matches
and one returning paths, so "same module" is not "same answer". This pins the
answer.

If these ever diverge, the lanes disagree about which test verifies a target,
and the CLI suite would catch it only for whichever lane it happens to run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uta_py_enforce.test_selection import (
    discover_strict_python_test_candidates,
    strict_test_candidates,
)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


SOURCE = "def run():\n    return 1\n"
TEST = "from jobs.forecast import run\n\n\ndef test_run():\n    assert run() == 1\n"


CASES = {
    "only-a-strict-test": {
        "jobs/forecast.py": SOURCE,
        "tests/test_forecast.py": TEST,
    },
    "a-broad-test-beside-a-strict-one": {
        "jobs/forecast.py": SOURCE,
        "tests/test_forecast.py": TEST,
        "tests/test_everything.py": "def test_all():\n    assert True\n",
    },
    "an-existing-generated-test": {
        "jobs/forecast.py": SOURCE,
        "tests/test_forecast.py": TEST,
        "tests/uta_generated/test_forecast.py": TEST,
    },
    "a-package-local-test-directory": {
        "jobs/forecast.py": SOURCE,
        "jobs/test/check_test.py": "def test_check():\n    assert True\n",
        "tests/test_forecast.py": TEST,
    },
    "no-test-at-all": {
        "jobs/forecast.py": SOURCE,
    },
}


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_the_two_entry_points_select_the_same_tests(tmp_path, case):
    repo = _repo(tmp_path, CASES[case])

    through_uta = [
        match.path
        for match in discover_strict_python_test_candidates(repo, "jobs/forecast.py")
    ]
    through_binding = strict_test_candidates(repo, "jobs/forecast.py")

    assert [str(path) for path in through_uta] == [
        str(path) for path in through_binding
    ], case


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_they_agree_when_a_test_path_is_configured(tmp_path, case):
    """The configured path is the CLI's `--test-path`, and it is the input the
    lanes are most likely to treat differently: legacy filters it after
    discovery, the binding passes it in as a preference."""
    repo = _repo(tmp_path, CASES[case])
    configured = ["tests/test_forecast.py"]

    through_uta = [
        match.path
        for match in discover_strict_python_test_candidates(repo, "jobs/forecast.py")
    ]
    through_binding = strict_test_candidates(
        repo, "jobs/forecast.py", configured=configured
    )

    if not through_uta:
        assert through_binding == [] or all(
            str(path) in configured for path in through_binding
        ), case
        return
    assert str(through_binding[0]) in [str(path) for path in through_uta], case
