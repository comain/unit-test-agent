"""Changed-line coverage verification for lightweight Python enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET

from uta_py_enforce.pytest_env import pytest_env, pytest_process_command
from uta_py_enforce.runtime import run_command
from uta_enforce_core.targets import safe_name


def run_coverage(
    repo: Path,
    source_path: str,
    test_path: str,
    changed_lines: Mapping[str, Sequence[int]],
    gate: float,
    python_bin: str,
    timeout: int,
    commands: list[dict[str, Any]],
    *,
    execution_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    coverage_dir = repo / ".uta_cache" / "python-enforcement" / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    coverage_xml = coverage_dir / f"{safe_name(source_path)}.xml"
    run_command("coverage_erase", [python_bin, "-m", "coverage", "erase"], repo, timeout, commands)
    include = f"{source_path},*/{source_path}"
    # The repository's tests import their own conftest and siblings by bare
    # name; without the import roots those fail at collection and the target
    # never reaches coverage.
    test_env = pytest_env(repo, [test_path], base_env=execution_env)
    result = run_command("pytest_coverage_run", pytest_process_command(python_bin, [test_path], coverage_include=include), repo, timeout, commands, env=test_env)
    if result["exitCode"] != 0:
        return coverage_summary(
            source_path, 0, 1, gate, False, str(coverage_xml),
            changed_lines.get(source_path, []), tests_pass=False,
        )
    xml = run_command("coverage_xml", [python_bin, "-m", "coverage", "xml", "-i", f"--include={include}", "-o", str(coverage_xml)], repo, timeout, commands)
    if xml["exitCode"] != 0 or not coverage_xml.exists():
        return coverage_summary(source_path, 0, 1, gate, False, str(coverage_xml), changed_lines.get(source_path, []))
    return parse_coverage_xml(coverage_xml, source_path, changed_lines.get(source_path, []), gate)


def parse_coverage_xml(xml_path: Path, source_path: str, changed_lines: Sequence[int], gate: float) -> dict[str, Any]:
    changed = {int(line) for line in changed_lines if int(line) > 0}
    covered = 0
    total = 0
    source_seen = False
    root = ET.parse(xml_path).getroot()
    for class_node in root.findall(".//class"):
        filename = str(class_node.attrib.get("filename") or "").replace("\\", "/")
        if filename != source_path and not filename.endswith("/" + source_path):
            continue
        source_seen = True
        for line_node in class_node.findall(".//line"):
            line_number = int(line_node.attrib.get("number") or 0)
            # An empty changed-line set means "no lines are in scope", not "no
            # filter". Guarding on truthiness conflated the two, so a
            # deletion-only file -- which has no added lines at all -- had its
            # whole body counted and reported a coverage obligation the diff
            # never created.
            if line_number not in changed:
                continue
            total += 1
            if int(line_node.attrib.get("hits") or 0) > 0:
                covered += 1
    requested = bool(changed)
    no_executable = bool(requested and source_seen and total == 0)
    if requested and total == 0 and not source_seen:
        rate = 0.0
    else:
        rate = 100.0 if total == 0 else round((covered / total) * 100.0, 4)
    return coverage_summary(source_path, covered, total, gate, rate >= gate, str(xml_path), changed_lines, no_executable)


def coverage_summary(
    source_path: str,
    covered: int,
    total: int,
    gate: float,
    passed: bool,
    xml_path: str,
    changed_lines: Sequence[int],
    no_executable: bool = False,
    tests_pass: bool = True,
) -> dict[str, Any]:
    rate = 100.0 if total == 0 and passed else (0.0 if total == 0 else round((covered / total) * 100.0, 4))
    return {
        "covered": covered,
        "total": total,
        "rate": rate,
        "gate": gate,
        "passed": passed,
        # Whether the test run itself succeeded, kept separate from whether
        # coverage met the gate. Without it a failing test is indistinguishable
        # from a passing test that covered nothing, and the caller reports
        # "coverage failed" to someone whose test did not run.
        "testsPass": tests_pass,
        "xml_path": xml_path,
        "scope": "changed_lines",
        "changed_lines": {source_path: list(changed_lines)},
        "no_executable_changed_lines": no_executable,
    }
