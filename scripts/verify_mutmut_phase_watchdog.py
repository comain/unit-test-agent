"""Verify the DEPLOYED mutmut phase watchdog closes the task-171 hang.

Two checks against the adapter file the node actually execs:

  wiring    -- _install_pytest_phase_isolation wraps execute_pytest in the
               watchdog, so every in-process pytest phase is bounded.
  mechanism -- the deployed _phase_watchdog aborts a real import-time deadlock
               and prints a stack, the failure mode that wedged task 171.

The mechanism check runs in a subprocess because a firing watchdog exits.
Exit 0 only when both pass, so a scheduler can gate on it.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

ADAPTER = Path(
    "/opt/app/unit_test_agent/tools/python-enforcement/uta_py_enforce/mutmut_adapter_runtime.py"
)
ENFORCEMENT_ROOT = ADAPTER.parent.parent
PYTHON = "/opt/app/unit_test_agent/.venv311/bin/python"
PHASE_TIMEOUT = 15


def check_wiring() -> bool:
    source = ADAPTER.read_text(encoding="utf-8")
    needed = ("_phase_watchdog", "phase_timeout_seconds", "dump_traceback_later")
    missing = [name for name in needed if name not in source]
    if missing:
        print(f"WIRING: FAIL -- deployed adapter lacks {missing} (fix not deployed)")
        return False
    isolation = source[source.index("def _install_pytest_phase_isolation") :]
    isolation = isolation[: isolation.index("\ndef ", 1)]
    if "_phase_watchdog" not in isolation:
        print("WIRING: FAIL -- execute_pytest is not wrapped by the watchdog")
        return False
    print("WIRING: PASS -- every in-process pytest phase runs under the watchdog")
    return True


def check_mechanism() -> bool:
    script = textwrap.dedent(
        f"""
        import sys, threading
        sys.path.insert(0, {str(ENFORCEMENT_ROOT)!r})
        from uta_py_enforce.mutmut_adapter_runtime import _phase_watchdog
        with _phase_watchdog({PHASE_TIMEOUT}):
            threading.Event().wait()   # the interpreter can no longer make progress
        print("NEVER REACHED")
        """
    )
    limit = PHASE_TIMEOUT * 6
    try:
        done = subprocess.run(
            [PYTHON, "-c", script], capture_output=True, text=True, timeout=limit
        )
    except subprocess.TimeoutExpired:
        print(f"MECHANISM: FAIL -- still wedged after {limit}s, watchdog never fired")
        return False
    if done.returncode == 0 or "NEVER REACHED" in done.stdout:
        print("MECHANISM: FAIL -- the wedged phase reported success")
        return False
    if "Timeout (" not in done.stderr:
        print(f"MECHANISM: FAIL -- aborted without a faulthandler dump: {done.stderr[-300:]}")
        return False
    head = done.stderr[done.stderr.index("Timeout (") :].splitlines()[:4]
    print(f"MECHANISM: PASS -- aborted with a stack (rc={done.returncode})")
    for line in head:
        print(f"    {line}")
    return True


if __name__ == "__main__":
    results = [check_wiring(), check_mechanism()]
    print("VERDICT: " + ("CLOSED" if all(results) else "NOT CLOSED"))
    sys.exit(0 if all(results) else 1)
