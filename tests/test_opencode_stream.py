"""Phase 2 gate tests for OpenCodeStreamParser."""

import pytest
from uta.opencode.stream import OpenCodeStreamParser


@pytest.fixture
def parser():
    return OpenCodeStreamParser()


def _step_start(session_id="ses_abc"):
    return {"type": "step_start", "sessionID": session_id, "part": {"type": "step-start"}}


def _text_event(text, session_id="ses_abc"):
    return {"type": "text", "sessionID": session_id, "part": {"type": "text", "text": text}}


def _tool_event(tool="bash", status="completed", title=""):
    return {
        "type": "tool_use",
        "sessionID": "ses_abc",
        "part": {
            "tool": tool,
            "state": {"status": status, "title": title},
        },
    }


def _step_finish(reason="stop", tokens=None):
    return {
        "type": "step_finish",
        "sessionID": "ses_abc",
        "part": {
            "reason": reason,
            "tokens": tokens or {"input": 100, "output": 50, "reasoning": 0, "cache": {"write": 0, "read": 0}, "total": 150},
        },
    }


def _error_event(message="something went wrong", status_code=None):
    data = {"message": message}
    if status_code is not None:
        data["statusCode"] = status_code
    return {"type": "error", "sessionID": "ses_abc", "error": {"name": "UnknownError", "data": data}}


# --- parse_line ---

def test_parser_handles_blank_line(parser):
    assert parser.parse_line("") is None
    assert parser.parse_line("   ") is None


def test_parser_handles_malformed_json(parser):
    assert parser.parse_line("not json at all") is None
    assert parser.parse_line("{broken") is None
    assert parser.parse_line("plugin initialized: cursor-auth") is None


def test_parser_parses_valid_json(parser):
    line = '{"type":"text","sessionID":"ses_abc","part":{"text":"hello"}}'
    result = parser.parse_line(line)
    assert result is not None
    assert result["type"] == "text"
    assert result["part"]["text"] == "hello"


# --- extract_text ---

def test_parser_text_extraction_single(parser):
    events = [_text_event("Hello world")]
    assert parser.extract_text(events) == "Hello world"


def test_parser_text_extraction_multiple(parser):
    events = [_text_event("line one"), _text_event("line two")]
    result = parser.extract_text(events)
    assert "line one" in result
    assert "line two" in result


def test_parser_text_extraction_ignores_non_text(parser):
    events = [_step_start(), _text_event("actual text"), _step_finish()]
    assert parser.extract_text(events) == "actual text"


def test_parser_text_extraction_empty(parser):
    assert parser.extract_text([]) == ""
    assert parser.extract_text([_step_finish()]) == ""


# --- extract_tokens ---

def test_parser_step_finish_stop_extracts_tokens(parser):
    tokens = {"input": 1000, "output": 200, "reasoning": 0, "cache": {"write": 500, "read": 300}, "total": 1200}
    events = [_step_start(), _text_event("done"), _step_finish(reason="stop", tokens=tokens)]
    result = parser.extract_tokens(events)
    assert result["input"] == 1000
    assert result["output"] == 200
    assert result["total"] == 1200


def test_parser_tokens_aggregate_all_step_finishes(parser):
    events = [
        _step_finish(reason="tool-calls", tokens={"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 0}, "total": 16}),
        _step_finish(reason="stop", tokens={"input": 100, "output": 50, "reasoning": 4, "cache": {"read": 5, "write": 0}, "total": 159}),
    ]
    result = parser.extract_tokens(events)
    assert result["input"] == 110
    assert result["output"] == 52
    assert result["reasoning"] == 5
    assert result["cache"]["read"] == 8
    assert result["total"] == 175


def test_parser_tokens_empty_when_no_stop(parser):
    events = [_step_start(), _text_event("hi")]
    assert parser.extract_tokens(events) == {}


# --- count_patches ---

def test_parser_counts_patch_part(parser):
    events = [{"type": "patch", "sessionID": "ses_abc", "part": {"type": "patch", "files": ["A.java"]}}]
    assert parser.count_patches(events) == 1


def test_parser_counts_completed_apply_patch_tool(parser):
    events = [
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "apply_patch",
                "state": {"status": "completed", "output": "Success. Updated the following files:\nM A.java"},
            },
        }
    ]
    assert parser.count_patches(events) == 1


def test_parser_ignores_failed_apply_patch_tool(parser):
    events = [
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "apply_patch",
                "state": {"status": "completed", "output": "Failed to apply patch"},
            },
        }
    ]
    assert parser.count_patches(events) == 0


# --- detect_completion ---

def test_parser_detect_completion_stop(parser):
    events = [_step_start(), _text_event("done"), _step_finish(reason="stop")]
    assert parser.detect_completion(events) == "stop"


def test_parser_detect_completion_tool_calls_not_stop(parser):
    events = [_step_start(), _step_finish(reason="tool-calls")]
    assert parser.detect_completion(events) is None


def test_parser_detect_completion_empty(parser):
    assert parser.detect_completion([]) is None


# --- detect_rate_limit ---

def test_parser_rate_limit_by_status_code(parser):
    ev = _error_event("rate limit exceeded", status_code=429)
    assert parser.detect_rate_limit(ev) is True


def test_parser_rate_limit_by_message_quota(parser):
    ev = _error_event("Free quota exhausted for this model")
    assert parser.detect_rate_limit(ev) is True


def test_parser_rate_limit_by_message_too_many_requests(parser):
    ev = _error_event("Too many requests, please slow down")
    assert parser.detect_rate_limit(ev) is True


def test_parser_rate_limit_by_message_usage_limit(parser):
    ev = _error_event("usage limit has been reached")
    assert parser.detect_rate_limit(ev) is True


def test_parser_rate_limit_by_raw_text(parser):
    text = 'provider returned 429: {"error":{"type":"usage_limit_reached","message":"The usage limit has been reached"}}'
    assert parser.detect_rate_limit_text(text) is True


def test_parser_rate_limit_false_for_normal_error(parser):
    ev = _error_event("Model not found: tencent/glm-5")
    assert parser.detect_rate_limit(ev) is False


def test_parser_rate_limit_false_for_non_error_event(parser):
    assert parser.detect_rate_limit(_text_event("hello")) is False


# --- progress_line ---

def test_progress_line_text(parser):
    line = parser.progress_line(_text_event("Hello there!"))
    assert line is not None
    assert "Hello there!" in line


def test_progress_line_tool_use(parser):
    line = parser.progress_line(_tool_event(tool="bash", status="completed", title="run tests"))
    assert line is not None
    assert "bash" in line
    assert "completed" in line
    assert "run tests" in line


def test_progress_line_step_start(parser):
    line = parser.progress_line(_step_start())
    assert line is not None
    assert "started" in line


def test_progress_line_step_finish(parser):
    line = parser.progress_line(_step_finish(reason="stop"))
    assert line is not None
    assert "stop" in line


def test_progress_line_none_for_unknown_type(parser):
    assert parser.progress_line({"type": "unknown_event"}) is None
