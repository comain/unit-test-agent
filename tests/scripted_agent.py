from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable


def _token_totals(input_tokens: int = 20, output_tokens: int = 5) -> dict[str, int]:
    total = input_tokens + output_tokens
    return {
        "input": input_tokens,
        "output": output_tokens,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": total,
    }


@dataclass
class ScriptedAgentClient:
    """Deterministic test double for the OpenCode client surface.

    E2E tests use this to exercise UTA's session, generation, repair, usage,
    and retrospect plumbing without depending on a live agent backend.
    """

    repo_path: str
    events: Iterable[dict[str, Any] | str]
    token_usage: dict[str, Any] | None = None
    retrospect: dict[str, Any] | None = None
    sessions: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._events: Deque[dict[str, Any] | str] = deque(self.events)

    def create_session(self, model_id=None, provider_id=None):
        session_id = f"scripted-session-{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        self.messages.append(
            {
                "type": "create_session",
                "session_id": session_id,
                "model_id": model_id,
                "provider_id": provider_id,
            }
        )
        return session_id

    def open_session(self, model_id=None, permissions=None):
        return self.create_session(model_id=model_id)

    def send_message(self, session_id, content, model_id=None):
        self.messages.append(
            {
                "type": "send_message",
                "session_id": session_id,
                "content": content,
                "model_id": model_id,
            }
        )
        return {}

    def send_message_split(self, session_id, stable_prefix, volatile_tail, model_id=None):
        self.messages.append(
            {
                "type": "send_message_split",
                "session_id": session_id,
                "stable_prefix": stable_prefix,
                "volatile_tail": volatile_tail,
                "content": f"{stable_prefix}{volatile_tail}",
                "model_id": model_id,
            }
        )
        return {}

    def poll_completion(self, session_id, timeout=600, on_update=None):
        if not self._events:
            return {"type": "completed", "result": ""}
        event = self._events.popleft()
        if isinstance(event, str):
            event = {"type": "completed", "result": event}
        if on_update:
            on_update({"type": "scripted_update", "session_id": session_id})
        return dict(event)

    def run_node(self, *, session_id, phase, prompt, timeout, **kwargs):
        self.messages.append(
            {
                "type": "run_node",
                "session_id": session_id,
                "phase": phase,
                "content": prompt,
                "model_id": kwargs.get("model_id"),
            }
        )
        return self.poll_completion(session_id, timeout=timeout)

    def analyze_session_tokens(self, session_id):
        usage = self.token_usage or {
            "main_model_tokens": _token_totals(),
            "small_model_tokens": _token_totals(0, 0),
            "other_model_tokens": _token_totals(0, 0),
            "total_tokens": _token_totals(),
        }
        return {"session_id": session_id, **usage}

    def analyze_session_retrospect(self, session_id):
        return self.retrospect or {"session_id": session_id, "hints": ["scripted agent completed"]}


def scripted_agent_factory(
    *events: dict[str, Any] | str,
    token_usage: dict[str, Any] | None = None,
    retrospect: dict[str, Any] | None = None,
):
    clients: list[ScriptedAgentClient] = []

    def factory(repo_path: str) -> ScriptedAgentClient:
        client = ScriptedAgentClient(
            repo_path=repo_path,
            events=list(events),
            token_usage=token_usage,
            retrospect=retrospect,
        )
        clients.append(client)
        return client

    factory.clients = clients  # type: ignore[attr-defined]
    return factory
