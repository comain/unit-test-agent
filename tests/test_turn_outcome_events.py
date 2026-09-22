"""A failed turn has to say what failed.

The public progress stream renders every provider error as "Agent needs
attention" -- agent-core projects it from a fixed table on purpose, because the
provider's own message may quote model text, a command or a credential. That
left production with two ERROR rows on a `fix_mutation` phase, no session, no
log path and no reason. These events are UTA's own account instead.
"""

from __future__ import annotations

from uta.testgen.turn_events import record_turn_outcome, turn_outcome_event


def test_a_clean_turn_says_nothing():
    assert turn_outcome_event("fix_mutation", {"status": "completed"}) is None


def test_a_failed_turn_names_the_reason_and_the_evidence():
    event = turn_outcome_event(
        "fix_mutation",
        {
            "status": "failed",
            "diagnostics": {
                "turn_type": "error",
                "node_status": "error",
                "fallback_eligible": True,
                "fallback_reason": "provider_auth_failed",
                "model_attempts": ["token-pool/gpt-5.5"],
            },
            "raw_log_path": "/opt/app/uta-data/turns/abc.log",
            "session_refs": [
                {"harness": "opencode", "locator": "ses_123", "scope": "phase"}
            ],
            "elapsed_seconds": 3.2,
        },
    )

    assert event["severity"] == "ERROR"
    assert event["message"] == "Turn failed (provider_auth_failed)"
    assert event["stage"] == "fix_mutation"
    assert event["payload"]["fallback_reason"] == "provider_auth_failed"
    assert event["payload"]["rawLogPath"] == "/opt/app/uta-data/turns/abc.log"
    assert event["payload"]["sessionRefs"] == [
        {"harness": "opencode", "locator": "ses_123", "scope": "phase"}
    ]


def test_a_turn_that_only_got_there_by_falling_back_is_worth_a_warning():
    event = turn_outcome_event(
        "generate_tests",
        {"status": "completed", "recovered": True,
         "diagnostics": {"fallback_reason": "stream_reset"}},
    )

    assert event["severity"] == "WARNING"
    assert event["message"] == "Turn completed after recovery (stream_reset)"


def test_a_successful_provider_fallback_keeps_sanitized_http_evidence():
    event = turn_outcome_event(
        "fix_tests",
        {
            "status": "completed",
            "diagnostics": {
                "turn_type": "completed",
                "model_attempts": [
                    {
                        "model": "token-pool/gpt-6-astra",
                        "outcome": "error",
                        "fallback_reason": "provider_auth_failed",
                        "http_status": 401,
                        "error_code": "invalid_api_key",
                        "error_detail": "Missing API key [redacted]",
                    },
                    {
                        "model": "openai/gpt-6-astra",
                        "outcome": "completed",
                        "fallback_reason": "",
                    },
                ],
            },
        },
    )

    assert event is not None
    assert event["severity"] == "WARNING"
    assert event["message"] == "Turn completed after recovery (provider_auth_failed)"
    assert event["payload"]["model_attempts"][0]["http_status"] == 401
    assert event["payload"]["model_attempts"][0]["error_code"] == "invalid_api_key"


def test_provider_text_never_rides_along():
    """Only the diagnostics agent-core already vetted may be copied."""
    event = turn_outcome_event(
        "fix_tests",
        {
            "status": "failed",
            "text": "sk-secret-key in the model's reply",
            "diagnostics": {
                "turn_type": "error",
                "provider_message": "Missing API key sk-secret",
            },
        },
    )

    serialized = repr(event)
    assert "sk-secret" not in serialized
    assert "provider_message" not in event["payload"]


def test_the_event_reaches_the_task_log():
    written = []

    class _Ports:
        def add_event(self, task_id, event_type, payload):
            written.append((task_id, event_type, payload))

    import uta.testgen.turn_events as turn_events

    original = turn_events.task_ports_from_state
    turn_events.task_ports_from_state = lambda state: _Ports()
    try:
        record_turn_outcome(
            {"task_id": 151},
            phase="fix_mutation",
            turn={"status": "failed", "diagnostics": {"turn_type": "error"}},
        )
        record_turn_outcome({"task_id": 151}, phase="fix_mutation", turn={"status": "completed"})
    finally:
        turn_events.task_ports_from_state = original

    assert len(written) == 1
    task_id, event_type, payload = written[0]
    assert (task_id, event_type) == ("151", "turn_outcome")
    assert payload["message"] == "Turn failed (error)"


def test_a_turn_without_a_task_is_not_reported():
    record_turn_outcome({}, phase="fix_mutation", turn={"status": "failed"})
