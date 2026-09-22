"""A wedged in-process mutmut pytest phase aborts with a traceback.

pytest-timeout guards test setup/call/teardown, so a phase that deadlocks while
importing the target -- collection, before the first test runs -- escapes it,
and escapes the per-mutant timeout that has not started yet. Only the outer
enforcement command timeout catches it, half an hour later and without a stack.
"""

import faulthandler
import os
import re
from pathlib import Path
import subprocess
import sys

import pytest

from uta_py_enforce import mutmut_adapter_runtime

_ENFORCEMENT_ROOT = Path(__file__).resolve().parents[1] / "tools" / "python-enforcement"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_active_watchdog_survives_fork_without_blocking_child():
    script = f"""
import os, signal, time, sys
sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})
from uta_py_enforce.mutmut_adapter_runtime import _watchdog
with _watchdog(30):
    pid = os.fork()
    if pid == 0:
        with _watchdog(5):
            print('child pytest reached', flush=True)
        os._exit(0)
    for _ in range(50):
        child, status = os.waitpid(pid, os.WNOHANG)
        if child:
            assert status == 0
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise AssertionError('forked watchdog deadlocked')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert "child pytest reached" in result.stdout


def test_phase_cleanup_reimports_namespace_children(tmp_path, monkeypatch):
    import importlib

    package = tmp_path / "uta_namespace_probe"
    child = package / "nested"
    child.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (child / "target.py").write_text("VALUE = []\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    name = "uta_namespace_probe.nested.target"
    try:
        first = importlib.import_module(name)
        first.VALUE.append("stale")
        mutmut_adapter_runtime._purge_modules_loaded_from(tmp_path)
        assert "uta_namespace_probe.nested" not in sys.modules
        second = importlib.import_module(name)
        assert second.VALUE == []
        assert second is not first
        assert sys.modules["pytest"] is pytest
    finally:
        for key in list(sys.modules):
            if key == "uta_namespace_probe" or key.startswith("uta_namespace_probe."):
                sys.modules.pop(key, None)


def test_phase_cleanup_removes_namespace_only_roots(tmp_path, monkeypatch):
    import importlib

    (tmp_path / "uta_namespace_only").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        importlib.import_module("uta_namespace_only")
        mutmut_adapter_runtime._purge_modules_loaded_from(tmp_path)
        assert "uta_namespace_only" not in sys.modules
    finally:
        sys.modules.pop("uta_namespace_only", None)


def _dump_seconds(stderr: str) -> float:
    """Seconds from a faulthandler header, e.g. "Timeout (0:00:02.799)!".

    The header carries the real remaining time, which is fractionally under the
    requested deadline and, for a re-armed outer watchdog, shorter still. The
    value is the assertion worth making -- which deadline fired.
    """
    match = re.search(r"Timeout \((\d+):(\d+):([\d.]+)\)", stderr)
    assert match, f"no faulthandler dump in stderr: {stderr[:400]!r}"
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)



def test_phase_timeout_defaults_and_env_overrides():
    assert mutmut_adapter_runtime.phase_timeout_seconds({}) == (
        mutmut_adapter_runtime.DEFAULT_PHASE_TIMEOUT_SECONDS
    )
    env = {mutmut_adapter_runtime.PHASE_TIMEOUT_ENV: "45"}
    assert mutmut_adapter_runtime.phase_timeout_seconds(env) == 45


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),  # explicit opt-out
        ("-1", 0),
        ("not-a-number", mutmut_adapter_runtime.DEFAULT_PHASE_TIMEOUT_SECONDS),
        ("", mutmut_adapter_runtime.DEFAULT_PHASE_TIMEOUT_SECONDS),
    ],
)
def test_phase_timeout_rejects_unusable_values(raw, expected):
    """0 and negatives disable the watchdog; garbage falls back to the default."""
    env = {mutmut_adapter_runtime.PHASE_TIMEOUT_ENV: raw}
    assert mutmut_adapter_runtime.phase_timeout_seconds(env) == expected


def _record_watchdog(monkeypatch):
    """Record how the watchdog was armed, without arming a real one."""
    armed = []
    monkeypatch.setattr(
        faulthandler,
        "dump_traceback_later",
        lambda seconds, exit=False, file=None: armed.append(
            {"seconds": seconds, "exit": exit, "fd": file}
        ),
    )
    monkeypatch.setattr(
        faulthandler,
        "cancel_dump_traceback_later",
        lambda: armed.append("cancelled"),
    )
    return armed


def test_pytest_phase_is_bounded_and_dumps_every_thread_on_abort(monkeypatch, tmp_path):
    """The phase runs under a faulthandler watchdog that exits the process.

    ``exit=True`` matters: the phase is wedged precisely because no Python
    thread can run, so anything that needs the interpreter to cooperate --
    raising in the main thread, a pytest hook -- never fires.
    """
    armed = _record_watchdog(monkeypatch)
    monkeypatch.setenv(mutmut_adapter_runtime.PHASE_TIMEOUT_ENV, "30")
    monkeypatch.chdir(tmp_path)

    armed_during_phase = []

    def execute_pytest(_self, params, **_kwargs):
        # Armed before the phase body, so the dump target is the real stderr
        # rather than the file descriptor pytest's capture swaps in.
        armed_during_phase.extend(armed)
        return 0

    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.PytestRunner, "execute_pytest", execute_pytest
    )
    mutmut_adapter_runtime._install_pytest_phase_isolation()
    runner = object.__new__(mutmut_adapter_runtime.mutmut_main.PytestRunner)

    assert runner.execute_pytest(["-q"]) == 0

    arms = [item for item in armed_during_phase if isinstance(item, dict)]
    (watchdog,) = arms
    # ~30s: _arm_nearest_watchdog re-derives the remaining time from the deadline.
    assert 29 <= watchdog["seconds"] <= 30
    assert watchdog["exit"] is True
    assert watchdog["fd"] != 2, "must dump to a dup of stderr taken before capture"
    assert armed[-1] == "cancelled", "a phase that returns must disarm the watchdog"


def test_pytest_watchdog_is_disarmed_when_the_phase_raises(monkeypatch, tmp_path):
    armed = _record_watchdog(monkeypatch)
    monkeypatch.setenv(mutmut_adapter_runtime.PHASE_TIMEOUT_ENV, "30")
    monkeypatch.chdir(tmp_path)

    def execute_pytest(_self, _params, **_kwargs):
        raise RuntimeError("collection blew up")

    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.PytestRunner, "execute_pytest", execute_pytest
    )
    mutmut_adapter_runtime._install_pytest_phase_isolation()
    runner = object.__new__(mutmut_adapter_runtime.mutmut_main.PytestRunner)

    with pytest.raises(RuntimeError):
        runner.execute_pytest(["-q"])

    assert armed[-1] == "cancelled"


def test_pytest_watchdog_is_skipped_when_disabled(monkeypatch, tmp_path):
    """An operator can turn the watchdog off without disabling phase isolation."""
    armed = _record_watchdog(monkeypatch)
    monkeypatch.setenv(mutmut_adapter_runtime.PHASE_TIMEOUT_ENV, "0")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.PytestRunner,
        "execute_pytest",
        lambda _self, _params, **_kwargs: 0,
    )
    mutmut_adapter_runtime._install_pytest_phase_isolation()
    runner = object.__new__(mutmut_adapter_runtime.mutmut_main.PytestRunner)

    assert runner.execute_pytest(["-q"]) == 0
    assert armed == []


def test_watchdog_aborts_a_real_deadlock_and_reports_the_stack(tmp_path):
    """End-to-end: the mechanism has to survive an interpreter that cannot run.

    The unit tests above prove the watchdog is armed. This proves it fires --
    against a main thread parked on a lock nothing will release, which is the
    state the production hang left mutmut in (PID 2549222: two seconds of CPU
    across fifteen minutes, main thread in futex_wait_queue, no children).
    """
    script = tmp_path / "deadlock.py"
    script.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})\n"
        "from uta_py_enforce.mutmut_adapter_runtime import _watchdog\n"
        "with _watchdog(2):\n"
        "    threading.Event().wait()\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0, "a wedged phase must not look like success"
    assert 1.0 <= _dump_seconds(completed.stderr) <= 2.5
    assert "deadlock.py" in completed.stderr, "the stack has to name the wedged frame"


def test_repeated_phases_do_not_leak_stderr_descriptors(monkeypatch, tmp_path):
    """Every phase dups fd 2, and mutmut runs one phase per mutant.

    A dup leaked per phase exhausts the descriptor table part-way through a
    large mutant set, which would surface as an unrelated OSError deep in a
    later run rather than as anything pointing back here.
    """
    monkeypatch.setenv(mutmut_adapter_runtime.PHASE_TIMEOUT_ENV, "30")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.PytestRunner,
        "execute_pytest",
        lambda _self, _params, **_kwargs: 0,
    )
    mutmut_adapter_runtime._install_pytest_phase_isolation()
    runner = object.__new__(mutmut_adapter_runtime.mutmut_main.PytestRunner)

    def open_descriptors():
        probe = os.dup(2)
        os.close(probe)
        return probe

    before = open_descriptors()
    for _ in range(50):
        runner.execute_pytest(["-q"])

    # Other pytest resources may close while this loop runs, making the next
    # available descriptor smaller. A leak can only move it upward.
    assert open_descriptors() <= before, "each phase must close the stderr dup it took"


def test_adapter_timeout_is_off_unless_the_caller_sets_it():
    """Only the caller knows the budget it will reap the adapter at."""
    assert mutmut_adapter_runtime.adapter_timeout_seconds({}) == 0
    assert mutmut_adapter_runtime.adapter_timeout_seconds(
        {mutmut_adapter_runtime.ADAPTER_TIMEOUT_ENV: "not-a-number"}
    ) == 0
    assert mutmut_adapter_runtime.adapter_timeout_seconds(
        {mutmut_adapter_runtime.ADAPTER_TIMEOUT_ENV: "6900"}
    ) == 6900


def test_adapter_watchdog_bounds_a_wedge_outside_any_pytest_phase(tmp_path):
    """The gap iteration 1 left: mutant generation and the fork wait.

    repo_task 173 hung 2h2m on the fixed adapter because a phase wrapper cannot
    reach mutmut's pre-phase generation block or its os.wait loop. This drives
    the same watchdog the entrypoint arms, around code that never enters
    execute_pytest at all.
    """
    script = tmp_path / "outside_phase.py"
    script.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})\n"
        "from uta_py_enforce.mutmut_adapter_runtime import _watchdog\n"
        "with _watchdog(2):\n"
        "    threading.Event().wait()   # no pytest.main anywhere in this run\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )

    assert completed.returncode != 0
    assert 1.0 <= _dump_seconds(completed.stderr) <= 2.5
    assert "outside_phase.py" in completed.stderr


def test_an_inner_watchdog_does_not_destroy_the_outer_one(tmp_path):
    """The defect that made iteration 2 inert in production.

    faulthandler keeps one process-global timer. Arming an inner watchdog
    replaces the outer one, and the inner's cancel destroyed it outright, so
    the adapter-wide watchdog vanished the first time any pytest phase
    completed normally. repo_task 180 then wedged with nothing armed.
    """
    script = tmp_path / "nested.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})\n"
        "from uta_py_enforce.mutmut_adapter_runtime import _watchdog\n"
        "with _watchdog(3):                      # adapter-wide\n"
        "    with _watchdog(3600):               # one long pytest phase\n"
        "        time.sleep(0.2)\n"
        "    threading.Event().wait()            # wedge AFTER the phase returns\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )

    assert completed.returncode != 0, "the outer watchdog must survive the inner one"
    # The outer 3s deadline, less the 0.2s the inner phase consumed.
    assert 2.0 <= _dump_seconds(completed.stderr) <= 3.0
    assert "nested.py" in completed.stderr


def test_the_nearer_deadline_wins_while_both_are_armed(tmp_path):
    """An inner phase timeout shorter than the outer budget still governs."""
    script = tmp_path / "inner_first.py"
    script.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})\n"
        "from uta_py_enforce.mutmut_adapter_runtime import _watchdog\n"
        "with _watchdog(3600):\n"
        "    with _watchdog(2):\n"
        "        threading.Event().wait()\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )

    assert completed.returncode != 0
    # The inner 2s, not the outer 3600s.
    assert _dump_seconds(completed.stderr) <= 2.5


def test_mutmut_exiting_via_systemexit_still_reaches_finish_process(tmp_path):
    """The 2h hang: mutmut's exit(1) skipped the shutdown bound entirely.

    A non-daemon thread that never stops holds the interpreter in
    threading._shutdown forever. finish_process arms a daemon timer that
    force-exits, but only if it is called -- and a propagating SystemExit
    walked straight past it.
    """
    script = tmp_path / "systemexit_shutdown.py"
    script.write_text(
        "import sys, threading\n"
        f"sys.path.insert(0, {str(_ENFORCEMENT_ROOT)!r})\n"
        "from uta_py_enforce.process_completion import finish_process\n"
        "def main():\n"
        "    t = threading.Thread(target=threading.Event().wait)  # non-daemon, never ends\n"
        "    t.start()\n"
        "    raise SystemExit(1)                                   # mutmut's exit(1)\n"
        "try:\n"
        "    code = main()\n"
        "except SystemExit as exc:\n"
        "    code = exc.code if isinstance(exc.code, int) else 1\n"
        "raise SystemExit(finish_process(code, 'probe'))\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )

    assert completed.returncode == 1, "the exit code mutmut chose must survive"
    assert "background threads prevented interpreter exit" in completed.stderr
