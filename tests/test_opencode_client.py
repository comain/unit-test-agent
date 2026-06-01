import pytest
import respx
import httpx
from uta.opencode.client import OpenCodeAuthClient as OpenCodeClient


def test_client_base_url_uses_configured_ipv6_host(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_host", "::1")
    client = OpenCodeClient(port=4096)
    assert client.base_url == "http://[::1]:4096"


@respx.mock
def test_create_session():
    client = OpenCodeClient(port=4096)
    respx.post("http://127.0.0.1:4096/session").mock(
        return_value=httpx.Response(200, json={"id": "session-123"})
    )

    session_id = client.create_session()
    assert session_id == "session-123"


@respx.mock
def test_send_message():
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session/session-123/message").mock(
        return_value=httpx.Response(200, json={
            "info": {"role": "assistant", "id": "msg-456"}
        })
    )

    resp = client.send_message("session-123", "Hello")
    assert resp == {}
    assert route.call_count == 1


@respx.mock
def test_send_message_split_concatenates_into_single_payload():
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session/session-123/message").mock(
        return_value=httpx.Response(200, json={"info": {"role": "assistant", "id": "msg-456"}})
    )

    resp = client.send_message_split("session-123", "stable-prefix\n", "volatile-tail")

    assert resp == {}
    assert route.call_count == 1
    assert b'"stable-prefix\\nvolatile-tail"' in route.calls[0].request.content


@respx.mock
def test_create_session_uses_configured_provider_for_plain_model(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_provider", "tencent")
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session").mock(
        return_value=httpx.Response(200, json={"id": "session-123"})
    )

    session_id = client.create_session(model_id="glm-5")
    assert session_id == "session-123"
    assert route.calls[0].request.content
    assert b'"providerID":"tencent"' in route.calls[0].request.content
    assert b'"modelID":"glm-5"' in route.calls[0].request.content


@respx.mock
def test_create_session_includes_permission_payload():
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session").mock(
        return_value=httpx.Response(200, json={"id": "session-123"})
    )

    session_id = client.create_session(
        model_id="openai/gpt-5.4",
        permission=[{"permission": "todowrite", "action": "deny", "pattern": "*"}],
    )

    assert session_id == "session-123"
    assert b'"permission":[{"permission":"todowrite","action":"deny","pattern":"*"}]' in route.calls[0].request.content


@respx.mock
def test_send_message_uses_configured_provider_for_plain_model(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_provider", "tencent")
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session/session-123/message").mock(
        return_value=httpx.Response(200, json={"info": {"role": "assistant", "id": "msg-456"}})
    )

    client.send_message("session-123", "Hello", model_id="glm-5")
    assert route.call_count == 1
    assert b'"providerID":"tencent"' in route.calls[0].request.content
    assert b'"modelID":"glm-5"' in route.calls[0].request.content


@respx.mock
def test_send_message_includes_configured_variant(monkeypatch):
    monkeypatch.setattr("uta.opencode.client.settings.opencode_variant", "none")
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session/session-123/message").mock(
        return_value=httpx.Response(200, json={"info": {"role": "assistant", "id": "msg-456"}})
    )

    client.send_message("session-123", "Hello", model_id="openai/gpt-5.5")
    assert route.call_count == 1
    assert b'"variant":"none"' in route.calls[0].request.content


def test_poll_completion_recovers_after_temporary_network_error():
    client = OpenCodeClient(port=4096)
    calls = {"count": 0}

    def fake_get_messages(_sid):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("temporary network issue")
        return [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2, "completed": 3}}, "parts": [
                {"type": "text", "text": "done!"},
                {"type": "step-finish", "reason": "stop"},
            ]},
        ]

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "get_messages", fake_get_messages)
        mp.setattr(client, "detect_rate_limit_issue", lambda sid: None)
        mp.setattr(time, "sleep", lambda x: None)

        event = client.poll_completion("session-123", timeout=3)
        assert event["type"] == "completed"
        assert event["result"] == "done!"


