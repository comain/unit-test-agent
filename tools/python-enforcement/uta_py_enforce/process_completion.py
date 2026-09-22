"""Bound interpreter shutdown only after the caller has finalized its result.

Kept Python-2-compatible for legacy mutmut runner commands. Never call this at
an internal pytest phase boundary inside a reusable mutation coordinator.
"""

import json
import os
import sys
import tempfile
import threading


def finish_process(exit_code, kind, coverage_saved=False):
    """Persist completion, then guard leaked threads without changing the verdict."""
    exit_code = int(exit_code)
    folder = os.path.join(os.getcwd(), ".uta_cache", "python-enforcement", "completion")
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except OSError:
            if not os.path.isdir(folder):
                raise
    fd, temporary = tempfile.mkstemp(prefix="completion-", suffix=".tmp", dir=folder)
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": os.getpid(), "exitCode": exit_code,
                   "kind": kind, "coverageSaved": coverage_saved}, handle)
    os.rename(temporary, temporary[:-4] + ".json")
    for stream in (sys.stdout, sys.stderr):
        stream.flush()

    def force_completed_exit():
        # This runs only after pytest.main (including unconfigure) and coverage
        # save returned. A stuck test/teardown never authorizes a successful exit.
        try:
            os.write(2, ("UTA pytest_shutdown_cleanup: completed %s; exitCode=%d; "
                         "background threads prevented interpreter exit\n" % (kind, exit_code)).encode("utf-8"))
            import faulthandler
            faulthandler.dump_traceback(all_threads=True)
        finally:
            os._exit(exit_code)

    try:
        grace = float(os.environ.get("UTA_PYTEST_SHUTDOWN_GRACE_SECONDS", "5"))
    except ValueError:
        grace = 5.0
    watchdog = threading.Timer(max(0.1, min(grace, 30.0)), force_completed_exit)
    watchdog.daemon = True
    watchdog.start()
    return exit_code
