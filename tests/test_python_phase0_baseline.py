import json
import os
import subprocess
from pathlib import Path

from e2e_repo_paths import python_repo_matrix


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "python_projects"
INVENTORY_PATH = REPO_ROOT / "docs" / "python-support-phase0-inventory.json"


def test_python_fixture_repos_are_small_and_open_code_free():
    py3_repo = FIXTURE_ROOT / "py3_flat_project"
    py2_repo = FIXTURE_ROOT / "py2_legacy_project"

    assert (py3_repo / "jobs" / "forecast.py").is_file()
    assert (py3_repo / "tests" / "test_forecast.py").is_file()
    assert (py2_repo / "legacy_job.py").is_file()
    assert (py2_repo / "tests" / "test_legacy_job.py").is_file()

    all_text = "\n".join(path.read_text(encoding="utf-8") for path in FIXTURE_ROOT.rglob("*") if path.is_file())
    assert "OpenCode" not in all_text
    assert "opencode" not in all_text
    assert "print 'legacy fixture ready'" in (py2_repo / "legacy_job.py").read_text(encoding="utf-8")


def test_python3_fixture_unit_tests_run_without_opencode():
    py3_repo = FIXTURE_ROOT / "py3_flat_project"

    result = subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        cwd=str(py3_repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ran 2 tests" in result.stderr


def test_python_e2e_repo_matrix_defaults_and_env_overrides(monkeypatch, tmp_path):
    py3_override = tmp_path / "py3"
    py2_override = tmp_path / "py2"
    monkeypatch.setenv("UTA_E2E_PY3_REPO", str(py3_override))
    monkeypatch.setenv("UTA_E2E_PY3_TARGET", "jobs/custom.py::main")
    monkeypatch.setenv("UTA_E2E_PY3_TEST_COMMAND", "python3 -m pytest")
    monkeypatch.setenv("UTA_E2E_PY2_REPO", str(py2_override))
    monkeypatch.setenv("UTA_E2E_PY2_TARGET", "legacy_custom.py")
    monkeypatch.setenv("UTA_E2E_PY2_TEST_COMMAND", "python2 -m unittest discover")
    monkeypatch.setenv("UTA_E2E_PY2_BIN", "/opt/python2/bin/python")
    monkeypatch.setenv("UTA_E2E_PY2_MUTMUT_BIN", "/opt/python2/bin/mutmut")

    rows = {row["language"]: row for row in python_repo_matrix()}

    assert rows["python3"]["repo_path"] == os.path.abspath(str(py3_override))
    assert rows["python3"]["target"] == "jobs/custom.py::main"
    assert rows["python3"]["test_command"] == "python3 -m pytest"
    assert rows["python2"]["repo_path"] == os.path.abspath(str(py2_override))
    assert rows["python2"]["target"] == "legacy_custom.py"
    assert rows["python2"]["test_command"] == "python2 -m unittest discover"
    assert rows["python2"]["python_bin"] == "/opt/python2/bin/python"
    assert rows["python2"]["mutmut_bin"] == "/opt/python2/bin/mutmut"


def test_phase0_inventory_maps_java_shaped_surfaces_to_later_phases():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    surfaces = {item["surface"]: item for item in inventory["surfaces"]}
    required = {
        "uta run",
        "uta tasks create",
        "uta tasks create-manifest",
        "uta tasks enqueue",
        "uta tasks show/watch/summary/export/dashboard/report",
        "uta tasks reprioritize-class",
        "uta resume-gates",
        "uta scan",
        "uta parse",
        "uta query-index",
        "bin/uta-query-index",
        "scripts/enqueue.sh",
        "scripts/start_daemon.sh",
        "scripts/start_ci_plugin.sh",
        "scripts/deploy_single_host.sh",
        "scripts/setup-fetchcode.py",
        "ci plugin enforcement/report/repair",
        "ci auto-push",
        "learning recorder/summary",
        "opencode retrospective hints",
        "e2e harness",
    }

    assert inventory["phase"] == 0
    assert required.issubset(surfaces)
    for item in surfaces.values():
        assert item["owner_phase"] > 0
        assert item["current_java_assumption"]
        assert item["required_python_treatment"]
        for path in item["paths"]:
            assert (REPO_ROOT / path).exists(), path
