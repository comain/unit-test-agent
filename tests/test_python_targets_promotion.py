"""Tests for target resolution, changed lines derivation, and non-executable shortcuts."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.targets import (
    has_only_non_executable_changed_lines,
    non_executable_target_evidence,
    resolve_enforcement_targets,
)
from uta_enforce_core.contracts import EnforcementTarget


def test_resolve_enforcement_targets_explicit(tmp_path: Path):
    targets = resolve_enforcement_targets(
        tmp_path,
        ["pkg/sub/module.py", EnforcementTarget(language="python", target_id="pyfile:a.py", source_path="a.py")],
    )
    assert len(targets) == 2
    assert targets[0].source_path == "pkg/sub/module.py"
    assert targets[1].source_path == "a.py"


def test_has_only_non_executable_changed_lines(tmp_path: Path):
    py_file = tmp_path / "service.py"
    py_file.write_text("""# Header comment
# Another comment

def run():
    return 42
""")
    # Lines 1 and 2 are comments
    assert has_only_non_executable_changed_lines(tmp_path, "service.py", [1, 2]) is True

    # Line 4 is 'def run():' (executable)
    assert has_only_non_executable_changed_lines(tmp_path, "service.py", [1, 4]) is False


def test_non_executable_target_evidence():
    evidence = non_executable_target_evidence("service.py", [1, 2], coverage_gate=0.8, mutation_gate=0.6)
    assert evidence["status"] == "passed"
    assert evidence["reasonCode"] == "no_executable_changed_lines"
    assert evidence["coverage"]["noExecutableChangedLines"] is True
    assert evidence["coverage"]["rate"] == 100.0
    assert evidence["mutation"]["runtimeLane"] == "not_run"
