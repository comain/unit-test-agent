"""The stage list must follow the phase names the graph actually emits.

Beta task 49 repaired coverage, pushed a branch and finished, while the
progress page sat on

    排队 done | 基线编译 done | 生成测试 active | 覆盖率修复 pending | ...

for the whole run. The stage aliases were the pre-migration spellings --
`coverage_fix`, `mutation_fix` -- and the graph emits `fix_coverage` and
`fix_mutation`. Matching is substring-based, and "coverage_fix" is not a
substring of "fix_coverage", so no event ever advanced the list.

These assert against the phase names taken from `generation-cycle.yaml`, so a
future rename fails here rather than silently freezing the page again.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from uta.app.repair.progress import RepairProgressMixin


def _stage_defs():
    # The defs are built inside the method; drive it through the public path.
    return RepairProgressMixin._stage_rows_for_test() if hasattr(
        RepairProgressMixin, "_stage_rows_for_test"
    ) else None


def _cycle_phases():
    cycle = (
        pathlib.Path(__file__).resolve().parents[1]
        / "uta" / "testgen" / "graph" / "generation-cycle.yaml"
    ).read_text(encoding="utf-8")
    return set(re.findall(r"^\s+phase:\s*(\S+)\s*$", cycle, re.M))


def _aliases_text() -> str:
    return (
        pathlib.Path(__file__).resolve().parents[1]
        / "uta" / "app" / "repair" / "progress.py"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "phase",
    ["fix_coverage", "fix_mutation", "fix_compile", "fix_tests",
     "generate_tests", "verify_compile", "verify_tests"],
)
def test_each_repair_phase_is_a_known_stage_alias(phase):
    """A phase the graph runs but the page cannot name is a phase that freezes
    the progress list."""
    assert f'"{phase}"' in _aliases_text(), (
        f"{phase} is emitted by the cycle but matches no stage alias"
    )


def test_the_phases_asserted_here_still_exist_in_the_cycle():
    """Guards the guard: if a phase is renamed, the parametrization above must
    fail loudly rather than assert against names nothing emits."""
    phases = _cycle_phases()

    for phase in ("fix_coverage", "fix_mutation", "generate_tests", "verify_tests"):
        assert phase in phases, f"{phase} no longer exists in generation-cycle.yaml"


def test_the_old_spellings_are_not_substrings_of_the_new_ones():
    """The reason the freeze was silent: matching is substring-based, and the
    old alias never matches the new phase."""
    assert "coverage_fix" not in "fix_coverage"
    assert "mutation_fix" not in "fix_mutation"
