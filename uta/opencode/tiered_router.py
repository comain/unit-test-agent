"""Provider-chain routing for OpenCode sessions.

The provider chain is the single source of model selection. Compile-fix,
coverage, mutation, planning, and generation phases use the same selected
provider/model candidate.

Usage:
    model = effective_model("compile_fix")
    session_id = client.create_session(model_id=model)
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from uta.config import settings

_DEFAULT_COOLDOWN_SECONDS = 120
_model_api_cache: Dict[str, Tuple[float, Optional[Set[str]]]] = {}


@dataclass(frozen=True)
class ProviderCandidate:
    """One ordered OpenCode provider/model candidate."""

    provider: str
    model: str
    index: int


def parse_provider_chain(raw: str) -> List[ProviderCandidate]:
    """Parse ``provider:model,model;provider:model`` preserving valid order."""
    candidates: List[ProviderCandidate] = []
    for provider_group in (raw or "").split(";"):
        group = provider_group.strip()
        if not group or ":" not in group:
            continue
        provider, raw_models = group.split(":", 1)
        provider = provider.strip()
        if not provider:
            continue
        for raw_model in raw_models.split(","):
            model = raw_model.strip()
            if not model:
                continue
            candidates.append(
                ProviderCandidate(
                    provider=provider,
                    model=model,
                    index=len(candidates),
                )
            )
    return candidates


def parse_provider_tokens(raw: str) -> Dict[str, str]:
    """Parse semicolon-separated ``provider.token=value`` entries."""
    tokens: Dict[str, str] = {}
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if not key.endswith(".token") or not value:
            continue
        provider = key[: -len(".token")].strip()
        if provider:
            tokens[provider] = value
    return tokens


def parse_provider_base_urls(raw: str) -> Dict[str, str]:
    """Parse semicolon-separated provider base URL entries."""
    urls: Dict[str, str] = {}
    if not isinstance(raw, str):
        return urls
    accepted_suffixes = (".base_url", ".baseURL", ".base-url", ".baseurl")
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip().rstrip("/")
        if not value:
            continue
        provider = ""
        for suffix in accepted_suffixes:
            if key.endswith(suffix):
                provider = key[: -len(suffix)].strip()
                break
        if provider:
            urls[provider] = value
    return urls


def _setting_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def provider_token_statuses(
    chain: Iterable[ProviderCandidate],
    tokens: Dict[str, str],
) -> Dict[str, str]:
    """Return token presence by provider without exposing token values."""
    statuses: Dict[str, str] = {}
    for candidate in chain:
        if candidate.provider in statuses:
            continue
        statuses[candidate.provider] = (
            "configured" if tokens.get(candidate.provider) else "missing"
        )
    return statuses


def provider_candidates(
    *,
    fallback_enabled: Optional[bool] = None,
) -> List[ProviderCandidate]:
    """Return active provider/model candidates for the current config."""
    candidates = parse_provider_chain(settings.opencode_provider_chain)
    if not candidates:
        return []
    enabled = (
        settings.opencode_provider_fallback_enabled
        if fallback_enabled is None
        else fallback_enabled
    )
    if not enabled:
        return candidates[:1]
    return candidates


def parse_model_list_response(payload: Any) -> Set[str]:
    """Extract model ids from common OpenAI-compatible model-list shapes."""
    raw_items: Any
    if isinstance(payload, dict):
        raw_items = payload.get("data")
        if raw_items is None:
            raw_items = payload.get("models")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return set()

    model_ids: Set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                model_ids.add(model_id.strip())
    return model_ids


def reset_model_availability_cache() -> None:
    _model_api_cache.clear()


def _provider_api_key(provider_id: str) -> str:
    tokens = parse_provider_tokens(settings.opencode_provider_tokens)
    token = tokens.get(provider_id, "")
    if token:
        return token
    if provider_id == "deepseek":
        return _setting_text(settings.deepseek_api_key)
    if provider_id == "openai":
        return _setting_text(settings.openai_api_key)
    if provider_id == "openrouter":
        return _setting_text(settings.openrouter_api_key)
    if provider_id == "tencent":
        return _setting_text(settings.tencent_api_key)
    if provider_id == "google":
        return _setting_text(settings.gemini_api_key)
    return ""


def _model_api_headers(provider_id: str) -> Dict[str, str]:
    token = _provider_api_key(provider_id)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _model_api_url(provider_id: str) -> Optional[str]:
    configured_urls = parse_provider_base_urls(settings.opencode_provider_base_urls)
    base_url = configured_urls.get(provider_id, "")
    if not base_url and provider_id in {"openai", "token-pool"}:
        base_url = _setting_text(settings.openai_base_url)
    elif not base_url and provider_id == "tencent":
        base_url = _setting_text(settings.tencent_base_url)
    elif not base_url and provider_id == "ollama":
        ollama_host = _setting_text(settings.ollama_host)
        base_url = f"{ollama_host.rstrip('/')}/v1" if ollama_host else ""
    elif not base_url and provider_id not in {
        "cursor",
        "deepseek",
        "google",
        "openrouter",
    }:
        base_url = _setting_text(settings.openai_base_url)
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _provider_available_models(
    provider_id: str,
    *,
    http_get: Callable[..., Any] = httpx.get,
) -> Optional[Set[str]]:
    url = _model_api_url(provider_id)
    if not url:
        return None

    ttl = max(0, int(settings.opencode_model_api_cache_seconds or 0))
    cached = _model_api_cache.get(url)
    now = time.time()
    if cached and ttl > 0 and now - cached[0] < ttl:
        return cached[1]

    try:
        headers = _model_api_headers(provider_id)
        kwargs: Dict[str, Any] = {"timeout": settings.opencode_model_api_timeout_seconds}
        if headers:
            kwargs["headers"] = headers
        response = http_get(url, **kwargs)
        status_code = getattr(response, "status_code", 200)
        if int(status_code) >= 400:
            raise RuntimeError(f"model API returned HTTP {status_code}")
        available = parse_model_list_response(response.json())
    except Exception:
        _model_api_cache[url] = (now, None)
        return None
    _model_api_cache[url] = (now, available)
    return available


def _candidate_model_ids(candidate: ProviderCandidate) -> Set[str]:
    ids = {candidate.model}
    if candidate.model.startswith(f"{candidate.provider}/"):
        ids.add(candidate.model.split("/", 1)[1])
    return ids


def available_provider_candidates(
    *,
    fallback_enabled: Optional[bool] = None,
    http_get: Callable[..., Any] = httpx.get,
) -> List[ProviderCandidate]:
    """Return candidates after non-fatal model API availability filtering."""
    candidates = provider_candidates(fallback_enabled=fallback_enabled)
    filtered: List[ProviderCandidate] = []
    by_provider: Dict[str, Optional[Set[str]]] = {}
    for candidate in candidates:
        if candidate.provider not in by_provider:
            by_provider[candidate.provider] = _provider_available_models(
                candidate.provider,
                http_get=http_get,
            )
        available = by_provider[candidate.provider]
        if available is None or _candidate_model_ids(candidate) & available:
            filtered.append(candidate)
    return filtered


class ModelHealthTracker:
    """Tracks per-model rate-limit cooldowns.

    Thread-safety: reads/writes to a dict are GIL-protected in CPython;
    sufficient for our use case (one writer per rate-limit event, many readers).
    """

    def __init__(self) -> None:
        self._unhealthy_until: Dict[str, float] = {}

    def mark_rate_limited(
        self,
        model_id: str,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        cooldown = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else _DEFAULT_COOLDOWN_SECONDS
        self._unhealthy_until[model_id] = time.time() + cooldown

    def is_healthy(self, model_id: str) -> bool:
        until = self._unhealthy_until.get(model_id)
        if until is None:
            return True
        if time.time() >= until:
            del self._unhealthy_until[model_id]
            return True
        return False

    def reset(self) -> None:
        self._unhealthy_until.clear()


_tracker = ModelHealthTracker()


def cheap_model_for_phase(phase: str) -> Optional[str]:
    """Return None because cheap-tier overrides are no longer model inputs."""
    return None


def effective_model(phase: str, *, fallback: Optional[str] = None) -> str:
    """Return the model ID to use for ``phase``.

    The fallback argument is retained for caller compatibility. Provider
    fallback is handled by task stop/resume, not by switching models inside the
    same running turn.
    """
    configured_model = settings.opencode_model
    all_candidates = provider_candidates(fallback_enabled=True)
    if any(candidate.model == configured_model for candidate in all_candidates):
        return configured_model
    candidates = available_provider_candidates(fallback_enabled=False)
    if not candidates:
        candidates = provider_candidates(fallback_enabled=False)
    if candidates:
        return candidates[0].model
    return configured_model
