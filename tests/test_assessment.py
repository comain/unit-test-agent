import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from uta.assessment import assess_session, assess_sessions, compare_sessions
from uta.cli import main


def _write_part(conn, session_id, time_created, payload):
    conn.execute(
        "insert into part(session_id, time_created, data) values (?, ?, ?)",
        (session_id, time_created, json.dumps(payload)),
    )


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table part(session_id text, time_created integer, data text)")

    # Current session
    _write_part(conn, "ses-current", 1000, {"type": "text", "text": "prompt"})
    _write_part(conn, "ses-current", 1100, {"type": "step-start"})
    _write_part(conn, "ses-current", 1200, {"type": "tool", "tool": "read"})
    _write_part(conn, "ses-current", 1300, {"type": "text", "text": "hello"})
    _write_part(conn, "ses-current", 1400, {"type": "reasoning", "text": "think"})
    _write_part(
        conn,
        "ses-current",
        1500,
        {
            "type": "step-finish",
            "reason": "tool-calls",
            "tokens": {"input": 10, "output": 2, "reasoning": 1, "total": 17, "cache": {"read": 4, "write": 0}},
        },
    )
    _write_part(conn, "ses-current", 1600, {"type": "step-start"})
    _write_part(conn, "ses-current", 1700, {"type": "tool", "tool": "grep"})
    _write_part(
        conn,
        "ses-current",
        1800,
        {
            "type": "step-finish",
            "reason": "stop",
            "tokens": {"input": 5, "output": 3, "reasoning": 0, "total": 9, "cache": {"read": 1, "write": 0}},
        },
    )

    # Baseline session
    _write_part(conn, "ses-base", 2000, {"type": "step-start"})
    _write_part(conn, "ses-base", 2100, {"type": "tool", "tool": "read"})
    _write_part(
        conn,
        "ses-base",
        2200,
        {
            "type": "step-finish",
            "reason": "stop",
            "tokens": {"input": 8, "output": 1, "reasoning": 0, "total": 10, "cache": {"read": 2, "write": 0}},
        },
    )

    conn.commit()
    conn.close()
    return db_path


def test_assess_session_summarizes_tokens_tools_and_text(tmp_path):
    db_path = _build_db(tmp_path)
    assessment = assess_session("ses-current", db_path)

    assert assessment.part_count == 9
    assert assessment.step_count == 2
    assert assessment.stop_steps == 1
    assert assessment.tool_call_count == 2
    assert assessment.tool_counts["read"] == 1
    assert assessment.tool_counts["grep"] == 1
    assert assessment.text_chars >= len("prompt") + len("hello")
    assert assessment.reasoning_chars == len("think")
    assert assessment.input_tokens == 15
    assert assessment.output_tokens == 5
    assert assessment.reasoning_tokens == 1
    assert assessment.cache_read_tokens == 5
    assert assessment.total_tokens == 26
    assert assessment.top_steps()[0].total_tokens == 17


def test_compare_sessions_calculates_deltas(tmp_path):
    db_path = _build_db(tmp_path)
    current = assess_session("ses-current", db_path)
    baseline = assess_session("ses-base", db_path)
    comparison = compare_sessions(current, baseline)

    assert comparison["non_cache_total"]["current"] == 21
    assert comparison["non_cache_total"]["baseline"] == 9
    assert comparison["tool_call_count"]["delta"] == 1
    assert comparison["output_tokens"]["pct_delta"] == 400.0


def test_assess_sessions_aggregates_multiple_session_ids(tmp_path):
    db_path = _build_db(tmp_path)

    assessment = assess_sessions(["ses-current", "ses-base"], db_path)

    assert assessment.session_ids == ["ses-current", "ses-base"]
    assert assessment.part_count == 12
    assert assessment.step_count == 3
    assert assessment.tool_call_count == 3
    assert assessment.input_tokens == 23
    assert assessment.output_tokens == 6
    assert assessment.cache_read_tokens == 7
    assert assessment.total_tokens == 36
    assert assessment.top_steps()[0].total_tokens == 17


def test_cli_assess_json_output(tmp_path):
    db_path = _build_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "assess",
            "--session-id",
            "ses-current",
            "--baseline-session-id",
            "ses-base",
            "--db-path",
            str(db_path),
            "--json-output",
        ],
    )

    assert result.exit_code == 0
    assert "\"session_id\": \"ses-current\"" in result.output
    assert "\"comparison\"" in result.output


def test_cli_assess_json_output_aggregates_multiple_session_ids(tmp_path):
    db_path = _build_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "assess",
            "--session-id",
            "ses-current",
            "--session-id",
            "ses-base",
            "--db-path",
            str(db_path),
            "--json-output",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["current"]["session_ids"] == ["ses-current", "ses-base"]
    assert payload["current"]["tokens"]["input"] == 23
