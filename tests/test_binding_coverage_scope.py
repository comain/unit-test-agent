"""The binding's changed-line coverage scope, at its two degenerate edges.

Legacy distinguished "no filter requested" (`None`) from "the filter matches
nothing" (an empty set). The binding takes only a sequence, so empty is the
only way to say "no lines are in scope" -- and guarding the filter on
truthiness silently turned that into "count the whole file". A deletion-only
file then reported a coverage obligation its diff never created, which cost a
real report ~8 points of aggregate coverage.
"""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.coverage import parse_coverage_xml

_XML = """<?xml version="1.0" ?>
<coverage>
  <packages><package><classes>
    <class filename="jobs/forecast.py">
      <lines>
        <line number="1" hits="1"/>
        <line number="2" hits="0"/>
        <line number="3" hits="0"/>
        <line number="4" hits="1"/>
      </lines>
    </class>
  </classes></package></packages>
</coverage>
"""


def _xml(tmp_path: Path) -> Path:
    path = tmp_path / "coverage.xml"
    path.write_text(_XML, encoding="utf-8")
    return path


def test_no_changed_lines_measures_nothing(tmp_path):
    """The regression: an empty scope must not fall back to the whole file."""
    summary = parse_coverage_xml(_xml(tmp_path), "jobs/forecast.py", [], 95.0)

    assert summary["total"] == 0
    assert summary["covered"] == 0
    assert summary["rate"] == 100.0
    assert summary["passed"] is True


def test_changed_lines_scope_the_measurement(tmp_path):
    summary = parse_coverage_xml(_xml(tmp_path), "jobs/forecast.py", [1, 2], 95.0)

    assert summary["total"] == 2
    assert summary["covered"] == 1
    assert summary["rate"] == 50.0
    assert summary["passed"] is False


def test_changed_lines_that_are_not_executable_are_reported_as_such(tmp_path):
    """Lines the diff touched but coverage never emits: a real skip signal,
    and one an empty scope must not be confused with."""
    summary = parse_coverage_xml(_xml(tmp_path), "jobs/forecast.py", [99], 95.0)

    assert summary["total"] == 0
    assert summary["no_executable_changed_lines"] is True
    assert summary["rate"] == 100.0


def test_an_unseen_source_with_changed_lines_scores_zero(tmp_path):
    """A file the coverage run never loaded is not vacuously covered."""
    summary = parse_coverage_xml(_xml(tmp_path), "jobs/missing.py", [3], 95.0)

    assert summary["total"] == 0
    assert summary["rate"] == 0.0
    assert summary["no_executable_changed_lines"] is False
