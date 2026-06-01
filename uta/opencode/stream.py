"""Pure JSONL parsing utilities for `opencode run --format json` output."""

import json
from typing import Dict, Any, List, Optional


_RATE_LIMIT_PHRASES = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "too many requests",
    "usage limit",
    "resource_exhausted",
    "free_quota_exhausted",
    "endpoint is inactive",
    "requires more credits",
    "add more credits",
)


class OpenCodeStreamParser:
    """Parses raw JSONL lines from `opencode run --format json`."""

    def parse_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Return parsed event dict or None for blank/malformed lines."""
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def extract_text(self, events: List[Dict[str, Any]]) -> str:
        """Concatenate text from all text events."""
        parts = []
        for ev in events:
            if ev.get("type") == "text":
                text = (ev.get("part") or {}).get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)

    def extract_tokens(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Return aggregate token dict from all step_finish events in the stream.

        OpenCode emits token-bearing assistant messages for intermediate
        ``tool-calls`` turns as well as the final ``stop`` turn. The in-memory
        stream accounting needs to aggregate all of them so it matches the
        persisted session/message totals.
        """
        total: Dict[str, Any] = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
            "total": 0,
        }
        saw_tokens = False
        for ev in events:
            if ev.get("type") != "step_finish":
                continue
            part = ev.get("part") or {}
            tokens = part.get("tokens") or {}
            if not tokens:
                continue
            saw_tokens = True
            total["input"] += int(tokens.get("input", 0) or 0)
            total["output"] += int(tokens.get("output", 0) or 0)
            total["reasoning"] += int(tokens.get("reasoning", 0) or 0)
            cache = tokens.get("cache") or {}
            total["cache"]["read"] += int(cache.get("read", 0) or 0)
            total["cache"]["write"] += int(cache.get("write", 0) or 0)
            tok_total = tokens.get("total")
            if tok_total is None:
                tok_total = (
                    int(tokens.get("input", 0) or 0)
                    + int(tokens.get("output", 0) or 0)
                    + int(tokens.get("reasoning", 0) or 0)
                )
            total["total"] += int(tok_total or 0)
        return total if saw_tokens else {}

    def count_patches(self, events: List[Dict[str, Any]]) -> int:
        """Count file-changing patch events in the stream.

        OpenCode versions differ here: some emit a dedicated ``patch`` part,
        while newer CLI streams expose the edit as a completed
        ``tool[apply_patch]`` event. Treat both as a successful patch signal.
        """
        count = 0
        for ev in events:
            etype = ev.get("type")
            part = ev.get("part") or {}
            ptype = part.get("type")
            if etype == "patch" or ptype == "patch":
                count += 1
                continue
            if etype == "tool_use" and part.get("tool") == "apply_patch":
                state = part.get("state") or {}
                output = str(state.get("output") or "")
                if state.get("status") == "completed" and "Success." in output:
                    count += 1
        return count

    def detect_completion(self, events: List[Dict[str, Any]]) -> Optional[str]:
        """Return "stop" if stream is done, else None."""
        for ev in reversed(events):
            if ev.get("type") == "step_finish":
                reason = (ev.get("part") or {}).get("reason")
                if reason == "stop":
                    return "stop"
        return None

    def detect_rate_limit(self, error_event: Dict[str, Any]) -> bool:
        """True if the error event represents a rate limit / quota exhaustion."""
        if error_event.get("type") != "error":
            return False
        error = error_event.get("error") or {}
        data = error.get("data") or {}
        message = str(data.get("message") or "").lower()
        status_code = data.get("statusCode")
        if status_code == 429:
            return True
        return any(phrase in message for phrase in _RATE_LIMIT_PHRASES)

    def detect_rate_limit_text(self, text: str) -> bool:
        """True if raw non-JSON process output looks like a rate limit / quota error."""
        lowered = (text or "").lower()
        if not lowered:
            return False
        return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)

    def progress_line(self, event: Dict[str, Any]) -> Optional[str]:
        """Return a single-line progress string for an event, or None if not worth logging."""
        etype = event.get("type")
        part = event.get("part") or {}
        if etype == "text":
            text = part.get("text", "").strip()
            if text:
                return f"text: {text.splitlines()[0][:180]}"
        elif etype == "reasoning":
            text = part.get("text", "").strip()
            if text:
                return f"reasoning: {text.splitlines()[0][:180]}"
            return "reasoning: updated"
        elif etype == "tool_use":
            tool = part.get("tool", "?")
            state = part.get("state") or {}
            status = state.get("status", "started")
            title = state.get("title") or (state.get("input") or {}).get("description") or ""
            suffix = f" - {title}" if title else ""
            return f"tool[{tool}] {status}{suffix}"
        elif etype == "step_start":
            return "step: started"
        elif etype == "step_finish":
            reason = part.get("reason", "unknown")
            return f"step: finished ({reason})"
        elif etype == "error":
            data = (event.get("error") or {}).get("data") or {}
            msg = str(data.get("message") or "unknown error")
            return f"error: {msg[:180]}"
        return None
