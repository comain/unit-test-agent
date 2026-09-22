"""Python survivors for the equivalent-mutant review come from mutmut's own ids."""

from __future__ import annotations

import hashlib
from pathlib import Path

from uta.language.python.equivalence import python_scoring_survivors

SOURCE = "pkg/text.py"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / SOURCE).write_text("def text_similarity(left, right):\n    return 0.0\n")
    return tmp_path


def _survivor(
    mutant_id="pkg.text.x_text_similarity__mutmut_3",
    line=2,
    file=SOURCE,
    mutation_diff="",
):
    survivor = {"file": file, "line": line, "description": "or -> and", "id": mutant_id}
    if mutation_diff:
        survivor["mutmut_show_output"] = mutation_diff
    return survivor


def _ci_mutation(**overrides):
    """The CI gate's aggregated `evidence.mutation` shape."""
    payload = {
        "generated": 10, "killed": 8, "survived": 2, "timeout": 0, "suspicious": 0,
        "noTests": 1, "rate": 80.0, "gate": 95.0, "passed": False, "scope": "changed_lines",
        "diff_survivors": [_survivor(), _survivor("pkg.text.x_text_similarity__mutmut_4", line=2)],
    }
    payload.update(overrides)
    return payload


def _cycle_mutation(**overrides):
    """The generation cycle's `MutationSummary.as_dict()` shape."""
    payload = {
        "generated": 10, "killed": 9, "survived": 1, "no_coverage": 0, "no_tests": 2,
        "timeout": 0, "suspicious": 0, "rate": 90.0, "gate": 95.0, "passed": False,
        "scope": "changed_lines", "diff_survivors": [_survivor()], "survivors": [_survivor(), _survivor("other")],
        "sampling": {},
    }
    payload.update(overrides)
    return payload


def test_ci_shape_yields_mutmut_ids_and_fingerprints(tmp_path):
    repo = _repo(tmp_path)

    survivors = python_scoring_survivors(_ci_mutation(), repo)

    assert survivors is not None
    assert survivors.language == "python"
    assert sorted(survivors.keys) == [
        "pkg.text.x_text_similarity__mutmut_3", "pkg.text.x_text_similarity__mutmut_4",
    ]
    assert survivors.unreviewed_scoring_failures == 0
    assert survivors.source_fingerprints == {SOURCE: hashlib.sha256((repo / SOURCE).read_bytes()).hexdigest()}
    assert (survivors.mutation_rate, survivors.mutation_gate) == (80.0, 95.0)


def test_cycle_shape_uses_diff_survivors_when_scoped(tmp_path):
    survivors = python_scoring_survivors(_cycle_mutation(), _repo(tmp_path))
    assert survivors is not None
    assert survivors.keys == {"pkg.text.x_text_similarity__mutmut_3"}


def test_cycle_shape_preserves_exact_mutation_diff_for_review(tmp_path):
    exact_diff = "--- before\n+++ after\n-    enabled = False\n+    enabled = None"
    mutation = _cycle_mutation(
        diff_survivors=[_survivor(mutation_diff=exact_diff)],
    )

    survivors = python_scoring_survivors(mutation, _repo(tmp_path))

    assert survivors is not None
    assert survivors.mutants[0].mutation_diff == exact_diff


def test_cycle_shape_recovers_survivor_location_from_candidate_plan(tmp_path):
    mutant_id = "pkg.text.x_text_similarity__mutmut_3"
    mutation = _cycle_mutation(
        diff_survivors=[],
        survivors=[_survivor(mutant_id=mutant_id, line=0, file="")],
        candidate_plan={
            "adapterFilteredGenerationApplied": True,
            "activeSelected": [
                {
                    "toolCandidateKey": mutant_id,
                    "opportunity": {"sourcePath": SOURCE, "line": 2},
                }
            ]
        },
    )

    survivors = python_scoring_survivors(mutation, _repo(tmp_path))

    assert survivors is not None
    assert survivors.mutants[0].source_path == SOURCE
    assert survivors.mutants[0].line == 2


def test_whole_file_scope_uses_all_survivors(tmp_path):
    mutation = _cycle_mutation(scope="target_file", survived=2, diff_survivors=[])
    survivors = python_scoring_survivors(mutation, _repo(tmp_path))
    assert survivors is not None and len(survivors.mutants) == 2


def test_timeout_and_suspicious_count_as_unreviewed_but_no_tests_does_not(tmp_path):
    survivors = python_scoring_survivors(_ci_mutation(timeout=1, suspicious=2, noTests=7), _repo(tmp_path))
    assert survivors is not None
    assert survivors.unreviewed_scoring_failures == 3


def test_unprovable_inputs_return_none(tmp_path):
    repo = _repo(tmp_path)
    no_id = {k: v for k, v in _survivor().items() if k != "id"}
    assert python_scoring_survivors(_ci_mutation(sampled=True), repo) is None
    assert python_scoring_survivors(_cycle_mutation(sampling={"enabled": True}), repo) is None
    assert python_scoring_survivors(_ci_mutation(diff_survivors=[no_id, _survivor("b")]), repo) is None
    assert python_scoring_survivors(_ci_mutation(diff_survivors=[_survivor(file=""), _survivor("b")]), repo) is None
    assert python_scoring_survivors(_ci_mutation(survived=3), repo) is None
    assert python_scoring_survivors(_ci_mutation(diff_survivors=[_survivor(), _survivor()]), repo) is None
    assert python_scoring_survivors(_ci_mutation(diff_survivors=[_survivor(file="gone.py"), _survivor("b")]), repo) is None
    assert python_scoring_survivors({}, repo) is None
    assert python_scoring_survivors(None, repo) is None
