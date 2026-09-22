import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from agent_core.harness import (
    AgentSessionRef,
    AvailableSessionDiagnostics,
    DiagnosticsReasonCode,
    ModelUsage,
    TokenUsage,
    UnavailableSessionDiagnostics,
)
from agent_core.harness.diagnostics import build_report
from uta.app.cli import main
from uta.app.session_assessment import (
    assess_sessions,
    assessment_from_report,
    compare_sessions,
)


SCHEMA = """
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL
);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
);
"""


def _build_db(tmp_path: Path) -> Path:
    path = tmp_path / "sessions.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for index, (session, model, total, input_tokens, output_tokens) in enumerate(
        [
            ("ses-current", "provider/current", 26, 15, 5),
            ("ses-base", "provider/base", 10, 8, 1),
        ]
    ):
        payload = {
            "role": "assistant",
            "providerID": "provider",
            "modelID": model,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": total - input_tokens - output_tokens - 5,
                "cache": {"read": 5, "write": 0},
                "total": total,
            },
            "time": {"created": 1000, "completed": 3000},
        }
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (f"m{index}", session, index, index, json.dumps(payload)),
        )
        connection.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (
                f"p{index}",
                f"m{index}",
                session,
                index,
                index,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "time": {"start": 1000, "end": 1500},
                        },
                    }
                ),
            ),
        )
    connection.commit()
    connection.close()
    return path


def test_assessment_projects_neutral_totals_models_and_tools(tmp_path):
    value = assess_sessions(
        ["ses-current", "ses-base"],
        _build_db(tmp_path),
        harness_name="opencode",
    )

    assert value.status == "available"
    assert value.session_ids == ["ses-current", "ses-base"]
    assert value.total_tokens == 36
    assert value.input_tokens == 23
    assert value.tool_call_count == 2
    assert value.top_tools() == [("read", 2)]
    assert set(value.usage_by_model) == {"provider/base", "provider/current"}


def test_unavailable_diagnostics_are_not_projected_as_zero():
    ref = AgentSessionRef("scripted", "missing")
    value = assessment_from_report(
        build_report(
            [
                UnavailableSessionDiagnostics(
                    session=ref,
                    reason_code=DiagnosticsReasonCode.STORAGE_UNAVAILABLE,
                )
            ]
        )
    )

    assert value.status == "unavailable"
    assert value.reason_codes == ["storage_unavailable"]
    assert value.total_tokens is None
    assert value.tool_call_count is None


def test_unregistered_harness_is_explicitly_unsupported():
    value = assess_sessions(["session-1"], harness_name="scripted")

    assert value.status == "unsupported"
    assert value.reason_codes == ["harness_unsupported"]
    assert value.total_tokens is None


def test_mixed_report_withholds_totals_and_comparison():
    current_ref = AgentSessionRef("scripted", "current")
    missing_ref = AgentSessionRef("scripted", "missing")
    current = assessment_from_report(
        build_report(
            [
                AvailableSessionDiagnostics(
                    session=current_ref,
                    usage=TokenUsage(input_tokens=5, total_tokens=5),
                    usage_by_model=(
                        ModelUsage(
                            model="model", usage=TokenUsage(input_tokens=5, total_tokens=5)
                        ),
                    ),
                ),
                UnavailableSessionDiagnostics(
                    session=missing_ref,
                    reason_code=DiagnosticsReasonCode.LOCATOR_NOT_FOUND,
                ),
            ]
        )
    )
    baseline = assessment_from_report(
        build_report(
            [
                AvailableSessionDiagnostics(
                    session=AgentSessionRef("scripted", "baseline"),
                    usage=TokenUsage(total_tokens=3),
                )
            ]
        )
    )

    assert current.status == "mixed"
    assert current.total_tokens is None
    assert compare_sessions(current, baseline)["total_tokens"]["delta"] is None


def test_cli_assess_json_keeps_db_path_compatibility_without_raw_payload(tmp_path):
    secret = "private-model-output-must-not-appear"
    path = _build_db(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("secret", "m0", "ses-current", 9, 9, json.dumps({"type": "text", "text": secret})),
    )
    connection.commit()
    connection.close()

    result = CliRunner().invoke(
        main,
        [
            "assess",
            "--session-id",
            "ses-current",
            "--baseline-session-id",
            "ses-base",
            "--harness",
            "opencode",
            "--db-path",
            str(path),
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["current"]["status"] == "available"
    assert payload["current"]["tokens"]["total"] == 26
    assert "comparison" in payload
    assert secret not in result.output


def test_cli_assess_reports_unsupported_instead_of_zero():
    result = CliRunner().invoke(
        main,
        [
            "assess",
            "--session-id",
            "session-1",
            "--harness",
            "scripted",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["current"]
    assert payload["status"] == "unsupported"
    assert payload["reason_codes"] == ["harness_unsupported"]
    assert payload["tokens"]["total"] is None
