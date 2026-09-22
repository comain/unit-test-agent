"""Tests for promoted Python runtime resolution and syntax precheck."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.runtime import (
    check_python_syntax_compatibility,
    resolve_python_runtime,
)


def test_resolve_python_runtime_defaults():
    res = resolve_python_runtime()
    assert res.lane == "mutmut-modern"
    assert res.is_python2 is False
    assert res.environment_profile == "python3"
    assert res.python_bin != ""


def test_resolve_python_runtime_py2():
    res = resolve_python_runtime(syntax_version="python2.7", python2_bin="/usr/bin/python2")
    assert res.lane == "mutmut-legacy-py2"
    assert res.is_python2 is True
    assert res.environment_profile == "python2"
    assert res.python_bin == "/usr/bin/python2"


def test_check_python_syntax_compatibility_valid(tmp_path: Path):
    file_path = tmp_path / "valid.py"
    file_path.write_text("def hello(name: str) -> str:\n    return f'Hello {name}'\n")
    failed = check_python_syntax_compatibility(tmp_path, ["valid.py"])
    assert failed is None


def test_check_python_syntax_compatibility_py2_print_statement(tmp_path: Path):
    file_path = tmp_path / "py2_syntax.py"
    # Python 2 print statement without parentheses is invalid in Python 3
    file_path.write_text("print 'hello world'\n")
    failed = check_python_syntax_compatibility(tmp_path, ["py2_syntax.py"], target_syntax="python3")
    assert failed is not None
    path, err = failed
    assert path == "py2_syntax.py"
    assert "SyntaxError" in err
