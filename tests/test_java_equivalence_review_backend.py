"""Java's delegated gate feeds the equivalent-mutant review its own survivors."""

from __future__ import annotations

from uta.language.java.generation_backend import JavaGenerationCycleBackend

from tests.test_java_equivalence_extractor import _fixture, _gate


def _state(repo, gate):
    return {
        "repo_path": str(repo),
        "phase_results": {
            "delegated_quality_gate_verify": {
                "phase_outcome": "review_equivalence",
                "evidence": {"quality_gate_result": gate},
            }
        },
    }


def test_delegated_gate_result_supplies_the_survivors(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    gate = _gate(repo, rows, detected=2, total=3)
    gate["evidence"]["baseRef"] = base

    survivors = JavaGenerationCycleBackend().scoring_survivors(_state(repo, gate))

    assert survivors is not None
    assert survivors.language == "java"
    assert len(survivors.mutants) == 1


def test_no_delegated_gate_result_means_no_survivors(tmp_path):
    repo, _base, _rows = _fixture(tmp_path)
    backend = JavaGenerationCycleBackend()

    assert backend.scoring_survivors({"repo_path": str(repo), "phase_results": {}}) is None
    assert backend.scoring_survivors(_state(repo, {})) is None
