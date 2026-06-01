"""Per-turn OpenCode process runner using `opencode run --format json`."""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from uta.config import settings
from uta.opencode.tiered_router import (
    parse_provider_base_urls,
    parse_provider_tokens,
    provider_candidates,
)
from uta.opencode.rate_limit import detect_rate_limit_in_logs, parse_rate_limit_payload
from uta.opencode.stream import OpenCodeStreamParser


@dataclass
class TurnResult:
    type: str  # "completed" | "error" | "rate_limited" | "timeout"
    result: str = ""
    session_id: Optional[str] = None
    tokens: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    patch_count: int = 0
    fallback_eligible: bool = False
    fallback_reason: Optional[str] = None


def classify_provider_model_error(error_obj: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a narrow fallback reason for provider/model availability errors."""
    if not error_obj:
        return None
    data = error_obj.get("data") if isinstance(error_obj.get("data"), dict) else {}
    status_code = data.get("statusCode") or data.get("status_code") or error_obj.get("statusCode")
    message_parts = [
        error_obj.get("message"),
        error_obj.get("name"),
        data.get("message"),
        data.get("error"),
        data.get("type"),
    ]
    text = " ".join(str(part) for part in message_parts if part).lower()
    if status_code == 404:
        return "model_not_found"
    if "rate limit" in text or "too many request" in text or "quota exceeded" in text:
        return "rate_limit"
    if "disabled" in text:
        return "model_disabled"
    if "not found" in text or "does not exist" in text or "unknown model" in text:
        return "model_not_found"
    if "unavailable" in text or "not available" in text:
        return "model_unavailable"
    return None


def _configured_providers() -> set:
    chain = provider_candidates(fallback_enabled=True)
    if chain:
        return {candidate.provider for candidate in chain}
    providers = set()
    if settings.opencode_provider:
        providers.add(settings.opencode_provider)
    return providers


def _provider_from_model(model_id: Optional[str] = None) -> Optional[str]:
    if model_id and "/" in model_id:
        return model_id.split("/", 1)[0]
    chain = provider_candidates(fallback_enabled=False)
    if chain:
        return chain[0].provider
    if settings.opencode_provider:
        return settings.opencode_provider
    return None


def _provider_token_env_var(provider_id: str) -> str:
    mapping = {
        "deepseek": "DEEPSEEK_API_KEY",
        "google": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "tencent": "TENCENT_API_KEY",
        "token-pool": "OPENAI_API_KEY",
    }
    if provider_id in mapping:
        return mapping[provider_id]
    normalized = "".join(ch if ch.isalnum() else "_" for ch in provider_id.upper())
    return f"{normalized}_API_KEY"


def _build_env(
    repo_path: Optional[str] = None,
    *,
    model_id: Optional[str] = None,
) -> Dict[str, str]:
    env = os.environ.copy()
    if repo_path:
        env["PWD"] = repo_path

    # OpenCode tool calls inherit this environment. The deployed service is
    # launched by a venv interpreter, but process PATH may not include that venv.
    # Prepending it keeps Python self-checks aligned with UTA's verifier runtime.
    service_python_bin = Path(sys.executable).resolve().parent
    if service_python_bin.exists():
        path_parts = [part for part in (env.get("PATH") or "").split(os.pathsep) if part]
        service_bin_text = str(service_python_bin)
        if service_bin_text not in path_parts:
            env["PATH"] = os.pathsep.join([service_bin_text, *path_parts])
        env.setdefault("UTA_SERVICE_PYTHON_BIN", str(Path(sys.executable).resolve()))

    provider_id = _provider_from_model(model_id)
    provider_tokens = parse_provider_tokens(settings.opencode_provider_tokens)
    provider_base_urls = parse_provider_base_urls(settings.opencode_provider_base_urls)
    provider_token = provider_tokens.get(provider_id or "")
    provider_base_url = provider_base_urls.get(provider_id or "")

    gemini_key = provider_token if provider_id == "google" else ""
    gemini_key = gemini_key or (settings.gemini_api_key if provider_id == "google" else None) or ""
    if provider_id == "google" and gemini_key:
        env["GOOGLE_GENERATIVE_AI_API_KEY"] = gemini_key
        env["GOOGLE_API_KEY"] = gemini_key

    openrouter_key = provider_token if provider_id == "openrouter" else ""
    openrouter_key = openrouter_key or (settings.openrouter_api_key if provider_id == "openrouter" else None) or ""
    if provider_id == "openrouter" and openrouter_key:
        env["OPENROUTER_API_KEY"] = openrouter_key

    deepseek_key = provider_token if provider_id == "deepseek" else ""
    deepseek_key = deepseek_key or (settings.deepseek_api_key if provider_id == "deepseek" else None) or ""
    if provider_id == "deepseek" and deepseek_key:
        env["DEEPSEEK_API_KEY"] = deepseek_key

    tencent_key = provider_token if provider_id == "tencent" else ""
    tencent_key = tencent_key or (settings.tencent_api_key if provider_id == "tencent" else None) or ""
    if provider_id == "tencent" and tencent_key:
        env["TENCENT_API_KEY"] = tencent_key

    tencent_base = settings.tencent_base_url or os.environ.get("TENCENT_BASE_URL", "")
    if provider_id == "tencent" and tencent_base:
        env["TENCENT_BASE_URL"] = tencent_base

    ollama_host = settings.ollama_host or os.environ.get("OLLAMA_HOST", "")
    if provider_id == "ollama" and ollama_host:
        env["OLLAMA_HOST"] = ollama_host

    if provider_token and provider_id not in {"deepseek", "google", "openrouter", "tencent"}:
        env[_provider_token_env_var(provider_id)] = provider_token
    if provider_base_url:
        env["OPENAI_BASE_URL"] = provider_base_url
        if provider_token:
            env["OPENAI_API_KEY"] = provider_token

    # Native OpenAI (ChatGPT subscription) uses OAuth stored in OpenCode config.
    # A leaked OPENAI_API_KEY would bypass OAuth and silently fall back to API key auth.
    if provider_id == "openai" and not provider_token and not settings.openai_api_key:
        env.pop("OPENAI_API_KEY", None)

    return env


def _effective_variant(variant: Optional[str] = None) -> Optional[str]:
    value = variant if variant is not None else settings.opencode_variant
    value = (value or "").strip()
    return value or None


class OpenCodeProcess:
    """Runs `opencode run --format json` for one turn and parses the JSONL stream."""

    def __init__(self):
        self._parser = OpenCodeStreamParser()

    def run_turn(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
        repo_path: str,
        timeout: int = 3600,
        on_update: Optional[Callable[[str], None]] = None,
    ) -> TurnResult:
        cmd = self._build_cmd(
            message,
            session_id=session_id,
            model_id=model_id,
            variant=variant,
            repo_path=repo_path,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repo_path,
            env=_build_env(repo_path, model_id=model_id),
        )
        return self._read_stream(
            proc,
            timeout=timeout,
            on_update=on_update,
            model_id=model_id,
            started_at=time.time(),
        )

    def _build_cmd(
        self,
        message: str,
        *,
        session_id: Optional[str],
        model_id: Optional[str],
        variant: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> List[str]:
        spawn_cmd_raw = settings.opencode_spawn_cmd
        if spawn_cmd_raw:
            try:
                base = json.loads(spawn_cmd_raw)
                if not isinstance(base, list):
                    base = [str(spawn_cmd_raw)]
            except (json.JSONDecodeError, TypeError):
                base = [spawn_cmd_raw]
        else:
            bin_path = settings.opencode_bin or "opencode"
            base = [bin_path, "run"]

        cmd = base + ["--print-logs", "--format", "json", "--dangerously-skip-permissions"]
        if getattr(settings, "opencode_pure", True):
            cmd.append("--pure")

        if settings.opencode_attach_url:
            cmd += ["--attach", settings.opencode_attach_url]
        # repo_path is already set as cwd on the subprocess; --dir overrides config
        # resolution in opencode 1.14.50+ and prevents local opencode.json from loading.

        if model_id:
            cmd += ["--model", model_id]

        effective_variant = _effective_variant(variant)
        if effective_variant:
            cmd += ["--variant", effective_variant]

        if session_id:
            cmd += ["--session", session_id, "--continue"]

        cmd.append(message)
        return cmd

    def _read_stream(
        self,
        proc: subprocess.Popen,
        timeout: int,
        on_update: Optional[Callable[[str], None]],
        *,
        model_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> TurnResult:
        events: List[Dict[str, Any]] = []
        collected_session_id: Optional[str] = None
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        progress_lock = threading.Lock()
        stream_started_at = time.time()
        last_event_at = stream_started_at
        saw_event = False

        stdout_stream = getattr(proc, "stdout", None)
        stderr_stream = getattr(proc, "stderr", None)

        def mark_progress() -> None:
            nonlocal last_event_at, saw_event
            with progress_lock:
                last_event_at = time.time()
                saw_event = True

        def stdout_reader():
            nonlocal collected_session_id
            if stdout_stream is None:
                return
            try:
                iterator = iter(stdout_stream)
            except TypeError:
                return
            while True:
                try:
                    raw = next(iterator)
                except (StopIteration, TypeError):
                    return
                line = raw.decode(errors="replace").rstrip()
                stdout_lines.append(line)
                event = self._parser.parse_line(line)
                if event is None:
                    continue
                events.append(event)
                mark_progress()
                if collected_session_id is None:
                    collected_session_id = event.get("sessionID")
                if on_update:
                    progress = self._parser.progress_line(event)
                    if progress:
                        on_update(progress)

        def stderr_reader():
            if stderr_stream is None:
                return
            try:
                iterator = iter(stderr_stream)
            except TypeError:
                return
            while True:
                try:
                    raw = next(iterator)
                except (StopIteration, TypeError):
                    return
                line = raw.decode(errors="replace").rstrip()
                stderr_lines.append(line)
                event = self._parser.parse_line(line)
                if event is None:
                    continue
                events.append(event)
                mark_progress()

        stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
        stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        def timeout_result() -> TurnResult:
            proc.kill()
            proc.wait()
            stdout_thread.join(timeout=0.2)
            stderr_thread.join(timeout=0.2)
            inferred = self._build_result(
                events,
                collected_session_id,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                model_id=model_id,
                started_at=started_at,
            )
            if inferred.type in {"rate_limited", "error"}:
                return inferred
            return TurnResult(
                type="timeout",
                session_id=collected_session_id,
                patch_count=inferred.patch_count,
            )

        base_timeout = max(0.001, float(timeout or 0))
        active_multiplier = max(1.0, float(settings.opencode_active_timeout_multiplier or 1.0))
        active_timeout = max(base_timeout, base_timeout * active_multiplier)
        idle_timeout = max(1.0, float(settings.opencode_stream_idle_timeout_seconds or base_timeout))

        while stdout_thread.is_alive() or stderr_thread.is_alive():
            now = time.time()
            with progress_lock:
                current_last_event_at = last_event_at
                current_saw_event = saw_event
            elapsed = now - stream_started_at

            # Before OpenCode emits any JSONL event, keep the stage timeout as
            # the hard TTFT cap. Once events arrive, recent reasoning/tool/text
            # updates count as liveness and the broader active cap applies.
            if not current_saw_event and elapsed >= base_timeout:
                return timeout_result()
            if current_saw_event and elapsed >= active_timeout:
                return timeout_result()
            if current_saw_event and now - current_last_event_at >= idle_timeout:
                return timeout_result()

            time.sleep(0.1)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        return self._build_result(
            events,
            collected_session_id,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            model_id=model_id,
            started_at=started_at,
        )

    def _build_result(
        self,
        events: List[Dict[str, Any]],
        session_id: Optional[str],
        *,
        stdout_lines: Optional[List[str]] = None,
        stderr_lines: Optional[List[str]] = None,
        model_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> TurnResult:
        for ev in events:
            if ev.get("type") == "error":
                error_obj = ev.get("error") or {}
                if self._parser.detect_rate_limit(ev):
                    data = error_obj.get("data") or {}
                    return TurnResult(
                        type="rate_limited",
                        session_id=session_id,
                        error=error_obj,
                        result="",
                        fallback_eligible=True,
                        fallback_reason="rate_limit",
                    )
                fallback_reason = classify_provider_model_error(error_obj)
                return TurnResult(
                    type="error",
                    session_id=session_id,
                    error=error_obj,
                    result=self._parser.extract_text(events),
                    fallback_eligible=fallback_reason is not None,
                    fallback_reason=fallback_reason,
                )

        result_text = self._parser.extract_text(events)
        tokens = self._parser.extract_tokens(events)
        patch_count = self._parser.count_patches(events)
        completion = self._parser.detect_completion(events)

        # If the JSONL stream reached an explicit terminal stop, trust that
        # completed turn over broader fallback heuristics that inspect raw
        # stderr/logs. Those fallbacks are intended for missing/partial streams
        # and can pick up unrelated provider noise.
        if completion == "stop":
            return TurnResult(
                type="completed",
                result=result_text,
                session_id=session_id,
                tokens=tokens,
                patch_count=patch_count,
            )

        stderr_output = "\n".join(line for line in (stderr_lines or []) if line).strip()
        text_rate_limit = self._infer_rate_limit_from_text(stderr_output)
        if text_rate_limit:
            return TurnResult(
                type="rate_limited",
                session_id=session_id,
                error=text_rate_limit,
                result=self._parser.extract_text(events),
                fallback_eligible=True,
                fallback_reason="rate_limit",
            )

        log_rate_limit = self._infer_rate_limit_from_logs(
            session_id=session_id,
            model_id=model_id,
            started_at=started_at,
        )
        if log_rate_limit:
            return TurnResult(
                type="rate_limited",
                session_id=session_id,
                error=log_rate_limit,
                result=self._parser.extract_text(events),
                fallback_eligible=True,
                fallback_reason="rate_limit",
            )

        if not completion and not result_text:
            return TurnResult(type="stalled", session_id=session_id, tokens=tokens)

        return TurnResult(
            type="completed",
            result=result_text,
            session_id=session_id,
            tokens=tokens,
        )

    def _infer_rate_limit_from_text(self, raw_output: str) -> Optional[Dict[str, Any]]:
        if not raw_output:
            return None
        parsed = self._infer_structured_rate_limit(raw_output)
        if parsed:
            return parsed
        plain_lines = []
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{") or " error={" in stripped:
                continue
            plain_lines.append(stripped)
        plain_text = "\n".join(plain_lines)
        if not self._parser.detect_rate_limit_text(plain_text):
            return None
        retry_after = self._extract_int(raw_output, "resets_in_seconds")
        reset_at = self._extract_int(raw_output, "resets_at")
        return {
            "raw_type": "rate_limit",
            "message": plain_text.splitlines()[0][:500] if plain_text else "Provider/model rate limit reached",
            "retry_after_seconds": retry_after,
            "reset_at": reset_at,
            "data": {
                "statusCode": 429,
                "message": plain_text.splitlines()[0][:500] if plain_text else "Provider/model rate limit reached",
                **({"resets_in_seconds": retry_after} if retry_after is not None else {}),
                **({"resets_at": reset_at} if reset_at is not None else {}),
            },
        }

    def _infer_structured_rate_limit(self, raw_output: str) -> Optional[Dict[str, Any]]:
        decoder = json.JSONDecoder()
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            candidates = [stripped]
            if " error=" in stripped:
                candidates.insert(0, stripped.partition(" error=")[2])
            for candidate in candidates:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    payload, _ = decoder.raw_decode(candidate)
                except Exception:
                    continue
                parsed = parse_rate_limit_payload(payload)
                if not parsed:
                    continue
                parsed["retry_after_seconds"] = parsed.get("retry_after_seconds") or self._extract_int(candidate, "resets_in_seconds")
                parsed["reset_at"] = parsed.get("reset_at") or self._extract_int(candidate, "resets_at")
                parsed.setdefault("data", {})
                parsed["data"].setdefault("statusCode", parsed.get("status_code") or 429)
                parsed["data"].setdefault("message", parsed.get("message") or "Provider/model rate limit reached")
                return parsed
        return None

    def _infer_rate_limit_from_logs(
        self,
        *,
        session_id: Optional[str],
        model_id: Optional[str],
        started_at: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        provider_id = model_id.split("/", 1)[0] if model_id and "/" in model_id else None
        bare_model_id = model_id.split("/", 1)[1] if model_id and "/" in model_id else model_id
        return detect_rate_limit_in_logs(
            session_id=session_id,
            provider_id=provider_id,
            model_id=bare_model_id,
            since_time=started_at,
        )

    @staticmethod
    def _extract_int(raw_text: Any, field: str) -> Optional[int]:
        if not isinstance(raw_text, str):
            return None
        marker = f'"{field}":'
        idx = raw_text.find(marker)
        if idx < 0:
            return None
        idx += len(marker)
        digits = []
        while idx < len(raw_text) and raw_text[idx] in " \t":
            idx += 1
        while idx < len(raw_text) and raw_text[idx].isdigit():
            digits.append(raw_text[idx])
            idx += 1
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None
