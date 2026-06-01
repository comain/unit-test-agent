"""Phase 1 gate tests for OpenCodeProcess."""

import io
import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from uta.opencode.process import OpenCodeProcess, TurnResult, _build_env


def _jsonl(*events) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def _step_start(session_id="ses_abc"):
    return {"type": "step_start", "sessionID": session_id, "part": {"type": "step-start"}}


def _text(text, session_id="ses_abc"):
    return {"type": "text", "sessionID": session_id, "part": {"type": "text", "text": text}}


def _step_finish(reason="stop", tokens=None, session_id="ses_abc"):
    return {
        "type": "step_finish",
        "sessionID": session_id,
        "part": {
            "reason": reason,
            "tokens": tokens or {"input": 100, "output": 20, "reasoning": 0, "cache": {"write": 0, "read": 0}, "total": 120},
        },
    }


def _error(message, status_code=None, session_id="ses_abc"):
    data = {"message": message}
    if status_code is not None:
        data["statusCode"] = status_code
    return {"type": "error", "sessionID": session_id, "error": {"name": "UnknownError", "data": data}}


def _make_proc(stdout_bytes: bytes, returncode: int = 0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(stdout_bytes)
    proc.returncode = returncode
    proc.wait = MagicMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def oc_process():
    return OpenCodeProcess()


# --- _build_cmd ---

def test_build_cmd_defaults(oc_process):
    with patch("uta.opencode.process.OpenCodeProcess._build_cmd", wraps=oc_process._build_cmd):
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert "opencode" in cmd[0] or cmd[0].endswith("opencode")
    assert "run" in cmd
    assert "--print-logs" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--pure" in cmd
    assert "hello" == cmd[-1]


def test_build_cmd_can_disable_pure_mode(monkeypatch, oc_process):
    monkeypatch.setattr("uta.opencode.process.settings.opencode_pure", False)
    cmd = oc_process._build_cmd("hello", session_id=None, model_id=None)
    assert "--pure" not in cmd


def test_build_cmd_with_model(oc_process):
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="cursor/claude-4.5-sonnet")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "cursor/claude-4.5-sonnet"


def test_build_cmd_with_variant(oc_process):
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="openai/gpt-5.5", variant="none")
    assert "--variant" in cmd
    idx = cmd.index("--variant")
    assert cmd[idx + 1] == "none"


def test_build_cmd_uses_configured_variant(monkeypatch, oc_process):
    monkeypatch.setattr("uta.opencode.process.settings.opencode_variant", "low")
    cmd = oc_process._build_cmd("hi", session_id=None, model_id="openai/gpt-5.5")
    assert "--variant" in cmd
    idx = cmd.index("--variant")
    assert cmd[idx + 1] == "low"


def test_build_cmd_with_session(oc_process):
    cmd = oc_process._build_cmd("hi", session_id="ses_abc123", model_id=None)
    assert "--session" in cmd
    idx = cmd.index("--session")
    assert cmd[idx + 1] == "ses_abc123"
    assert "--continue" in cmd


def test_build_cmd_attach_url(oc_process):
    with patch("uta.opencode.process.settings") as mock_settings:
        mock_settings.opencode_attach_url = "http://localhost:4096"
        mock_settings.opencode_spawn_cmd = None
        mock_settings.opencode_bin = None
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None, repo_path="/tmp/repo")
    assert "--attach" in cmd
    idx = cmd.index("--attach")
    assert cmd[idx + 1] == "http://localhost:4096"
    assert "--dir" not in cmd  # repo_path is cwd, not --dir (opencode 1.14.50+ compat)


def test_build_cmd_custom_spawn_cmd(oc_process):
    with patch("uta.opencode.process.settings") as mock_settings:
        mock_settings.opencode_spawn_cmd = '["bun", "run", "src/index.ts", "run"]'
        mock_settings.opencode_attach_url = None
        mock_settings.opencode_bin = None
        cmd = oc_process._build_cmd("hello", session_id=None, model_id=None, repo_path="/tmp/repo")
    assert cmd[0] == "bun"
    assert cmd[1] == "run"
    assert cmd[2] == "src/index.ts"
    assert cmd[3] == "run"
    assert "--dir" not in cmd


def test_run_turn_uses_repo_dir_for_command_and_process_cwd(oc_process, tmp_path):
    repo_path = str(tmp_path)
    captured = {}
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(_jsonl(_step_start(), _text("ok"), _step_finish()))
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc

    with patch("uta.opencode.process.subprocess.Popen", side_effect=fake_popen):
        result = oc_process.run_turn("hello", repo_path=repo_path, timeout=10)

    assert result.type == "completed"
    assert "--dir" not in captured["cmd"]
    assert captured["kwargs"]["cwd"] == repo_path
    assert captured["kwargs"]["env"]["PWD"] == repo_path


def test_build_env_prepends_service_python_bin(monkeypatch, tmp_path):
    service_bin = tmp_path / "venv" / "bin"
    service_bin.mkdir(parents=True)
    service_python = service_bin / "python"
    service_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("uta.opencode.process.sys.executable", str(service_python))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = _build_env(str(tmp_path))

    assert env["PATH"].split(":")[0] == str(service_bin.resolve())
    assert env["UTA_SERVICE_PYTHON_BIN"] == str(service_python.resolve())


