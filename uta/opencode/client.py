"""OpenCode client.

- ``OpenCodeClient`` — stream-based client using ``opencode run --format json``.
  Used by nodes.py for the main generation / fix workflow.
- ``OpenCodeAuthClient`` — HTTP client. Preserves the old HTTP polling logic
  for provider auth flows (``uta connect``) and the ``/init`` slash bootstrap.
  Used by cli.py and project_summary.py.
"""

import httpx
import json
import re
import time
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional, Set

from uta.config import settings
from uta.opencode.net import build_base_url
from uta.opencode.process import OpenCodeProcess, TurnResult
from uta.opencode.rate_limit import parse_rate_limit_payload, recent_log_files


# ---------------------------------------------------------------------------
# Stream-based client (new) — used by nodes.py
# ---------------------------------------------------------------------------

@dataclass
class _SessionState:
    model_id: Optional[str] = None
    variant: Optional[str] = None
    opencode_session_id: Optional[str] = None
    pending_message: Optional[str] = None
    pending_model_id: Optional[str] = None
    pending_variant: Optional[str] = None
    patch_count: int = 0
    accumulated_tokens: Dict[str, Any] = field(default_factory=dict)
    turn_texts: List[str] = field(default_factory=list)


def _add_tokens(dest: Dict[str, Any], src: Dict[str, Any]) -> None:
    for key in ("input", "output", "reasoning", "total"):
        dest[key] = dest.get(key, 0) + int(src.get(key, 0) or 0)
    cache_src = src.get("cache") or {}
    cache_dest = dest.setdefault("cache", {})
    for ckey in ("read", "write"):
        cache_dest[ckey] = cache_dest.get(ckey, 0) + int(cache_src.get(ckey, 0) or 0)


def _message_payload(msg: Dict[str, Any]) -> Dict[str, Any]:
    data = msg.get("data")
    return data if isinstance(data, dict) else msg


def _message_info(msg: Dict[str, Any]) -> Dict[str, Any]:
    payload = _message_payload(msg)
    info = payload.get("info")
    return info if isinstance(info, dict) else {}


def _message_role(msg: Dict[str, Any]) -> str:
    payload = _message_payload(msg)
    info = _message_info(msg)
    return str(info.get("role") or payload.get("role") or "")


def _message_tokens(msg: Dict[str, Any]) -> Dict[str, Any]:
    payload = _message_payload(msg)
    root_tokens = payload.get("tokens")
    if isinstance(root_tokens, dict) and root_tokens:
        return root_tokens
    info_tokens = _message_info(msg).get("tokens")
    return info_tokens if isinstance(info_tokens, dict) else {}


def _message_model(msg: Dict[str, Any]) -> tuple[str, str]:
    payload = _message_payload(msg)
    info = _message_info(msg)
    provider_id = str(info.get("providerID") or info.get("provider_id") or payload.get("providerID") or payload.get("provider_id") or "")
    model_id = str(info.get("modelID") or info.get("model_id") or payload.get("modelID") or payload.get("model_id") or "")
    return provider_id, model_id


def _cache_tokens(tokens: Dict[str, Any], key: str) -> int:
    cache = tokens.get("cache", {}) or {}
    flat_key = f"cache_{key}"
    camel_key = f"cache{key.title()}"
    return int(cache.get(key, tokens.get(flat_key, tokens.get(camel_key, 0))) or 0)


def _effective_variant(variant: Optional[str] = None) -> Optional[str]:
    value = variant if variant is not None else settings.opencode_variant
    value = (value or "").strip()
    return value or None


def _provider_from_model_id(model_id: Optional[str]) -> Optional[str]:
    if model_id and "/" in model_id:
        return model_id.split("/", 1)[0]
    return None


