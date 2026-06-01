"""Phase 3 gate tests for the stream-based OpenCodeClient."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from uta.opencode.client import OpenCodeClient
from uta.opencode.process import TurnResult


def _make_turn_result(
    type_="completed",
    result="",
    session_id="ses_abc",
    tokens=None,
    error=None,
    patch_count=0,
    fallback_eligible=False,
    fallback_reason=None,
):
    return TurnResult(
        type=type_,
        result=result,
        session_id=session_id,
        tokens=tokens or {"input": 100, "output": 20, "total": 120,
                          "cache": {"read": 0, "write": 0}},
        error=error,
        patch_count=patch_count,
        fallback_eligible=fallback_eligible,
        fallback_reason=fallback_reason,
    )


@pytest.fixture
def client(tmp_path):
    return OpenCodeClient(repo_path=str(tmp_path))


# --- create_session / delete_session ---

def test_create_session_returns_truthy_id(client):
    sid = client.create_session(model_id="cursor/claude-4.5-sonnet")
    assert sid
    assert isinstance(sid, str)


def test_create_session_unique_ids(client):
    sid1 = client.create_session()
    sid2 = client.create_session()
    assert sid1 != sid2


def test_delete_session_removes_state(client):
    sid = client.create_session()
    client.delete_session(sid)
    # After deletion, poll_completion should return error (unknown session)
    event = client.poll_completion(sid)
    assert event["type"] == "error"


# --- send_message + poll_completion ---

def test_run_turn_returns_completed(client):
    sid = client.create_session(model_id="cursor/claude-4.5-sonnet")
    client.send_message(sid, "Generate tests")

    with patch.object(client._process, "run_turn", return_value=_make_turn_result(
        type_="completed", result="Here are your tests.", session_id="ses_abc"
    )):
        event = client.poll_completion(sid)

    assert event["type"] == "completed"
    assert event["result"] == "Here are your tests."


def test_session_id_persisted_across_turns(client):
    sid = client.create_session()
    client.send_message(sid, "First message")

    captured_args = {}

    def fake_run_turn(message, *, session_id, model_id, repo_path, timeout, on_update):
        captured_args["first_session_id"] = session_id
        return _make_turn_result(session_id="ses_opencode_001")

    with patch.object(client._process, "run_turn", side_effect=fake_run_turn):
        client.poll_completion(sid)

    # First turn: no prior opencode session_id
    assert captured_args["first_session_id"] is None

    # Second turn: should use the opencode session_id from first turn
    client.send_message(sid, "Second message")

    def fake_run_turn2(message, *, session_id, model_id, repo_path, timeout, on_update):
        captured_args["second_session_id"] = session_id
        return _make_turn_result(session_id="ses_opencode_001")

    with patch.object(client._process, "run_turn", side_effect=fake_run_turn2):
        client.poll_completion(sid)

    assert captured_args["second_session_id"] == "ses_opencode_001"


def test_token_accumulation_across_turns(client):
    sid = client.create_session()

    turn1_tokens = {"input": 100, "output": 50, "total": 150, "cache": {"read": 0, "write": 0}}
    turn2_tokens = {"input": 200, "output": 80, "total": 280, "cache": {"read": 0, "write": 0}}

    client.send_message(sid, "Turn 1")
    with patch.object(client._process, "run_turn",
                      return_value=_make_turn_result(tokens=turn1_tokens, session_id="ses_x")):
        client.poll_completion(sid)

    client.send_message(sid, "Turn 2")
    with patch.object(client._process, "run_turn",
                      return_value=_make_turn_result(tokens=turn2_tokens, session_id="ses_x")):
        client.poll_completion(sid)

    usage = client.analyze_session_tokens(sid)
    assert usage["total_tokens"]["input"] == 300
    assert usage["total_tokens"]["output"] == 130
    assert usage["total_tokens"]["total"] == 430


def test_patch_count_accumulates_from_run_turn(client):
    sid = client.create_session()
    client.send_message(sid, "Patch test")

    with patch.object(client._process, "run_turn",
                      return_value=_make_turn_result(patch_count=1, session_id="ses_x")):
        client.poll_completion(sid)

    assert client.get_session_patch_count(sid) == 1


def test_rate_limited_result_propagated(client):
    sid = client.create_session()
    client.send_message(sid, "Generate")
    error_obj = {"name": "RateLimitError", "data": {"message": "Too many requests", "statusCode": 429}}
    with patch.object(client._process, "run_turn",
                      return_value=_make_turn_result(type_="rate_limited", error=error_obj, session_id="ses_x")):
        event = client.poll_completion(sid)

    assert event["type"] == "rate_limited"
    assert "rate_limit" in event


def test_fallback_eligible_error_propagates_metadata(client):
    sid = client.create_session(model_id="token-pool/missing-model")
    client.send_message(sid, "Generate")
    error_obj = {"name": "ModelError", "data": {"message": "model is not available"}}
    with patch.object(
        client._process,
        "run_turn",
        return_value=_make_turn_result(
            type_="error",
            error=error_obj,
            session_id="ses_x",
            fallback_eligible=True,
            fallback_reason="model_unavailable",
        ),
    ):
        event = client.poll_completion(sid)

    assert event["type"] == "error"
    assert event["fallback_eligible"] is True
    assert event["fallback_reason"] == "model_unavailable"
    assert event["provider_id"] == "token-pool"
    assert event["model_id"] == "token-pool/missing-model"


def test_timeout_result_propagated(client):
    sid = client.create_session()
    client.send_message(sid, "Generate")
    with patch.object(client._process, "run_turn",
                      return_value=_make_turn_result(type_="timeout", session_id="ses_x")):
        event = client.poll_completion(sid)

    assert event["type"] == "timeout"


def test_send_message_split_concatenates(client):
    sid = client.create_session()
    client.send_message_split(sid, "stable-prefix\n", "volatile-tail")

    state = client._sessions[sid]
    assert state.pending_message == "stable-prefix\nvolatile-tail"


def test_model_id_from_create_session_used_in_run_turn(client):
    sid = client.create_session(model_id="cursor/claude-4.5-sonnet")
    client.send_message(sid, "hello")

    captured = {}

    def fake_run_turn(message, *, session_id, model_id, repo_path, timeout, on_update):
        captured["model_id"] = model_id
        return _make_turn_result(session_id="ses_x")

    with patch.object(client._process, "run_turn", side_effect=fake_run_turn):
        client.poll_completion(sid)

    assert captured["model_id"] == "cursor/claude-4.5-sonnet"


def test_configured_variant_forwarded_to_run_turn(monkeypatch, client):
    monkeypatch.setattr("uta.opencode.client.settings.opencode_variant", "none")
    sid = client.create_session(model_id="openai/gpt-5.5")
    client.send_message(sid, "hello")

    captured = {}

    def fake_run_turn(message, **kwargs):
        captured["variant"] = kwargs.get("variant")
        return _make_turn_result(session_id="ses_x")

    with patch.object(client._process, "run_turn", side_effect=fake_run_turn):
        client.poll_completion(sid)

    assert captured["variant"] == "none"


def test_on_update_forwarded_to_run_turn(client):
    sid = client.create_session()
    client.send_message(sid, "hi")
    updates = []

    def fake_run_turn(message, *, session_id, model_id, repo_path, timeout, on_update):
        if on_update:
            on_update("text: hello from stream")
        return _make_turn_result(session_id="ses_x")

    with patch.object(client._process, "run_turn", side_effect=fake_run_turn):
        client.poll_completion(sid, on_update=updates.append)

    assert any("hello from stream" in u for u in updates)


# --- detect_rate_limit_issue ---

def test_detect_rate_limit_issue_returns_none(client):
    assert client.detect_rate_limit_issue("any-session") is None


# --- no pending message ---

def test_poll_completion_no_pending_message_returns_error(client):
    sid = client.create_session()
    event = client.poll_completion(sid)
    assert event["type"] == "error"
