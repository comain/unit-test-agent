from uta.testgen.turn_accounting import aggregate_turn_accounting, merge_accounting


def test_testgen_has_no_provider_shaped_session_analyzers():
    from pathlib import Path

    root = Path(__file__).parents[1] / "uta" / "testgen"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert "analyze_session_tokens" not in source
    assert "analyze_session_retrospect" not in source


def test_turn_accounting_preserves_every_normalized_evidence_field():
    turns = [
        {
            "phase": "generate_tests",
            "session_refs": [
                {
                    "harness": "opencode",
                    "locator": "session-generate-first",
                    "scope": "durable",
                },
                {
                    "harness": "opencode",
                    "locator": "session-generate",
                    "scope": "durable",
                },
            ],
            "usage": {
                "total_tokens": {"input": 8, "output": 3, "total": 11},
                "by_model": {"provider/model": {"input": 8, "output": 3}},
            },
            "retrospective": {"hints": ["focus branches"]},
            "diagnostics": {"node_status": "accepted"},
            "patch_count": 2,
            "elapsed_seconds": 4.5,
        },
        {
            "phase": "fix_compile",
            "session_id": "session-fix",
            "usage": {
                "total_tokens": {"input": 5, "output": 2, "total": 7},
                "by_model": {"provider/model": {"input": 5, "output": 2}},
            },
            "retrospective": {"observations": ["missing import"]},
            "diagnostics": {"turn_type": "completed"},
            "patch_count": 1,
            "elapsed_seconds": 2.0,
        },
    ]

    accounting = aggregate_turn_accounting(turns)

    assert accounting["session_ids"] == [
        "session-generate-first",
        "session-generate",
        "session-fix",
    ]
    assert accounting["session_refs"] == [
        {
            "harness": "opencode",
            "locator": "session-generate-first",
            "scope": "durable",
        },
        {
            "harness": "opencode",
            "locator": "session-generate",
            "scope": "durable",
        },
        {
            "harness": "legacy",
            "locator": "session-fix",
            "scope": "durable",
        },
    ]
    assert accounting["session_token_usage"]["total_tokens"] == {
        "input": 13,
        "output": 5,
        "total": 18,
    }
    assert accounting["phase_token_usage"]["generate_tests"]["total"] == 11
    assert accounting["phase_token_usage"]["fix_compile"]["total"] == 7
    assert accounting["session_patch_count"] == 3
    assert accounting["agent_phase_timings"] == {
        "generate_tests": 4.5,
        "fix_compile": 2.0,
    }
    assert [item["session_id"] for item in accounting["session_retrospect"]["turns"]] == [
        "session-generate",
        "session-fix",
    ]
    assert accounting["session_diagnostics"]["turns"][1]["value"] == {
        "turn_type": "completed"
    }


def test_outer_accounting_accumulates_batches_without_replacing_prior_values():
    merged = merge_accounting(
        {
            "session_ids": ["session-a"],
            "session_refs": [
                {"harness": "scripted", "locator": "session-a", "scope": "durable"}
            ],
            "session_token_usage": {"total_tokens": {"input": 8, "total": 8}},
            "phase_token_usage": {"generate_tests": {"input": 8}},
            "phase_timings": {"generate_tests": 4.0},
            "session_retrospect": {"turns": [{"phase": "generate_tests"}]},
            "session_diagnostics": {"turns": [{"phase": "generate_tests"}]},
            "session_patch_count": 2,
        },
        {
            "session_ids": ["session-a", "session-b"],
            "session_refs": [
                {"harness": "scripted", "locator": "session-a", "scope": "durable"},
                {"harness": "scripted", "locator": "session-b", "scope": "durable"},
            ],
            "session_token_usage": {"total_tokens": {"input": 5, "total": 5}},
            "phase_token_usage": {"fix_compile": {"input": 5}},
            "phase_timings": {"fix_compile": 2.0},
            "session_retrospect": {"turns": [{"phase": "fix_compile"}]},
            "session_diagnostics": {"turns": [{"phase": "fix_compile"}]},
            "session_patch_count": 1,
        },
    )

    assert merged["session_token_usage"]["total_tokens"] == {
        "input": 13,
        "total": 13,
    }
    assert set(merged["phase_token_usage"]) == {"generate_tests", "fix_compile"}
    assert merged["phase_timings"] == {"generate_tests": 4.0, "fix_compile": 2.0}
    assert merged["session_patch_count"] == 3
    assert merged["session_ids"] == ["session-a", "session-b"]
    assert [ref["locator"] for ref in merged["session_refs"]] == [
        "session-a",
        "session-b",
    ]
    assert len(merged["session_retrospect"]["turns"]) == 2