class OpenCodeClient:
    """Stream-based OpenCode client.

    Uses ``opencode run --format json`` per turn. The public API is backward-
    compatible with the old HTTP-based client so nodes.py callers need no changes.

    Session lifecycle::

        sid = client.create_session(model_id="cursor/claude-4.5-sonnet")
        client.send_message(sid, "Generate tests…")
        event = client.poll_completion(sid, timeout=3600, on_update=…)
        client.delete_session(sid)
    """

    def __init__(self, repo_path: Optional[str] = None, port: int = None):
        self._repo_path = repo_path
        self._process = OpenCodeProcess()
        self._sessions: Dict[str, _SessionState] = {}
        self.port = port or settings.opencode_port
        self.base_url = build_base_url(settings.opencode_host, self.port)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(
        self,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        permission: Optional[list] = None,
        variant: Optional[str] = None,
    ) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = _SessionState(model_id=model_id, variant=_effective_variant(variant))
        return sid

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    def send_message(
        self,
        session_id: str,
        content: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        state = self._sessions.setdefault(session_id, _SessionState())
        state.pending_message = content
        state.pending_model_id = model_id or state.model_id
        state.pending_variant = _effective_variant(variant) or state.variant
        return {}

    def send_message_split(
        self,
        session_id: str,
        stable_prefix: str,
        volatile_tail: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        return self.send_message(session_id, f"{stable_prefix}{volatile_tail}", model_id=model_id, variant=variant)

    def send_prompt(
        self,
        session_id: str,
        prompt: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> str:
        self.send_message(session_id, prompt, model_id=model_id, variant=variant)
        return session_id

    def send_message_and_get_user_info(
        self,
        session_id: str,
        content: str,
        timeout: int = 120,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.send_message(session_id, content, model_id=model_id, variant=variant)
        result = self.poll_completion(session_id, timeout=timeout)
        return {"role": "user", "result": result}

    # ------------------------------------------------------------------
    # Completion polling
    # ------------------------------------------------------------------

    def poll_completion(
        self,
        session_id: str,
        timeout: int = 600,
        on_update: Optional[Callable[[str], None]] = None,
        stalled_after_recovery_seconds: int = 180,
        stalled_no_progress_seconds: int = 180,
        rate_limit_check_interval_seconds: int = 3,
    ) -> Dict[str, Any]:
        state = self._sessions.get(session_id)
        if state is None:
            return {"type": "error", "result": "",
                    "error": {"data": {"message": f"unknown session {session_id}"}}}

        message = state.pending_message
        if not message:
            return {"type": "error", "result": "", "error": {"data": {"message": "no pending message"}}}
        state.pending_message = None

        model_id = state.pending_model_id or state.model_id or settings.opencode_model
        variant = state.pending_variant or state.variant or _effective_variant()

        run_kwargs = {
            "session_id": state.opencode_session_id,
            "model_id": model_id,
            "repo_path": self._repo_path,
            "timeout": timeout,
            "on_update": on_update,
        }
        if variant:
            run_kwargs["variant"] = variant
        result: TurnResult = self._process.run_turn(message, **run_kwargs)

        if result.session_id:
            state.opencode_session_id = result.session_id
        if result.tokens:
            _add_tokens(state.accumulated_tokens, result.tokens)
        state.patch_count += int(result.patch_count or 0)
        if result.result:
            state.turn_texts.append(result.result)

        if result.type == "rate_limited":
            data = (result.error or {}).get("data") or {}
            return {
                "type": "rate_limited",
                "result": "",
                "reason": data.get("message") or "provider/model rate limit reached",
                "rate_limit": result.error,
            }
        if result.type == "error":
            event = {"type": "error", "result": result.result, "error": result.error}
            if result.fallback_eligible:
                error_obj = result.error or {}
                data = error_obj.get("data") if isinstance(error_obj.get("data"), dict) else {}
                event.update(
                    {
                        "fallback_eligible": True,
                        "fallback_reason": result.fallback_reason,
                        "provider_id": data.get("provider_id")
                        or _provider_from_model_id(model_id)
                        or settings.opencode_provider,
                        "model_id": data.get("model_id") or model_id,
                    }
                )
                if result.fallback_reason == "rate_limit":
                    return {
                        "type": "rate_limited",
                        "result": "",
                        "reason": data.get("message")
                        or error_obj.get("message")
                        or "provider/model rate limit reached",
                        "rate_limit": {
                            "provider_id": event["provider_id"],
                            "model_id": event["model_id"],
                            "message": data.get("message") or error_obj.get("message"),
                            "status_code": data.get("statusCode") or data.get("status_code"),
                            "raw_type": result.fallback_reason,
                        },
                    }
            return event
        if result.type == "timeout":
            return {"type": "timeout", "result": ""}
        if result.type == "stalled":
            return {"type": "stalled_no_progress", "result": "", "reason": "no output from stream"}
        return {"type": "completed", "result": result.result}

    def poll_events(self, session_id: str, request_id: str, timeout: int = 600) -> Dict[str, Any]:
        return self.poll_completion(session_id, timeout)

    # ------------------------------------------------------------------
    # Token / retrospect analysis
    # ------------------------------------------------------------------

    def analyze_session_tokens(self, session_id: str) -> Dict[str, Any]:
        configured_main = settings.opencode_model or ""
        configured_small = settings.opencode_small_model or ""

        def empty_bucket() -> Dict[str, int]:
            return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0}

        def add_usage(bucket: Dict[str, int], tokens: Dict[str, Any]) -> None:
            bucket["input"] += int(tokens.get("input", 0) or 0)
            bucket["output"] += int(tokens.get("output", 0) or 0)
            bucket["reasoning"] += int(tokens.get("reasoning", 0) or 0)
            bucket["cache_read"] += _cache_tokens(tokens, "read")
            bucket["cache_write"] += _cache_tokens(tokens, "write")
            total = tokens.get("total")
            if total is None:
                total = (
                    int(tokens.get("input", 0) or 0)
                    + int(tokens.get("output", 0) or 0)
                    + int(tokens.get("reasoning", 0) or 0)
                )
            bucket["total"] += int(total or 0)

        main_bucket = empty_bucket()
        small_bucket = empty_bucket()
        other_bucket = empty_bucket()
        total_bucket = empty_bucket()
        assistant_messages = 0
        by_model: Dict[str, Dict[str, int]] = {}

        try:
            messages = self.get_messages(session_id)
        except Exception:
            messages = []

        if messages:
            for msg in messages:
                if _message_role(msg) != "assistant":
                    continue
                assistant_messages += 1
                tokens = _message_tokens(msg)
                provider_id, model_id = _message_model(msg)
                full_model = f"{provider_id}/{model_id}" if provider_id else model_id

                add_usage(total_bucket, tokens)
                model_bucket = by_model.setdefault(full_model, empty_bucket())
                add_usage(model_bucket, tokens)

                if full_model == configured_main:
                    add_usage(main_bucket, tokens)
                elif full_model == configured_small:
                    add_usage(small_bucket, tokens)
                else:
                    add_usage(other_bucket, tokens)
        else:
            state = self._sessions.get(session_id)
            tokens = state.accumulated_tokens if state else {}
            if tokens:
                add_usage(total_bucket, tokens)
                add_usage(main_bucket, tokens)
            assistant_messages = len(state.turn_texts) if state else 0

        return {
            "session_id": session_id,
            "assistant_messages": assistant_messages,
            "main_model": configured_main,
            "small_model": configured_small,
            "main_model_tokens": main_bucket,
            "small_model_tokens": small_bucket,
            "other_model_tokens": other_bucket,
            "total_tokens": total_bucket,
            "by_model": by_model,
        }

    def analyze_session_retrospect(self, session_id: str) -> Dict[str, Any]:
        state = self._sessions.get(session_id)
        texts = state.turn_texts if state else []
        blob = "\n".join(texts).lower()

        hints: List[str] = []

        _HINT_SIGNALS = [
            ("mockito" in blob or "byte-buddy" in blob or "concrete class" in blob,
             "Front-load compile-safe test construction: avoid mocking concrete classes."),
            ("jar" in blob or "maven repository" in blob or "decompile" in blob,
             "Prefer current repo source over jars/decompiled artifacts."),
            ("long vs" in blob or "enum constant" in blob,
             "Verify exact numeric types, enum constants, and value-object APIs before writing assertions."),
            ("failifnotests=false" in blob or "no matching tests" in blob,
             "Use -DfailIfNoTests=false for multi-module Maven runs."),
            ("surviving mutants" in blob or "mutation" in blob,
             "Bias first-round tests toward mutation resistance."),
        ]
        for condition, hint in _HINT_SIGNALS:
            if condition:
                hints.append(hint)

        return {
            "session_id": session_id,
            "hint_count": len(hints),
            "hints": hints,
            "compile_facts": [],
            "observations": [],
            "tool_count": 0,
            "patch_count": 0,
            "repeated_tools": [],
        }

    # ------------------------------------------------------------------
    # HTTP pass-through for init slash bootstrap (used by project_summary.py)
    # ------------------------------------------------------------------

    def _http_request(self, method: str, path: str, timeout: float = 10.0, **kwargs) -> dict:
        try:
            r = httpx.request(method, f"{self.base_url}{path}", timeout=timeout, **kwargs)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    def get_messages(self, session_id: str) -> list:
        state = self._sessions.get(session_id)
        oc_id = state.opencode_session_id if state else session_id
        return self._http_request("GET", f"/session/{oc_id}/message") or []

    def get_session_patch_count(self, session_id: str) -> int:
        state = self._sessions.get(session_id)
        return int(state.patch_count if state else 0)

    def init_session(self, session_id: str, message_id: str, provider_id: str, model_id: str) -> None:
        state = self._sessions.get(session_id)
        oc_id = state.opencode_session_id if state else session_id
        try:
            httpx.post(
                f"{self.base_url}/session/{oc_id}/init",
                json={"messageID": message_id, "providerID": provider_id, "modelID": model_id},
                timeout=3600.0,
            )
        except Exception:
            pass

    def latest_completion(self, session_id: str) -> Optional[Dict[str, Any]]:
        return None

    def detect_rate_limit_issue(self, session_id: str, max_files: int = 5) -> Optional[Dict[str, Any]]:
        return None


# ---------------------------------------------------------------------------
# HTTP-based auth client — preserved for `uta connect`, `project_summary.py`
# ---------------------------------------------------------------------------

class OpenCodeAuthClient:
    """Full HTTP client preserving the old polling-based implementation.

    Used by ``cli.py`` (auth flows) and ``project_summary.py`` (/init bootstrap).
    Not used in the main generation workflow.
    """

    def __init__(self, repo_path: Optional[str] = None, port: int = None):
        self._repo_path = repo_path
        self.port = port or settings.opencode_port
        self.base_url = build_base_url(settings.opencode_host, self.port)

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                             httpx.WriteTimeout, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
        return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 10.0,
        retries: int = 3,
        retry_delay: float = 1.0,
        **kwargs,
    ) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                response = httpx.request(method, f"{self.base_url}{path}", timeout=timeout, **kwargs)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                if attempt >= retries or not self._is_transient_error(exc):
                    raise
                time.sleep(retry_delay * attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Request failed: {method} {path}")

    @staticmethod
    def _opencode_log_dir() -> Path:
        from uta.opencode.rate_limit import opencode_log_dir

        return opencode_log_dir()

    @classmethod
    def _uta_debug_log_dir(cls) -> Path:
        from uta.opencode.rate_limit import uta_debug_log_dir

        return uta_debug_log_dir()

    @classmethod
    def _recent_log_files(cls, limit: int = 5) -> List[Path]:
        candidates: List[Path] = []
        opencode_log_dir = cls._opencode_log_dir()
        if opencode_log_dir.exists():
            candidates.extend(p for p in opencode_log_dir.iterdir() if p.is_file())
        uta_log_dir = cls._uta_debug_log_dir()
        if uta_log_dir.exists():
            candidates.extend(p for p in uta_log_dir.iterdir() if p.is_file() and "_opencode_" in p.name)
        if not candidates:
            return []
        unique = {p.resolve(): p for p in candidates}
        return sorted(unique.values(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]

    @staticmethod
    def _parse_rate_limit_payload(error_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return parse_rate_limit_payload(error_payload)

    def detect_rate_limit_issue(self, session_id: str, max_files: int = 5) -> Optional[Dict[str, Any]]:
        session_marker = f"sessionID={session_id}"
        alt_session_marker = f"session.id={session_id}"
        provider_re = re.compile(r"providerID=([^\s]+)")
        model_re = re.compile(r"modelID=([^\s]+)")

        for log_path in self._recent_log_files(limit=max_files):
            try:
                lines = log_path.read_text(errors="replace").splitlines()
            except Exception:
                continue

            for idx in range(len(lines) - 1, -1, -1):
                line = lines[idx]
                if session_marker not in line and alt_session_marker not in line:
                    continue
                if "service=llm" not in line or "error=" not in line:
                    continue

                _, _, payload = line.partition(" error=")
                error_payload = None
                combined = payload
                for next_idx in range(idx, min(idx + 8, len(lines))):
                    if next_idx > idx:
                        combined += "\n" + lines[next_idx]
                    try:
                        error_payload, _ = json.JSONDecoder().raw_decode(combined)
                        break
                    except Exception:
                        continue
                if error_payload is None:
                    continue

                parsed = self._parse_rate_limit_payload(error_payload)
                if not parsed:
                    continue

                provider_match = provider_re.search(line)
                model_match = model_re.search(line)
                parsed.update(
                    {
                        "session_id": session_id,
                        "provider_id": provider_match.group(1) if provider_match else None,
                        "model_id": model_match.group(1) if model_match else None,
                        "log_path": str(log_path),
                    }
                )
                return parsed
        return None

    @staticmethod
    def _workspace_query(repo_path: Optional[str]) -> dict:
        return {"directory": repo_path} if repo_path else {}

    def create_session(
        self,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        permission: Optional[list] = None,
        variant: Optional[str] = None,
    ) -> str:
        payload = {}
        if model_id:
            pid = provider_id or (settings.opencode_provider if "/" not in model_id else None)
            mid = model_id
            if "/" in model_id:
                parts = model_id.split("/", 1)
                if pid and parts[0] == pid:
                    mid = parts[1]
                elif not pid:
                    pid, mid = parts
            if pid:
                payload["model"] = {"providerID": pid, "modelID": mid}
            else:
                payload["modelID"] = mid
        if permission:
            payload["permission"] = permission
        response = self._request("POST", "/session", json=payload, params=self._workspace_query(self._repo_path))
        return response.json()["id"]

    def delete_session(self, session_id: str) -> None:
        try:
            self._request("DELETE", f"/session/{session_id}", timeout=5.0, retries=2, retry_delay=0.5)
        except Exception:
            pass

    def list_providers(self, repo_path: Optional[str] = None) -> Dict[str, Any]:
        response = self._request("GET", "/provider", params=self._workspace_query(repo_path))
        return response.json()

    def list_provider_auth(self, repo_path: Optional[str] = None) -> Dict[str, Any]:
        response = self._request("GET", "/provider/auth", params=self._workspace_query(repo_path))
        return response.json()

    def authorize_provider_oauth(
        self,
        provider_id: str,
        method_index: int,
        repo_path: Optional[str] = None,
        inputs: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"method": method_index}
        if inputs:
            payload["inputs"] = inputs
        response = self._request(
            "POST",
            f"/provider/{provider_id}/oauth/authorize",
            params=self._workspace_query(repo_path),
            json=payload,
        )
        return response.json()

    def wait_for_provider_connection(
        self,
        provider_id: str,
        repo_path: Optional[str] = None,
        timeout: int = 300,
        poll_interval: int = 2,
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                providers = self.list_providers(repo_path=repo_path)
            except Exception:
                providers = {}
            if provider_id in (providers.get("connected") or []):
                return True
            time.sleep(poll_interval)
        return False

    def get_messages(self, session_id: str) -> list:
        response = self._request("GET", f"/session/{session_id}/message")
        return response.json()

    def _send_parts(
        self,
        session_id: str,
        parts: list,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        payload: dict = {"parts": parts}
        if model_id:
            pid = settings.opencode_provider if "/" not in model_id else None
            mid = model_id
            if "/" in model_id:
                pid, mid = model_id.split("/", 1)
            if pid:
                payload["model"] = {"providerID": pid, "modelID": mid}
            else:
                payload["modelID"] = mid
        effective_variant = _effective_variant(variant)
        if effective_variant:
            payload["variant"] = effective_variant
        self._request("POST", f"/session/{session_id}/message", json=payload, timeout=3600.0)
        return {}

    def send_message(
        self,
        session_id: str,
        content: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        return self._send_parts(
            session_id,
            [{"type": "text", "text": content}],
            model_id=model_id,
            variant=variant,
        )

    def send_message_split(
        self,
        session_id: str,
        stable_prefix: str,
        volatile_tail: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        return self.send_message(session_id, f"{stable_prefix}{volatile_tail}", model_id=model_id, variant=variant)

    def send_message_and_get_user_info(
        self,
        session_id: str,
        content: str,
        timeout: int = 120,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.send_message(session_id, content, model_id=model_id, variant=variant)
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = self.get_messages(session_id)
                for msg in reversed(messages):
                    info = msg.get("info", {})
                    if info.get("role") != "user":
                        continue
                    parts = msg.get("parts", [])
                    if any(p.get("type") == "text" and p.get("text") == content for p in parts):
                        return info
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError(f"Failed to locate user message metadata for: {content[:50]}...")

    def init_session(self, session_id: str, message_id: str, provider_id: str, model_id: str) -> None:
        payload = {"messageID": message_id, "providerID": provider_id, "modelID": model_id}
        response = self._request("POST", f"/session/{session_id}/init", json=payload, timeout=3600.0)
        response.raise_for_status()

    @staticmethod
    def _extract_event(messages: list) -> Optional[Dict[str, Any]]:
        latest_user_created = 0
        for msg in messages:
            info = msg.get("info", {})
            if info.get("role") == "user":
                latest_user_created = max(latest_user_created, info.get("time", {}).get("created", 0))

        for msg in reversed(messages):
            info = msg.get("info", {})
            if info.get("role") != "assistant":
                continue
            created = info.get("time", {}).get("created", 0)
            if latest_user_created and created and created < latest_user_created:
                return None
            error = info.get("error")
            if not error:
                has_stop = any(
                    p.get("type") == "step-finish" and p.get("reason") == "stop"
                    for p in msg.get("parts", [])
                )
                if not has_stop:
                    continue
            text_parts = [p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"]
            result = "\n".join(text_parts)
            if error:
                return {"type": "error", "error": error, "result": result}
            return {"type": "completed", "result": result}
        return None

    def latest_completion(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            messages = self.get_messages(session_id)
        except Exception:
            return None
        return self._extract_event(messages)

    @staticmethod
    def _part_progress_key(msg: Dict[str, Any], part: Dict[str, Any]) -> str:
        part_id = part.get("id")
        if part_id:
            return f"id:{part_id}"
        created = msg.get("info", {}).get("time", {}).get("created", 0)
        return f"fingerprint:{created}:{json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)}"

    @classmethod
    def _progress_lines(cls, messages: list, seen_part_ids: Set[str]) -> list:
        lines = []
        latest_user_created = 0
        for msg in messages:
            info = msg.get("info", {})
            if info.get("role") == "user":
                latest_user_created = max(latest_user_created, info.get("time", {}).get("created", 0))

        for msg in messages:
            info = msg.get("info", {})
            if info.get("role") != "assistant":
                continue
            created = info.get("time", {}).get("created", 0)
            if latest_user_created and created and created < latest_user_created:
                continue

            for part in msg.get("parts", []):
                part_key = cls._part_progress_key(msg, part)
                if part_key in seen_part_ids:
                    continue
                seen_part_ids.add(part_key)

                ptype = part.get("type")
                if ptype == "reasoning":
                    text = (part.get("text") or "").strip()
                    if text:
                        lines.append(f"reasoning: {text.splitlines()[0][:180]}")
                    else:
                        lines.append("reasoning: updated")
                elif ptype == "text":
                    text = (part.get("text") or "").strip()
                    if text:
                        lines.append(f"text: {text.splitlines()[0][:180]}")
                elif ptype == "tool":
                    tool = part.get("tool", "?")
                    state = part.get("state", {}) or {}
                    status = state.get("status", "started")
                    title = state.get("title") or state.get("input", {}).get("description") or ""
                    suffix = f" - {title}" if title else ""
                    lines.append(f"tool[{tool}] {status}{suffix}")
                elif ptype == "step-start":
                    lines.append("step: started")
                elif ptype == "step-finish":
                    reason = part.get("reason", "unknown")
                    lines.append(f"step: finished ({reason})")
        return lines

    def poll_completion(
        self,
        session_id: str,
        timeout: int = 600,
        on_update: Optional[Callable[[str], None]] = None,
        stalled_after_recovery_seconds: Optional[int] = None,
        stalled_no_progress_seconds: Optional[int] = None,
        rate_limit_check_interval_seconds: int = 3,
    ) -> Dict[str, Any]:
        start_time = time.time()
        seen_part_ids: Set[str] = set()
        pending_tool_started_at: Dict[str, float] = {}
        pending_tool_last_emit_at: Dict[str, float] = {}
        saw_transport_error = False
        post_recovery_progress_at: Optional[float] = None
        last_progress_at = start_time
        last_rate_limit_check_at = 0.0
        stalled_after_recovery_seconds = (
            int(stalled_after_recovery_seconds)
            if stalled_after_recovery_seconds is not None
            else max(60, int(settings.opencode_stalled_no_progress_seconds or 900))
        )
        stalled_no_progress_seconds = (
            int(stalled_no_progress_seconds)
            if stalled_no_progress_seconds is not None
            else max(60, int(settings.opencode_stalled_no_progress_seconds or 900))
        )

        while time.time() - start_time < timeout:
            now = time.time()
            try:
                messages = self.get_messages(session_id)
            except Exception:
                saw_transport_error = True
                messages = []
            else:
                if saw_transport_error and post_recovery_progress_at is None:
                    post_recovery_progress_at = now
                    if on_update:
                        on_update("connection: recovered after transient transport error")

            if on_update and messages:
                progress_lines = self._progress_lines(messages, seen_part_ids)
                for line in progress_lines:
                    on_update(line)
                if progress_lines:
                    post_recovery_progress_at = now
                    last_progress_at = now
                active_pending = set()
                for msg in messages:
                    info = msg.get("info", {})
                    if info.get("role") != "assistant":
                        continue
                    for part in msg.get("parts", []):
                        if part.get("type") != "tool":
                            continue
                        state = part.get("state", {}) or {}
                        if state.get("status") != "pending":
                            continue
                        tool = part.get("tool", "?")
                        title = state.get("title") or state.get("input", {}).get("description") or ""
                        key = f"{tool}:{title}"
                        active_pending.add(key)
                        started = pending_tool_started_at.setdefault(key, now)
                        last_emit = pending_tool_last_emit_at.get(key, 0.0)
                        if now - started >= 15 and now - last_emit >= 15:
                            suffix = f" - {title}" if title else ""
                            on_update(f"waiting: tool[{tool}] pending for {int(now - started)}s{suffix}")
                            pending_tool_last_emit_at[key] = now
                for key in list(pending_tool_started_at):
                    if key not in active_pending:
                        pending_tool_started_at.pop(key, None)
                        pending_tool_last_emit_at.pop(key, None)

            event = self._extract_event(messages) if messages else None
            if event:
                return event

            if now - last_rate_limit_check_at >= rate_limit_check_interval_seconds:
                last_rate_limit_check_at = now
                rate_limit = self.detect_rate_limit_issue(session_id)
                if rate_limit:
                    if on_update:
                        retry_after = rate_limit.get("retry_after_seconds")
                        provider = rate_limit.get("provider_id") or "provider"
                        model = rate_limit.get("model_id") or "model"
                        details = f"retry after {retry_after}s" if retry_after else "no retry-after provided"
                        on_update(f"rate-limit: {provider}/{model} hit {rate_limit.get('raw_type')} ({details})")
                    return {
                        "type": "rate_limited",
                        "result": "",
                        "reason": rate_limit.get("message") or "provider/model rate limit reached",
                        "rate_limit": rate_limit,
                    }

            if post_recovery_progress_at is not None and now - post_recovery_progress_at >= stalled_after_recovery_seconds:
                return {
                    "type": "stalled_after_recovery",
                    "result": "",
                    "reason": f"no session progress for {int(now - post_recovery_progress_at)}s after transport recovery",
                }
            if now - last_progress_at >= stalled_no_progress_seconds:
                return {
                    "type": "stalled_no_progress",
                    "result": "",
                    "reason": f"no session progress for {int(now - last_progress_at)}s",
                }
            time.sleep(3)

        return {"type": "timeout", "result": ""}

    def send_prompt(
        self,
        session_id: str,
        prompt: str,
        model_id: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> str:
        self.send_message(session_id, prompt, model_id=model_id, variant=variant)
        return session_id

    def poll_events(self, session_id: str, request_id: str, timeout: int = 600) -> Dict[str, Any]:
        return self.poll_completion(session_id, timeout)

    def analyze_session_tokens(self, session_id: str) -> Dict[str, Any]:
        try:
            messages = self.get_messages(session_id)
        except Exception:
            return {"session_id": session_id, "assistant_messages": 0,
                    "main_model_tokens": {}, "small_model_tokens": {}, "other_model_tokens": {}, "total_tokens": {}}

        configured_main = settings.opencode_model or ""
        configured_small = settings.opencode_small_model or ""

        def empty_bucket() -> Dict[str, int]:
            return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0}

        def add_usage(bucket: Dict[str, int], tokens: Dict[str, Any]) -> None:
            bucket["input"] += int(tokens.get("input", 0) or 0)
            bucket["output"] += int(tokens.get("output", 0) or 0)
            bucket["reasoning"] += int(tokens.get("reasoning", 0) or 0)
            bucket["cache_read"] += _cache_tokens(tokens, "read")
            bucket["cache_write"] += _cache_tokens(tokens, "write")
            total = tokens.get("total")
            if total is None:
                total = int(tokens.get("input", 0) or 0) + int(tokens.get("output", 0) or 0) + int(tokens.get("reasoning", 0) or 0)
            bucket["total"] += int(total or 0)

        main_bucket = empty_bucket()
        small_bucket = empty_bucket()
        other_bucket = empty_bucket()
        total_bucket = empty_bucket()
        assistant_messages = 0
        by_model: Dict[str, Dict[str, int]] = {}

        for msg in messages:
            if _message_role(msg) != "assistant":
                continue
            assistant_messages += 1
            tokens = _message_tokens(msg)
            provider_id, model_id = _message_model(msg)
            full_model = f"{provider_id}/{model_id}" if provider_id else model_id

            add_usage(total_bucket, tokens)
            model_bucket = by_model.setdefault(full_model, empty_bucket())
            add_usage(model_bucket, tokens)

            if full_model == configured_main:
                add_usage(main_bucket, tokens)
            elif full_model == configured_small:
                add_usage(small_bucket, tokens)
            else:
                add_usage(other_bucket, tokens)

        return {
            "session_id": session_id,
            "assistant_messages": assistant_messages,
            "main_model": configured_main,
            "small_model": configured_small,
            "main_model_tokens": main_bucket,
            "small_model_tokens": small_bucket,
            "other_model_tokens": other_bucket,
            "total_tokens": total_bucket,
            "by_model": by_model,
        }

    def analyze_session_retrospect(self, session_id: str) -> Dict[str, Any]:
        try:
            messages = self.get_messages(session_id)
        except Exception:
            return {"session_id": session_id, "hints": [], "observations": []}

        texts: List[str] = []
        tool_count = 0
        patch_count = 0
        tool_fingerprints: Dict[str, Dict[str, Any]] = {}
        for msg in messages:
            for part in msg.get("parts", []):
                ptype = part.get("type")
                if ptype in {"text", "reasoning"}:
                    text = (part.get("text") or "").strip()
                    if text:
                        texts.append(text)
                elif ptype == "tool":
                    tool_count += 1
                    tool = str(part.get("tool") or "?")
                    state = part.get("state", {}) or {}
                    title = (state.get("title") or state.get("input", {}).get("description") or "").strip()
                    key = f"{tool}|{title}"
                    entry = tool_fingerprints.setdefault(key, {"tool": tool, "title": title, "count": 0})
                    entry["count"] += 1
                elif ptype == "patch":
                    patch_count += 1

        blob = "\n".join(texts).lower()
        hints: List[str] = []
        observations: List[str] = []
        compile_facts: List[str] = []

        def add_hint(condition: bool, text: str) -> None:
            if condition and text not in hints:
                hints.append(text)

        def add_obs(condition: bool, text: str) -> None:
            if condition and text not in observations:
                observations.append(text)

        def add_compile_fact(text: str) -> None:
            cleaned = " ".join(text.strip().split())
            if cleaned and cleaned not in compile_facts:
                compile_facts.append(cleaned)

        fact_patterns = [
            r"[^.\n]*\breturns?\b[^.\n]*",
            r"[^.\n]*\bis an interface\b[^.\n]*",
            r"[^.\n]*\bno-arg constructor\b[^.\n]*",
            r"[^.\n]*\bstring-constant interface\b[^.\n]*",
            r"[^.\n]*\bscale-sensitive\b[^.\n]*",
            r"[^.\n]*\bexact enum constant\b[^.\n]*",
            r"[^.\n]*\bassertequals is ambiguous\b[^.\n]*",
        ]
        for text in texts:
            lowered = text.lower()
            if "compile" not in lowered and "mismatch" not in lowered and "confirmed" not in lowered and "assert" not in lowered:
                continue
            for pattern in fact_patterns:
                for match in re.findall(pattern, text, flags=re.IGNORECASE):
                    add_compile_fact(match.rstrip(" ."))

        add_hint(
            ("mockito" in blob or "byte-buddy" in blob or "concrete class" in blob),
            "Front-load compile-safe test construction: avoid mocking concrete classes and prefer stubs/proxies/manual object construction when Mockito/runtime support is weak.",
        )
        add_hint(
            ("jar" in blob or "maven repository" in blob or "decompile" in blob or ".class" in blob),
            "Strengthen source-of-truth lookup order: prefer current repo source and sibling API repos before jars, Maven cache, or decompiled artifacts.",
        )
        add_hint(
            ("long vs `int`" in blob or "long vs int" in blob or "enum constant" in blob or "scale6decimal" in blob),
            "Verify exact numeric types, enum constants, and value-object APIs before writing assertions or helpers.",
        )
        add_hint(
            ("failifnotests=false" in blob or "no matching tests" in blob or "surefire fails when a module has no matching tests" in blob),
            "Use repo-compatible Maven commands up front, including -DfailIfNoTests=false when multi-module test execution can be blocked by empty upstream modules.",
        )
        add_hint(
            ("surviving mutants" in blob or "mutation" in blob),
            "Bias first-round tests toward mutation resistance: assert exact request/query fields, inclusion/exclusion, branch pairs, and mutated object state.",
        )
        add_hint(
            ("all requested test files" in blob or "write all 4 test files" in blob or "batch" in blob),
            "In batch mode, write all requested test files first, then run one shared validation pass instead of per-file loops.",
        )

        add_obs(tool_count > 80, f"Session used many tool steps ({tool_count}), suggesting high exploration cost.")
        add_obs(patch_count > 8, f"Session required many patch rounds ({patch_count}), suggesting late discovery of constraints.")
        add_obs("surviving mutants" in blob, "Session entered mutation-driven repair loops after initial compile/test success.")
        add_obs("mockito itself is broken" in blob or "cannot mock this class" in blob,
                "Session spent time recovering from an invalid mocking strategy.")

        repeated_tools = [
            {"tool": item["tool"], "title": item["title"], "count": int(item["count"] or 0)}
            for item in sorted(tool_fingerprints.values(),
                               key=lambda item: (-int(item["count"] or 0), item["tool"], item["title"]))
            if int(item["count"] or 0) >= 3
        ]
        if repeated_tools:
            top = repeated_tools[0]
            tool_name = top.get("tool") or "tool"
            title = (top.get("title") or "").strip()
            repeated_desc = f"`{tool_name}`" + (f" ({title})" if title else "")
            add_obs(True, f"Session repeated the same tool pattern {top.get('count', 0)}× via {repeated_desc}.")
            add_hint(True, "When a symbol/path/command was already resolved earlier in the run, reuse the cached context artifacts before repeating the same read/grep/bash lookup sequence.")

        return {
            "session_id": session_id,
            "hint_count": len(hints),
            "hints": hints,
            "compile_facts": compile_facts,
            "observations": observations,
            "tool_count": tool_count,
            "patch_count": patch_count,
            "repeated_tools": repeated_tools,
        }