# --- _read_stream / _build_result ---

def test_turn_result_from_jsonl_stream(oc_process):
    events = [_step_start(), _text("Hello world"), _step_finish()]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "Hello world" in result.result
    assert result.session_id == "ses_abc"


def test_read_stream_ignores_non_iterable_mock_streams(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = object()
    proc.stderr = object()
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=1, on_update=None)

    assert result.type == "stalled"


def test_session_id_extracted_from_first_event(oc_process):
    events = [_step_start("ses_xyz789"), _text("hi", "ses_xyz789"), _step_finish(session_id="ses_xyz789")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.session_id == "ses_xyz789"


def test_tokens_extracted_from_step_finish(oc_process):
    tokens = {"input": 500, "output": 100, "reasoning": 0, "cache": {"write": 200, "read": 100}, "total": 600}
    events = [_step_start(), _text("done"), _step_finish(tokens=tokens)]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.tokens["input"] == 500
    assert result.tokens["output"] == 100
    assert result.tokens["total"] == 600


def test_patch_count_extracted_from_stream(oc_process):
    events = [
        _step_start(),
        {
            "type": "tool_use",
            "sessionID": "ses_abc",
            "part": {
                "type": "tool",
                "tool": "apply_patch",
                "state": {"status": "completed", "output": "Success. Updated the following files:\nM A.java"},
            },
        },
        _step_finish(),
    ]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert result.patch_count == 1


def test_rate_limit_error_detected(oc_process):
    events = [_error("Too many requests, quota exceeded", status_code=429)]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "rate_limited"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "rate_limit"


def test_regular_error_detected(oc_process):
    events = [_error("Model not found: tencent/glm-5")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.error is not None
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_not_found"


def test_disabled_model_error_is_fallback_eligible(oc_process):
    events = [_error("The requested model is disabled for this account")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_disabled"


def test_unavailable_model_error_is_fallback_eligible(oc_process):
    events = [_error("Model openai/gpt-5.5 is temporarily unavailable")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is True
    assert result.fallback_reason == "model_unavailable"


def test_generic_error_is_not_fallback_eligible(oc_process):
    events = [_error("OpenCode command failed while applying patch")]
    proc = _make_proc(_jsonl(*events))
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "error"
    assert result.fallback_eligible is False
    assert result.fallback_reason is None


def test_rate_limit_detected_from_raw_stderr_without_events(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(b"")
    proc.stderr = io.BytesIO(
        b'{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached","resets_in_seconds":2493,"resets_at":1776925907}}\n'
    )
    proc.returncode = 1
    proc.wait = MagicMock(return_value=1)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["data"]["statusCode"] == 429
    assert result.error["retry_after_seconds"] == 2493
    assert result.error["reset_at"] == 1776925907


def test_connection_error_prompt_text_does_not_trigger_rate_limit(oc_process):
    raw = json.dumps(
        {
            "error": {
                "name": "AI_RetryError",
                "errors": [
                    {
                        "name": "AI_APICallError",
                        "cause": {"code": "ConnectionRefused", "path": "http://127.0.0.1:1234/v1/responses"},
                        "requestBodyValues": {
                            "input": [
                                {
                                    "role": "system",
                                    "content": 'Example: "implement rate limiting" -> Rate limiting implementation',
                                }
                            ]
                        },
                    }
                ],
            }
        }
    )

    assert oc_process._infer_rate_limit_from_text(f"ERROR service=llm error={raw}") is None


def test_non_terminal_stdout_rate_limit_words_do_not_trigger_rate_limit(oc_process):
    events = [_step_start(), _text("planning an implement rate limiting example")]
    proc = _make_proc(_jsonl(*events))

    result = oc_process._read_stream(proc, timeout=10, on_update=None)

    assert result.type == "completed"
    assert "rate limiting" in result.result


def test_completed_stop_beats_raw_rate_limit_noise(oc_process):
    events = [_step_start(), _text("### SampleServiceImpl"), _step_finish(reason="stop")]
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(_jsonl(*events))
    proc.stderr = io.BytesIO(
        b'{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached","resets_in_seconds":2493,"resets_at":1776925907}}\n'
    )
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "SampleServiceImpl" in result.result


def test_rate_limit_detected_from_server_logs_without_stream_output(oc_process):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = io.BytesIO(b"")
    proc.stderr = io.BytesIO(b"")
    proc.returncode = 1
    proc.wait = MagicMock(return_value=1)
    proc.kill = MagicMock()

    with patch(
        "uta.opencode.process.detect_rate_limit_in_logs",
        return_value={
            "provider_id": "openai",
            "model_id": "gpt-5.4",
            "status_code": 429,
            "raw_type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 2493,
            "reset_at": 1776925907,
        },
    ):
        result = oc_process._read_stream(
            proc,
            timeout=10,
            on_update=None,
            model_id="openai/gpt-5.4",
            started_at=1776923400.0,
        )
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["provider_id"] == "openai"
    assert result.error["model_id"] == "gpt-5.4"
    assert result.error["retry_after_seconds"] == 2493


def test_on_update_receives_progress(oc_process):
    updates = []
    events = [_step_start(), _text("Hello!"), _step_finish()]
    proc = _make_proc(_jsonl(*events))
    oc_process._read_stream(proc, timeout=10, on_update=updates.append)
    assert any("Hello!" in u for u in updates)


def test_malformed_lines_ignored(oc_process):
    raw = b"plugin initialized\n" + _jsonl(_step_start(), _text("real output"), _step_finish())
    proc = _make_proc(raw)
    result = oc_process._read_stream(proc, timeout=10, on_update=None)
    assert result.type == "completed"
    assert "real output" in result.result


def test_timeout_kills_process(oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.kill = MagicMock()
    proc.wait = MagicMock()

    result = oc_process._read_stream(proc, timeout=0.1, on_update=None)
    assert result.type == "timeout"
    proc.kill.assert_called_once()


def test_timeout_returns_rate_limit_if_logs_show_429(oc_process):
    import time

    def slow_read(*args, **kwargs):
        time.sleep(60)

    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = MagicMock()
    proc.stdout.__iter__ = slow_read
    proc.stderr = io.BytesIO(b"")
    proc.kill = MagicMock()
    proc.wait = MagicMock(return_value=1)

    with patch(
        "uta.opencode.process.detect_rate_limit_in_logs",
        return_value={
            "provider_id": "openai",
            "model_id": "gpt-5.4",
            "status_code": 429,
            "raw_type": "usage_limit_reached",
            "message": "The usage limit has been reached",
            "retry_after_seconds": 2493,
            "reset_at": 1776925907,
        },
    ):
        result = oc_process._read_stream(
            proc,
            timeout=0.1,
            on_update=None,
            model_id="openai/gpt-5.4",
            started_at=1776923400.0,
        )
    assert result.type == "rate_limited"
    assert result.error is not None
    assert result.error["retry_after_seconds"] == 2493
    proc.kill.assert_called_once()


# --- _build_env ---

def test_env_strips_openai_key_for_openai_provider(monkeypatch):
    monkeypatch.setattr("uta.opencode.process.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("uta.opencode.process.settings.openai_api_key", None)
    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-test"}, clear=True):
        env = _build_env(model_id="openai/gpt-5.5")
    assert "OPENAI_API_KEY" not in env


def test_env_keeps_openai_key_for_other_providers():
    with patch("uta.opencode.process._configured_providers", return_value={"cursor"}):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
            env = _build_env()
    assert "OPENAI_API_KEY" in env


def test_env_sets_google_key_from_settings(monkeypatch):
    monkeypatch.setattr("uta.opencode.process.settings.gemini_api_key", "gkey-123")
    monkeypatch.setattr("uta.opencode.process.settings.opencode_provider_tokens", "")
    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="google/gemini-2.5-flash")
    assert env.get("GOOGLE_GENERATIVE_AI_API_KEY") == "gkey-123"
    assert env.get("GOOGLE_API_KEY") == "gkey-123"


def test_env_injects_only_selected_token_pool_token(monkeypatch):
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;openai.token=openai-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr("uta.opencode.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("uta.opencode.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="token-pool/gpt-5.5")

    assert env["OPENAI_API_KEY"] == "tp-secret"
    assert env.get("DEEPSEEK_API_KEY") != "deepseek-secret"
    assert "openai-secret" not in env.values()


def test_env_injects_only_selected_deepseek_token(monkeypatch):
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr("uta.opencode.process.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("uta.opencode.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("uta.opencode.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked"}, clear=True):
        env = _build_env(model_id="deepseek/deepseek-v4-pro")

    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert env.get("OPENAI_API_KEY") == "leaked"
    assert "tp-secret" not in env.values()


def test_env_sets_openai_compatible_base_url_for_selected_provider(monkeypatch):
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;deepseek.token=deepseek-secret",
    )
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_base_urls",
        "token-pool.base_url=http://token-pool.test/v1;deepseek.base_url=http://deepseek.test/v1",
    )
    monkeypatch.setattr("uta.opencode.process.settings.deepseek_api_key", None)
    monkeypatch.setattr("uta.opencode.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
        env = _build_env(model_id="deepseek/deepseek-v4-pro")

    assert env["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert env["OPENAI_API_KEY"] == "deepseek-secret"
    assert env["OPENAI_BASE_URL"] == "http://deepseek.test/v1"
    assert "tp-secret" not in env.values()


def test_env_keeps_native_openai_oauth_when_no_provider_token(monkeypatch):
    monkeypatch.setattr(
        "uta.opencode.process.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5",
    )
    monkeypatch.setattr("uta.opencode.process.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("uta.opencode.process.settings.openai_api_key", None)

    with patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "leaked"}, clear=True):
        env = _build_env(model_id="openai/gpt-5.5")

    assert "OPENAI_API_KEY" not in env
