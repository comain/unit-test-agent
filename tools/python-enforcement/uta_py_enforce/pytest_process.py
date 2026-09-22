"""Standalone pytest boundary for coverage and legacy mutation subprocesses."""

import argparse
import os
import sys

# Executed by file path under the target interpreter, including legacy Python 2.
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
from process_completion import finish_process
sys.path[:] = [entry for entry in sys.path if entry != _script_dir]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-include")
    parser.add_argument("--coverage-omit")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.args
    if args[:1] == ["--"]:
        args = args[1:]
    # Match `python -m pytest`: the working directory is importable.
    sys.path.insert(0, os.getcwd())
    cov = None
    if options.coverage_include:
        import coverage
        cov = coverage.Coverage(include=options.coverage_include.split(","),
                                omit=options.coverage_omit.split(",") if options.coverage_omit else None)
        cov.start()
    import pytest
    code = int(pytest.main(args))
    if cov is not None:
        cov.stop()
        cov.save()
    return finish_process(code, "pytest", coverage_saved=cov is not None)


if __name__ == "__main__":
    raise SystemExit(main())
