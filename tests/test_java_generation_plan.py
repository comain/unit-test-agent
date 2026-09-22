from pathlib import Path

from uta.language.java import generation
from uta.language.java import generation_plan


def test_generation_reexports_plan_lifecycle_helpers_from_focused_module():
    helper_names = (
        "_MAX_PLAN_CHARS",
        "_clear_generation_plan",
        "_compress_plan_for_generation",
        "_extract_plan_body_from_artifact",
        "_extract_planned_tests_table",
        "_generation_plan_artifact_classes",
        "_generation_plan_artifact_session_id",
        "_generation_plan_candidate_path",
        "_generation_plan_path",
        "_load_generation_plan_for_resume",
        "_prepare_continue_artifact_for_phase",
        "_recover_plan_text_from_session_artifact",
        "_strip_plan_prose",
        "_write_generation_plan",
        "_write_generation_plan_candidate",
    )

    for helper_name in helper_names:
        assert getattr(generation, helper_name) is getattr(generation_plan, helper_name)


def test_candidate_plan_round_trip_preserves_only_the_plan_body(tmp_path):
    repo = tmp_path / "repo"
    generation_plan._write_generation_plan_candidate(
        str(repo),
        "session-123",
        ["com.example.Sample"],
        "## Planned tests\n\n- testHappyPath",
        ["Need stronger branch reach"],
    )

    assert generation_plan._load_generation_plan_for_resume(
        str(repo), ["com.example.Sample"]
    ) == "## Planned tests\n\n- testHappyPath"


def test_planning_resume_promotes_candidate_to_final_artifact(tmp_path):
    repo = tmp_path / "repo"
    candidate_path = generation_plan._write_generation_plan_candidate(
        str(repo),
        "session-123",
        ["com.example.Sample"],
        "candidate body",
        ["Need stronger branch reach"],
    )

    generation_plan._prepare_continue_artifact_for_phase(
        repo_path=str(repo), phase="plan"
    )

    assert not Path(candidate_path).exists()
    assert generation_plan._load_generation_plan_for_resume(
        str(repo), ["com.example.Sample"]
    ) == "candidate body"


def test_long_generation_plan_compresses_to_planned_test_table():
    plan = "- testWaveTwo: covers alternate branch (wave 2)\n" + ("context " * 500)

    compressed = generation_plan._compress_plan_for_generation(plan)

    assert compressed == (
        "| Test method | Description | Wave |\n"
        "|---|---|---|\n"
        "| `testWaveTwo` | covers alternate branch (wave 2) | W2 |"
    )


def test_long_generation_plan_without_test_entries_keeps_structure():
    plan = "# Strategy\n\n" + ("long prose " * 400) + "\n\n- keep this branch"

    assert generation_plan._compress_plan_for_generation(plan) == (
        "# Strategy\n\n\n- keep this branch"
    )
