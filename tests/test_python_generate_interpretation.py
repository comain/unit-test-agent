"""Recognising a test the agent wrote as a file rather than as a reply.

Found by replaying a production task on beta. The agent produced a 374-line
test, coverage measured `line-rate="1"`, and the cycle reported:

    failure_reason: python_test_file_not_produced
    terminal_status: PROVIDER_ERROR
    target: FAIL cov=0.0

`interpret_generate` read its "before" snapshot of the generated file *after*
the turn had already run. When the agent writes the file directly — which is
what it normally does, rather than pasting the test into its reply — that
snapshot is the agent's own new content. So `generated_preexisted` is true and
`generated_before` equals what is on disk, `changed` computes false, and the
file is treated as untouched. Interpretation then falls back to scraping the
reply text, finds no test there, and declares nothing was produced.

The consequence is worse than a bad status: the phase fails, the cycle routes
straight to completion, and verification and measurement never run. A good
test is thrown away and reported as a 0% failure.

The observation has to be made before the turn. It is taken from the ledger's
`output_fingerprints_before`, built from the `backend.output_paths` that both
languages already declare -- Java answers the same question by checking its
expected test files exist, so this belongs in the neutral layer, not in a
Python-specific snapshot.

These tests pin both halves: a file created during the turn is recognised, and
one genuinely left untouched is still not mistaken for new work.
"""

from __future__ import annotations


from uta.language.python import phases as cycle_phases

def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


TEST_BODY = (
    "# UTA-OWNED GENERATED TEST\n"
    "def test_addition_returns_sum():\n"
    "    assert 1 + 1 == 2\n"
)


def make_state(tmp_path, **overrides):
    repo = tmp_path / "repo"
    (repo / "tests" / "uta_generated").mkdir(parents=True, exist_ok=True)
    state = {
        "repo_path": str(repo),
        "generated_test_path": "tests/uta_generated/test_thing.py",
        "turn_status": "completed",
        "turn_text": "I wrote the tests to the requested file.",
        "target": {
            "target_id": "pyfile:pkg/thing.py",
            "source_path": "pkg/thing.py",
            "language": "python",
        },
    }
    state.update(overrides)
    return repo, state


def test_a_file_the_agent_wrote_during_the_turn_is_recognised(tmp_path):
    """The beta case exactly: no reply body, file created by the agent."""
    repo, state = make_state(tmp_path)
    # Recorded by the ledger before the turn: the declared output was absent.
    state["output_fingerprints_before"] = {
        "tests/uta_generated/test_thing.py": "<missing>"
    }
    (repo / "tests" / "uta_generated" / "test_thing.py").write_text(TEST_BODY, encoding="utf-8")

    result = cycle_phases.interpret_generate(state, {"status": "completed", "text": ""})

    assert result["phase_outcome"] != "failed", (
        f"a produced test was reported as failed: {result.get('evidence')}"
    )


def test_an_untouched_existing_file_is_not_mistaken_for_new_work(tmp_path):
    """The safety half: if the agent changed nothing, nothing was produced."""
    repo, state = make_state(tmp_path)
    generated = repo / "tests" / "uta_generated" / "test_thing.py"
    generated.write_text(TEST_BODY, encoding="utf-8")
    # Recorded before the turn: this exact content was already there.
    state["output_fingerprints_before"] = {
        "tests/uta_generated/test_thing.py": _digest(TEST_BODY)
    }

    result = cycle_phases.interpret_generate(state, {"status": "completed", "text": ""})

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "python_test_file_not_produced"


def test_an_edit_to_an_existing_file_counts_as_produced(tmp_path):
    repo, state = make_state(tmp_path)
    generated = repo / "tests" / "uta_generated" / "test_thing.py"
    generated.write_text(TEST_BODY + "\ndef test_more():\n    assert True\n", encoding="utf-8")
    state["output_fingerprints_before"] = {
        "tests/uta_generated/test_thing.py": _digest(TEST_BODY)
    }

    result = cycle_phases.interpret_generate(state, {"status": "completed", "text": ""})

    assert result["phase_outcome"] != "failed"


def test_without_recorded_fingerprints_it_falls_back_to_reading_disk(tmp_path):
    """An embedder driving the phase directly records nothing; it keeps
    today's behaviour rather than crashing on a missing key."""
    repo, state = make_state(tmp_path)
    (repo / "tests" / "uta_generated" / "test_thing.py").write_text(TEST_BODY, encoding="utf-8")

    result = cycle_phases.interpret_generate(state, {"status": "completed", "text": ""})

    assert "phase_outcome" in result
