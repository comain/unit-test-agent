"""The standalone adapter must preserve the caller's execution worker limit."""

import json
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize("workers,expected", [(1, "1"), (3, "3"), (0, "1")])
def test_run_forwards_worker_limit(tmp_path, monkeypatch, modern, workers, expected):
    backend = ModuleType("mutmut.__main__")
    package = ModuleType("mutmut")
    package.__path__ = []
    package.__main__ = backend
    monkeypatch.setitem(sys.modules, "mutmut", package)
    monkeypatch.setitem(sys.modules, "mutmut.__main__", backend)
    calls = []
    if modern:
        backend.cli = lambda **kwargs: calls.append(kwargs)
    else:
        monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: calls.append(list(sys.argv)))
    path = Path(__file__).resolve().parents[1] / "tools/python-enforcement/uta_py_enforce/mutmut_adapter_runtime.py"
    module = runpy.run_path(str(path))
    main = module["main"]
    monkeypatch.setitem(main.__globals__, "_install_policy", lambda _: None)
    monkeypatch.setitem(main.__globals__, "_install_pytest_phase_isolation", lambda: None)
    monkeypatch.setattr(sys, "argv", ["adapter"])
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({}))
    assert main([str(workers), "run", str(policy)]) == 0
    expected_args = ["run", "--max-children", expected]
    assert calls == ([{"args": expected_args, "standalone_mode": False}]
                     if modern else [["mutmut", *expected_args]])