def test_poll_completion_returns_stalled_after_recovery_when_no_progress():
    client = OpenCodeClient(port=4096)
    calls = {"count": 0}
    now = {"value": 0.0}

    def fake_get_messages(_sid):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("temporary network issue")
        return [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": []},
        ]

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "get_messages", fake_get_messages)
        mp.setattr(client, "detect_rate_limit_issue", lambda sid: None)
        mp.setattr(time, "time", lambda: now["value"])
        mp.setattr(time, "sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

        event = client.poll_completion("session-123", timeout=10, stalled_after_recovery_seconds=3)
        assert event["type"] == "stalled_after_recovery"
        assert "no session progress" in event["reason"]


def test_poll_completion_returns_stalled_no_progress_without_transport_error():
    client = OpenCodeClient(port=4096)
    now = {"value": 0.0}

    def fake_get_messages(_sid):
        return [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": []},
        ]

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "get_messages", fake_get_messages)
        mp.setattr(client, "detect_rate_limit_issue", lambda sid: None)
        mp.setattr(time, "time", lambda: now["value"])
        mp.setattr(time, "sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

        event = client.poll_completion("session-123", timeout=10, stalled_no_progress_seconds=3)
        assert event["type"] == "stalled_no_progress"
        assert "no session progress" in event["reason"]


def test_poll_completion_ignores_repeated_no_id_parts_for_progress():
    client = OpenCodeClient(port=4096)
    now = {"value": 0.0}

    repeated_messages = [
        {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
        {
            "info": {"role": "assistant", "time": {"created": 2}},
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": "Still working"},
            ],
        },
    ]

    def fake_get_messages(_sid):
        return repeated_messages

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "get_messages", fake_get_messages)
        mp.setattr(client, "detect_rate_limit_issue", lambda sid: None)
        mp.setattr(time, "time", lambda: now["value"])
        mp.setattr(time, "sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

        event = client.poll_completion("session-123", timeout=10, stalled_no_progress_seconds=3)
        assert event["type"] == "stalled_no_progress"
        assert "no session progress" in event["reason"]


def test_detect_rate_limit_issue_from_opencode_log(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-15T091846.log"
    log_file.write_text(
        'ERROR 2026-04-15T09:18:49 service=llm providerID=openai modelID=gpt-5.4 sessionID=ses_abc error='
        '{"statusCode":429,"data":{"error":{"message":"The usage limit has been reached","type":"usage_limit_reached"}},'
        '"responseHeaders":{"x-codex-primary-reset-after-seconds":"7798","x-codex-primary-reset-at":"1776252465"},'
        '"responseBody":"{\\"error\\":{\\"type\\":\\"usage_limit_reached\\",\\"message\\":\\"The usage limit has been reached\\",\\"resets_in_seconds\\":7797,\\"resets_at\\":1776252465}}"}\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_abc")

    assert detected is not None
    assert detected["status_code"] == 429
    assert detected["raw_type"] == "usage_limit_reached"
    assert detected["retry_after_seconds"] == 7798
    assert detected["provider_id"] == "openai"
    assert detected["model_id"] == "gpt-5.4"


def test_detect_rate_limit_issue_from_openrouter_insufficient_credits(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-16T082316.log"
    log_file.write_text(
        'ERROR 2026-04-16T08:23:17 service=llm providerID=openrouter modelID=z-ai/glm-5.1 sessionID=ses_xyz error='
        '{"statusCode":402,"data":{"error":{"message":"This request requires more credits, or fewer max_tokens","type":null}},'
        '"responseBody":"{\\"error\\":{\\"message\\":\\"This request requires more credits, or fewer max_tokens\\",\\"code\\":402}}"}\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_xyz")

    assert detected is not None
    assert detected["status_code"] == 402
    assert detected["raw_type"] == "insufficient_credits"
    assert "requires more credits" in detected["message"]
    assert detected["provider_id"] == "openrouter"
    assert detected["model_id"] == "z-ai/glm-5.1"


def test_detect_rate_limit_issue_from_tencent_free_quota_exhausted(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-21T103308.log"
    log_file.write_text(
        'ERROR 2026-04-21T10:34:10 service=llm providerID=tencent modelID=glm-5 sessionID=ses_tencent error='
        '{"statusCode":402,"data":{"error":{"message":"endpoint is inactive: FREE_QUOTA_EXHAUSTED","code":"401008","type":"gateway_error"}},'
        '"responseBody":"{\\"error\\":{\\"message\\":\\"endpoint is inactive: FREE_QUOTA_EXHAUSTED\\",\\"code\\":\\"401008\\",\\"type\\":\\"gateway_error\\"}}"} stream error\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_tencent")

    assert detected is not None
    assert detected["status_code"] == 402
    assert detected["raw_type"] == "gateway_error"
    assert "free_quota_exhausted" in detected["message"].lower()
    assert detected["provider_id"] == "tencent"
    assert detected["model_id"] == "glm-5"


def test_detect_rate_limit_issue_from_openrouter_upstream_429_with_trailing_text(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-16T094335.log"
    log_file.write_text(
        'ERROR 2026-04-16T09:43:35 service=llm providerID=openrouter modelID=z-ai/glm-4.5-air:free sessionID=ses_glm error='
        '{"statusCode":429,"data":{"error":{"code":429,"message":"Provider returned error","type":null,"param":null,"metadata":{"raw":"z-ai/glm-4.5-air:free is temporarily rate-limited upstream. Please retry shortly","provider_name":"Z.AI","is_byok":false}},"user_id":"user_123"}} stream error\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_glm")

    assert detected is not None
    assert detected["status_code"] == 429
    assert detected["raw_type"] == "rate_limit"
    assert "temporarily rate-limited upstream" in detected["message"]
    assert detected["provider_id"] == "openrouter"
    assert detected["model_id"] == "z-ai/glm-4.5-air:free"
    assert detected["provider_metadata"]["provider_name"] == "Z.AI"


def test_detect_rate_limit_issue_reads_uta_persisted_debug_logs(tmp_path):
    client = OpenCodeClient(port=4096)
    debug_log_dir = tmp_path / "uta-run-logs"
    debug_log_dir.mkdir()
    log_file = debug_log_dir / "demo_opencode_20260416_171030.log"
    log_file.write_text(
        'ERROR 2026-04-16T09:10:39 service=llm providerID=openai modelID=gpt-5.4 sessionID=ses_debug error='
        '{"statusCode":429,"data":{"error":{"message":"The usage limit has been reached","type":"usage_limit_reached"}},'
        '"responseHeaders":{"x-codex-primary-reset-after-seconds":"14006","x-codex-primary-reset-at":"1776344676"}}\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_opencode_log_dir", staticmethod(lambda: tmp_path / "missing-opencode-log"))
        mp.setattr(OpenCodeClient, "_uta_debug_log_dir", classmethod(lambda cls: debug_log_dir))
        detected = client.detect_rate_limit_issue("ses_debug")

    assert detected is not None
    assert detected["status_code"] == 429
    assert detected["provider_id"] == "openai"
    assert detected["model_id"] == "gpt-5.4"
    assert detected["retry_after_seconds"] == 14006
    assert detected["log_path"] == str(log_file)


def test_detect_rate_limit_issue_from_gemini_resource_exhausted(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-17T020604.log"
    log_file.write_text(
        'ERROR 2026-04-17T03:02:41 service=llm providerID=google modelID=gemini-3.1-pro-preview sessionID=ses_gem error='
        '{"statusCode":429,"responseBody":"{\\"error\\":{\\"code\\":429,\\"message\\":\\"Resource has been exhausted (e.g. check quota).\\",\\"status\\":\\"RESOURCE_EXHAUSTED\\"}}"} stream error\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_gem")

    assert detected is not None
    assert detected["status_code"] == 429
    assert detected["raw_type"] == "RESOURCE_EXHAUSTED"
    assert "resource has been exhausted" in detected["message"].lower()
    assert detected["provider_id"] == "google"
    assert detected["model_id"] == "gemini-3.1-pro-preview"


def test_detect_rate_limit_issue_from_multiline_gemini_resource_exhausted(tmp_path):
    client = OpenCodeClient(port=4096)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    log_file = log_dir / "2026-04-17T020604.log"
    log_file.write_text(
        'ERROR 2026-04-17T03:02:41 service=llm providerID=google modelID=gemini-3.1-pro-preview sessionID=ses_gem_multi error={"statusCode":429,"responseBody":"{\\n'
        '  \\"error\\": {\\n'
        '    \\"code\\": 429,\\n'
        '    \\"message\\": \\"Resource has been exhausted (e.g. check quota).\\",\\n'
        '    \\"status\\": \\"RESOURCE_EXHAUSTED\\"\\n'
        '  }\\n'
        '}\\n","isRetryable":true,"data":{"error":{"code":429,"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}} stream error\n',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenCodeClient, "_recent_log_files", classmethod(lambda cls, limit=5: [log_file]))
        detected = client.detect_rate_limit_issue("ses_gem_multi")

    assert detected is not None
    assert detected["status_code"] == 429
    assert detected["raw_type"] == "RESOURCE_EXHAUSTED"
    assert "resource has been exhausted" in detected["message"].lower()


def test_poll_completion_returns_rate_limited_event():
    client = OpenCodeClient(port=4096)
    now = {"value": 0.0}

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "get_messages", lambda sid: [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": []},
        ])
        mp.setattr(client, "detect_rate_limit_issue", lambda sid: {
            "session_id": sid,
            "status_code": 429,
            "raw_type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 120,
            "provider_id": "openai",
            "model_id": "gpt-5.4",
        })
        mp.setattr(time, "time", lambda: now["value"])
        mp.setattr(time, "sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

        event = client.poll_completion(
            "session-123",
            timeout=30,
            rate_limit_check_interval_seconds=0,
        )

    assert event["type"] == "rate_limited"
    assert event["rate_limit"]["retry_after_seconds"] == 120


def test_poll_completion_checks_rate_limits_quickly_by_default():
    client = OpenCodeClient(port=4096)
    now = {"value": 0.0}
    seen = {"calls": 0}

    with pytest.MonkeyPatch.context() as mp:
        import time

        mp.setattr(client, "get_messages", lambda sid: [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": []},
        ])

        def fake_detect(_sid):
            seen["calls"] += 1
            if seen["calls"] >= 2:
                return {
                    "session_id": _sid,
                    "status_code": 429,
                    "raw_type": "usage_limit_reached",
                    "message": "The usage limit has been reached",
                    "retry_after_seconds": 90,
                    "provider_id": "openai",
                    "model_id": "gpt-5.4",
                }
            return None

        mp.setattr(client, "detect_rate_limit_issue", fake_detect)
        mp.setattr(time, "time", lambda: now["value"])
        mp.setattr(time, "sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

        event = client.poll_completion("session-123", timeout=10)

    assert event["type"] == "rate_limited"
    assert seen["calls"] >= 2
    assert now["value"] <= 6.0


@respx.mock
def test_request_retries_transient_http_status():
    client = OpenCodeClient(port=4096)
    route = respx.get("http://127.0.0.1:4096/provider").mock(
        side_effect=[
            httpx.Response(503, json={"error": "busy"}),
            httpx.Response(200, json={"connected": ["openai"]}),
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        payload = client.list_providers()

    assert payload["connected"] == ["openai"]
    assert route.call_count == 2


@respx.mock
def test_poll_completion():
    client = OpenCodeClient(port=4096)

    # First poll: assistant message not yet completed
    # Second poll: assistant message completed with time.completed set
    route = respx.get("http://127.0.0.1:4096/session/session-123/message")
    route.side_effect = [
        httpx.Response(200, json=[
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [{"type": "text", "text": "working..."}]},
        ]),
        httpx.Response(200, json=[
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2, "completed": 3}}, "parts": [
                {"type": "step-start", "snapshot": "abc"},
                {"type": "text", "text": "done!"},
                {"type": "step-finish", "reason": "stop"},
            ]},
        ]),
    ]

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)

        event = client.poll_completion("session-123")
        assert event["type"] == "completed"
        assert event["result"] == "done!"


def test_poll_completion_accepts_step_finish_without_completed_timestamp():
    client = OpenCodeClient(port=4096)

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        mp.setattr(client, "get_messages", lambda sid: [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [
                {"type": "text", "text": "done without completed"},
                {"type": "step-finish", "reason": "stop"},
            ]},
        ])

        event = client.poll_completion("session-123", timeout=1)
        assert event["type"] == "completed"
        assert event["result"] == "done without completed"


def test_poll_completion_ignores_trailing_placeholder():
    client = OpenCodeClient(port=4096)
    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        mp.setattr(client, "get_messages", lambda sid: [
            {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
            {"info": {"role": "assistant", "time": {"created": 2, "completed": 3}}, "parts": [
                {"type": "text", "text": "done!"},
                {"type": "step-finish", "reason": "stop"},
            ]},
            {"info": {"role": "assistant", "time": {"created": 4}}, "parts": []},
        ])

        event = client.poll_completion("session-123", timeout=1)
        assert event["type"] == "completed"
        assert event["result"] == "done!"


def test_poll_completion_streams_progress():
    client = OpenCodeClient(port=4096)
    updates = []

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        batches = iter([
            [
                {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
                {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [
                    {"id": "p1", "type": "step-start", "snapshot": "abc"},
                    {"id": "p2", "type": "reasoning", "text": "Planning next step"},
                    {"id": "p3", "type": "tool", "tool": "bash", "state": {"status": "completed", "title": "Run mvn test-compile"}},
                ]},
            ],
            [
                {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
                {"info": {"role": "assistant", "time": {"created": 2, "completed": 3}}, "parts": [
                    {"id": "p1", "type": "step-start", "snapshot": "abc"},
                    {"id": "p2", "type": "reasoning", "text": "Planning next step"},
                    {"id": "p3", "type": "tool", "tool": "bash", "state": {"status": "completed", "title": "Run mvn test-compile"}},
                    {"id": "p4", "type": "text", "text": "done!"},
                    {"id": "p5", "type": "step-finish", "reason": "stop"},
                ]},
            ],
        ])
        mp.setattr(client, "get_messages", lambda sid: next(batches))
        event = client.poll_completion("session-123", on_update=updates.append)

    assert event["type"] == "completed"
    assert any("reasoning:" in line for line in updates)
    assert any("tool[bash] completed" in line for line in updates)


def test_poll_completion_emits_pending_tool_heartbeat():
    client = OpenCodeClient(port=4096)
    updates = []

    with pytest.MonkeyPatch.context() as mp:
        import time
        now = {"value": 0.0}

        def fake_time():
            return now["value"]

        def fake_sleep(seconds):
            now["value"] += 15

        batches = [
            [
                {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
                {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [
                    {"id": "p1", "type": "step-start", "snapshot": "abc"},
                    {"id": "p2", "type": "tool", "tool": "apply_patch", "state": {"status": "pending", "title": "Write ReceiptUpBizImplTest.java"}},
                ]},
            ],
            [
                {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
                {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [
                    {"id": "p1", "type": "step-start", "snapshot": "abc"},
                    {"id": "p2", "type": "tool", "tool": "apply_patch", "state": {"status": "pending", "title": "Write ReceiptUpBizImplTest.java"}},
                ]},
            ],
            [
                {"info": {"role": "user", "time": {"created": 1}}, "parts": [{"type": "text", "text": "Hello"}]},
                {"info": {"role": "assistant", "time": {"created": 2, "completed": 3}}, "parts": [
                    {"id": "p1", "type": "step-start", "snapshot": "abc"},
                    {"id": "p2", "type": "tool", "tool": "apply_patch", "state": {"status": "completed", "title": "Write ReceiptUpBizImplTest.java"}},
                    {"id": "p3", "type": "text", "text": "done!"},
                    {"id": "p4", "type": "step-finish", "reason": "stop"},
                ]},
            ],
        ]
        mp.setattr(time, "time", fake_time)
        mp.setattr(time, "sleep", fake_sleep)
        mp.setattr(client, "get_messages", lambda sid: batches.pop(0))

        event = client.poll_completion("session-123", on_update=updates.append)

    assert event["type"] == "completed"
    assert any("waiting: tool[apply_patch] pending for" in line for line in updates)


@respx.mock
def test_list_providers():
    client = OpenCodeClient(port=4096)
    respx.get("http://127.0.0.1:4096/provider").mock(
        return_value=httpx.Response(200, json={"connected": ["openai"]})
    )

    payload = client.list_providers(repo_path="/tmp/demo")
    assert payload["connected"] == ["openai"]


@respx.mock
def test_authorize_provider_oauth():
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/provider/openai/oauth/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://auth.example.test",
                "method": "auto",
                "instructions": "Open the URL",
            },
        )
    )

    payload = client.authorize_provider_oauth("openai", 0, repo_path="/tmp/demo")

    assert payload["method"] == "auto"
    assert route.calls[0].request.url.params["directory"] == "/tmp/demo"
    assert route.calls[0].request.content == b'{"method":0}'


@respx.mock
def test_init_session_is_synchronous():
    client = OpenCodeClient(port=4096)
    route = respx.post("http://127.0.0.1:4096/session/session-123/init").mock(
        return_value=httpx.Response(200, json={})
    )

    client.init_session("session-123", "msg-1", "openai", "gpt-5.4")

    assert route.called
    assert route.calls[0].request.content == b'{"messageID":"msg-1","providerID":"openai","modelID":"gpt-5.4"}'


def test_wait_for_provider_connection():
    client = OpenCodeClient(port=4096)
    responses = iter([
        {"connected": []},
        {"connected": ["openai"]},
    ])

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(client, "list_providers", lambda repo_path=None: next(responses))
        mp.setattr(time, "sleep", lambda x: None)

        assert client.wait_for_provider_connection("openai", repo_path="/tmp/demo", timeout=5, poll_interval=0)


def test_analyze_session_retrospect_extracts_hints():
    client = OpenCodeClient(port=4096)
    messages = [
        {
            "parts": [
                {"type": "text", "text": "Mockito itself is broken at runtime because of a byte-buddy incompatibility."},
                {"type": "text", "text": "mvn test is being blocked; rerunning with -DfailIfNoTests=false."},
                {"type": "text", "text": "Surviving mutants indicate the assertions are not strong enough."},
                {"type": "text", "text": "Compile exposed the first mismatches cleanly: DiffEvent.getOrderNo() returns Long."},
                {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
                {"type": "patch"},
            ]
        },
        {
            "parts": [
                {"type": "reasoning", "text": "I should avoid decompiling jars and verify exact enum constants and Scale6Decimal APIs first."},
                {"type": "tool", "tool": "apply_patch", "state": {"status": "completed"}},
            ]
        },
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(client, "get_messages", lambda sid: messages)
        retrospect = client.analyze_session_retrospect("session-123")

    assert retrospect["session_id"] == "session-123"
    assert any("compile-safe test construction" in hint for hint in retrospect["hints"])
    assert any("source-of-truth lookup order" in hint for hint in retrospect["hints"])
    assert any("failIfNoTests=false" in hint for hint in retrospect["hints"])
    assert any("mutation resistance" in hint for hint in retrospect["hints"])
    assert any("getOrderNo() returns Long" in fact for fact in retrospect["compile_facts"])
    assert any("value-object APIs" in hint for hint in retrospect["hints"])


def test_analyze_session_tokens_splits_main_and_small(monkeypatch):
    client = OpenCodeClient(port=4096)
    messages = [
        {
            "info": {
                "role": "assistant",
                "providerID": "openai",
                "modelID": "gpt-5.4",
                "tokens": {
                    "total": 120,
                    "input": 50,
                    "output": 20,
                    "reasoning": 10,
                    "cache": {"read": 40, "write": 0},
                },
            },
            "parts": [],
        },
        {
            "info": {
                "role": "assistant",
                "providerID": "openrouter",
                "modelID": "z-ai/glm-5.1",
                "tokens": {
                    "total": 60,
                    "input": 20,
                    "output": 10,
                    "reasoning": 5,
                    "cache": {"read": 25, "write": 0},
                },
            },
            "parts": [],
        },
        {
            "info": {
                "role": "assistant",
                "providerID": "other",
                "modelID": "misc",
                "tokens": {
                    "total": 30,
                    "input": 10,
                    "output": 10,
                    "reasoning": 0,
                    "cache": {"read": 10, "write": 0},
                },
            },
            "parts": [],
        },
    ]

    monkeypatch.setattr(client, "get_messages", lambda sid: messages)
    monkeypatch.setattr("uta.opencode.client.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.opencode.client.settings.opencode_small_model", "openrouter/z-ai/glm-5.1")

    usage = client.analyze_session_tokens("session-123")

    assert usage["assistant_messages"] == 3
    assert usage["main_model_tokens"]["total"] == 120
    assert usage["small_model_tokens"]["total"] == 60
    assert usage["other_model_tokens"]["total"] == 30
    assert usage["total_tokens"]["total"] == 210


def test_analyze_session_tokens_reads_root_level_tokens(monkeypatch):
    client = OpenCodeClient(port=4096)
    messages = [
        {
            "role": "assistant",
            "providerID": "openai",
            "modelID": "gpt-5.4",
            "tokens": {
                "total": 501416,
                "input": 53257,
                "output": 4113,
                "reasoning": 1166,
                "cache": {"read": 442880, "write": 0},
            },
        },
        {
            "role": "assistant",
            "providerID": "openai",
            "modelID": "gpt-5.4",
            "tokens": {
                "input": 25148,
                "output": 2066,
                "reasoning": 427,
                "cache_read": 133120,
            },
        },
        {
            "role": "user",
            "tokens": {"input": 999999, "output": 999999},
        },
    ]

    monkeypatch.setattr(client, "get_messages", lambda sid: messages)
    monkeypatch.setattr("uta.opencode.client.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.opencode.client.settings.opencode_small_model", "")

    usage = client.analyze_session_tokens("session-123")

    assert usage["assistant_messages"] == 2
    assert usage["main_model_tokens"]["input"] == 78405
    assert usage["main_model_tokens"]["output"] == 6179
    assert usage["main_model_tokens"]["reasoning"] == 1593
    assert usage["main_model_tokens"]["cache_read"] == 576000
    assert usage["total_tokens"]["total"] == 501416 + 25148 + 2066 + 427


def test_analyze_session_tokens_prefers_persisted_messages(monkeypatch):
    from uta.opencode.client import OpenCodeClient as StreamOpenCodeClient

    client = StreamOpenCodeClient(repo_path="/tmp/repo", port=4096)
    session_id = client.create_session(model_id="openai/gpt-5.4")
    client._sessions[session_id].accumulated_tokens = {
        "input": 999,
        "output": 888,
        "reasoning": 777,
        "cache": {"read": 666, "write": 0},
        "total": 3330,
    }
    client._sessions[session_id].turn_texts = ["placeholder"]
    client._sessions[session_id].opencode_session_id = "ses_real"

    messages = [
        {
            "info": {
                "role": "assistant",
                "providerID": "openai",
                "modelID": "gpt-5.4",
                "tokens": {
                    "total": 120,
                    "input": 50,
                    "output": 20,
                    "reasoning": 10,
                    "cache": {"read": 40, "write": 0},
                },
            },
            "parts": [],
        }
    ]

    monkeypatch.setattr(client, "get_messages", lambda sid: messages)
    monkeypatch.setattr("uta.opencode.client.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.opencode.client.settings.opencode_small_model", "")

    usage = client.analyze_session_tokens(session_id)

    assert usage["assistant_messages"] == 1
    assert usage["main_model_tokens"]["input"] == 50
    assert usage["main_model_tokens"]["total"] == 120
    assert usage["total_tokens"]["total"] == 120


def test_stream_analyze_session_tokens_reads_root_level_tokens(monkeypatch):
    from uta.opencode.client import OpenCodeClient as StreamOpenCodeClient

    client = StreamOpenCodeClient(repo_path="/tmp/repo", port=4096)
    session_id = client.create_session(model_id="openai/gpt-5.4")
    messages = [
        {
            "role": "assistant",
            "providerID": "openai",
            "modelID": "gpt-5.4",
            "tokens": {
                "total": 160761,
                "input": 25148,
                "output": 2066,
                "reasoning": 427,
                "cache": {"read": 133120, "write": 0},
            },
        }
    ]

    monkeypatch.setattr(client, "get_messages", lambda sid: messages)
    monkeypatch.setattr("uta.opencode.client.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.opencode.client.settings.opencode_small_model", "")

    usage = client.analyze_session_tokens(session_id)

    assert usage["assistant_messages"] == 1
    assert usage["main_model_tokens"]["input"] == 25148
    assert usage["main_model_tokens"]["cache_read"] == 133120
    assert usage["total_tokens"]["total"] == 160761


def test_analyze_session_tokens_falls_back_to_accumulated_tokens(monkeypatch):
    from uta.opencode.client import OpenCodeClient as StreamOpenCodeClient

    client = StreamOpenCodeClient(repo_path="/tmp/repo", port=4096)
    session_id = client.create_session(model_id="openai/gpt-5.4")
    client._sessions[session_id].accumulated_tokens = {
        "input": 12,
        "output": 3,
        "reasoning": 1,
        "cache": {"read": 4, "write": 0},
        "total": 20,
    }
    client._sessions[session_id].turn_texts = ["first", "second"]

    monkeypatch.setattr(client, "get_messages", lambda sid: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("uta.opencode.client.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.opencode.client.settings.opencode_small_model", "")

    usage = client.analyze_session_tokens(session_id)

    assert usage["assistant_messages"] == 2
    assert usage["main_model_tokens"]["input"] == 12
    assert usage["main_model_tokens"]["cache_read"] == 4
    assert usage["total_tokens"]["total"] == 20


def test_analyze_session_retrospect_extracts_repeated_tools(monkeypatch):
    client = OpenCodeClient(port=4096)
    messages = [
        {
            "parts": [
                {"type": "tool", "tool": "grep", "state": {"status": "completed", "title": "Find State enum"}},
                {"type": "tool", "tool": "grep", "state": {"status": "completed", "title": "Find State enum"}},
                {"type": "tool", "tool": "grep", "state": {"status": "completed", "title": "Find State enum"}},
                {"type": "tool", "tool": "read", "state": {"status": "completed", "title": "Open Foo.java"}},
            ]
        }
    ]
    monkeypatch.setattr(client, "get_messages", lambda sid: messages)

    retrospect = client.analyze_session_retrospect("session-xyz")

    assert retrospect["repeated_tools"] == [
        {"tool": "grep", "title": "Find State enum", "count": 3}
    ]
    assert any("cached context artifacts" in hint for hint in retrospect["hints"])
