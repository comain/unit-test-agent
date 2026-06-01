from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional


class ProviderRateLimitError(RuntimeError):
    def __init__(
        self,
        *,
        session_id: str,
        phase: str,
        rate_limit: Optional[Dict[str, Any]] = None,
        reason: str = "rate_limit",
    ):
        self.session_id = session_id
        self.phase = phase
        self.rate_limit = rate_limit or {}
        self.reason = reason
        super().__init__(self.output)

    @property
    def output(self) -> str:
        provider = self.rate_limit.get("provider_id") or "provider"
        model = self.rate_limit.get("model_id") or "model"
        message = self.rate_limit.get("message") or "Provider/model rate limit reached"
        retry_after = self.rate_limit.get("retry_after_seconds")
        output = f"{provider}/{model}: {message}"
        if retry_after:
            output += f" (retry after {retry_after}s)"
        return output


def raise_for_provider_fallback_event(
    *,
    event: Dict[str, Any],
    session_id: str,
    client: Any,
    phase: str,
) -> Dict[str, Any]:
    if event.get("type") == "rate_limited":
        rate_limit = event.get("rate_limit") or {}
        if not rate_limit:
            detect = getattr(client, "detect_rate_limit_issue", None)
            if callable(detect):
                rate_limit = detect(session_id) or {}
        raise ProviderRateLimitError(
            session_id=session_id,
            phase=phase,
            rate_limit=rate_limit,
            reason="rate_limit",
        )

    if not (event.get("type") == "error" and event.get("fallback_eligible")):
        return event
    error_obj = event.get("error") if isinstance(event.get("error"), dict) else {}
    data = error_obj.get("data") if isinstance(error_obj.get("data"), dict) else {}
    reason = event.get("fallback_reason") or "model_unavailable"
    raise ProviderRateLimitError(
        session_id=session_id,
        phase=phase,
        reason=reason,
        rate_limit={
            "provider_id": event.get("provider_id") or data.get("provider_id"),
            "model_id": event.get("model_id") or data.get("model_id"),
            "message": data.get("message") or error_obj.get("message") or reason,
            "status_code": data.get("statusCode") or data.get("status_code"),
            "raw_type": reason,
        },
    )


def poll_completion_with_task_guard(
    client: Any,
    session_id: str,
    *,
    timeout_seconds: int,
    phase: str,
    guard_state: Optional[Dict[str, Any]] = None,
    batch: Optional[Iterable[str]] = None,
    guard_before: Optional[Callable[[Dict[str, Any], list[str], str], Any]] = None,
    guard_after: Optional[Callable[[Dict[str, Any], Any], None]] = None,
) -> Dict[str, Any]:
    targets = list(batch or [])
    if not guard_state or not guard_before or not guard_after:
        return raise_for_provider_fallback_event(
            event=client.poll_completion(session_id, timeout=timeout_seconds),
            session_id=session_id,
            client=client,
            phase=phase,
        )
    snapshot = guard_before(guard_state, targets, phase)
    try:
        event = client.poll_completion(session_id, timeout=timeout_seconds)
        return raise_for_provider_fallback_event(
            event=event,
            session_id=session_id,
            client=client,
            phase=phase,
        )
    finally:
        guard_after(guard_state, snapshot)
