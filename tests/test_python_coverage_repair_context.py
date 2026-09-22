from uta.language.python.phases import render_repair_prompt
from uta.shared.languages import RawTargetSelection, default_registry


def test_coverage_repair_prompt_names_changed_line_denominator_and_misses():
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="chat_robot/models.py")
    )

    prompt = render_repair_prompt(
        {
            "repo_path": "/repo",
            "target": target.as_selection(),
            "generated_test_path": "tests/uta_generated/test_chat_robot_models.py",
            "coverage_gate": 95.0,
            "mutation_gate": 100.0,
            "phase_results": {
                "measure_coverage": {
                    "evidence": {
                        "message": "coverage below gate",
                        "coverage": {
                            "scope": "changed_lines",
                            "covered": 8,
                            "total": 9,
                            "rate": 88.8889,
                            "gate": 95.0,
                            "uncovered_lines": {"chat_robot/models.py": [152]},
                        },
                    }
                }
            },
        },
        "fix_coverage",
    )

    assert "Changed-line coverage: 8/9 (88.89%); gate: 95.00%." in prompt
    assert "Uncovered changed lines: chat_robot/models.py: 152" in prompt
    assert "Whole-file coverage is not the gate" in prompt
