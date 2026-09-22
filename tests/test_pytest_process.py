"""Real-process regressions for post-pytest interpreter shutdown."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / "tools/python-enforcement"
ENTRY = ROOT / "uta_py_enforce/pytest_process.py"


def invoke(repo, *args, timeout=8):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               UTA_PYTEST_SHUTDOWN_GRACE_SECONDS="0.1")
    env.pop("PYTEST_PLUGINS", None)
    return subprocess.run([sys.executable, str(ENTRY), *args], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("fails", [False, True])
def test_completed_pytest_preserves_result_with_leaked_thread(tmp_path, fails):
    (tmp_path / "test_leak.py").write_text(
        "import threading\n"
        "def test_leak():\n"
        "    threading.Thread(target=threading.Event().wait).start()\n"
        f"    assert {not fails}\n"
    )
    result = invoke(tmp_path, "--", "-q", "test_leak.py")
    assert result.returncode == int(fails), result.stderr
    assert "pytest_shutdown_cleanup" in result.stderr
    records = list((tmp_path / ".uta_cache/python-enforcement/completion").glob("*.json"))
    assert json.loads(records[0].read_text())["exitCode"] == int(fails)


def test_leaked_thread_keeps_real_coverage(tmp_path):
    (tmp_path / "subject.py").write_text("def f():\n    return 42\n")
    (tmp_path / "test_subject.py").write_text(
        "import subject, threading\n"
        "def test_f():\n"
        "    threading.Thread(target=threading.Event().wait).start()\n"
        "    assert subject.f() == 42\n"
    )
    result = invoke(tmp_path, "--coverage-include", "subject.py", "--", "-q", "test_subject.py")
    assert result.returncode == 0, result.stderr
    import coverage
    data = coverage.CoverageData(str(tmp_path / ".coverage"))
    data.read()
    assert 2 in data.lines(str(tmp_path / "subject.py"))


def test_normal_completion_needs_no_forced_exit(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): assert True\n")
    result = invoke(tmp_path, "--", "-q", "test_ok.py")
    assert result.returncode == 0, result.stderr
    assert "pytest_shutdown_cleanup" not in result.stderr


def test_session_hook_failure_cannot_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): assert True\n")
    (tmp_path / "conftest.py").write_text("def pytest_sessionfinish(session): raise RuntimeError('cleanup broke')\n")
    result = invoke(tmp_path, "--", "-q", "test_ok.py")
    assert result.returncode != 0
    assert "cleanup broke" in result.stdout + result.stderr


def test_hanging_teardown_has_no_completion_record(tmp_path):
    (tmp_path / "test_hang.py").write_text(
        "import pytest, time\n@pytest.fixture\ndef x():\n    yield\n    time.sleep(20)\n"
        "def test_ok(x): assert True\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(tmp_path, "--", "-q", "test_hang.py", timeout=2)
    assert not list((tmp_path / ".uta_cache/python-enforcement/completion").glob("*.json"))


def test_coverage_save_failure_cannot_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok(): assert True\n")
    (tmp_path / "conftest.py").write_text(
        "import coverage\ndef broken(self): raise RuntimeError('save broke')\n"
        "coverage.Coverage.save = broken\n"
    )
    result = invoke(tmp_path, "--coverage-include", "test_ok.py", "--", "-q", "test_ok.py")
    assert result.returncode != 0
    assert "save broke" in result.stdout + result.stderr
    assert not list((tmp_path / ".uta_cache/python-enforcement/completion").glob("*.json"))


def test_old_pytest_entrypoint_reproduces_shutdown_hang(tmp_path):
    (tmp_path / "test_leak.py").write_text(
        "import threading\ndef test_ok():\n"
        "    threading.Thread(target=threading.Event().wait).start()\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as error:
        subprocess.run([sys.executable, "-m", "pytest", "-q", "test_leak.py"],
                       cwd=tmp_path, capture_output=True, timeout=2,
                       env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"))
    assert b"1 passed" in error.value.stdout


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5])
def test_shared_completion_preserves_mutation_process_exit_code(tmp_path, code):
    script = (
        "import threading,sys; from uta_py_enforce.process_completion import finish_process; "
        "threading.Thread(target=threading.Event().wait).start(); "
        f"sys.exit(finish_process({code}, 'mutmut_adapter'))"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            env=dict(os.environ, PYTHONPATH=str(ROOT),
                                     UTA_PYTEST_SHUTDOWN_GRACE_SECONDS="0.1"),
                            capture_output=True, timeout=5)
    assert result.returncode == code
