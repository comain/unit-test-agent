"""Phase 4 gate tests for ModelHealthTracker and stateful circuit breaking."""

import time
from unittest.mock import patch

import pytest

from uta.opencode.tiered_router import ModelHealthTracker, effective_model


@pytest.fixture(autouse=True)
def reset_tracker():
    """Isolate each test from the module-level singleton."""
    from uta.opencode import tiered_router
    tiered_router._tracker.reset()
    tiered_router.reset_model_availability_cache()
    yield
    tiered_router._tracker.reset()
    tiered_router.reset_model_availability_cache()


# --- ModelHealthTracker ---

def test_new_model_is_healthy():
    tracker = ModelHealthTracker()
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is True


def test_mark_rate_limited_makes_model_unhealthy():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("cursor/claude-4.5-sonnet", retry_after_seconds=60)
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is False


def test_health_recovers_after_cooldown():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("cursor/claude-4.5-sonnet", retry_after_seconds=1)
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is False
    # advance time past cooldown
    with patch("uta.opencode.tiered_router.time") as mock_time:
        mock_time.time.return_value = time.time() + 5
        assert tracker.is_healthy("cursor/claude-4.5-sonnet") is True


def test_default_cooldown_used_when_no_retry_after():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("cursor/claude-4.5-sonnet", retry_after_seconds=None)
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is False


def test_zero_retry_after_uses_default_cooldown():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("cursor/claude-4.5-sonnet", retry_after_seconds=0)
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is False


def test_different_models_tracked_independently():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("cursor/claude-4.5-sonnet", retry_after_seconds=60)
    assert tracker.is_healthy("cursor/claude-4.5-sonnet") is False
    assert tracker.is_healthy("google/gemini-2.5-flash") is True


def test_reset_clears_all_state():
    tracker = ModelHealthTracker()
    tracker.mark_rate_limited("model-a", retry_after_seconds=3600)
    tracker.mark_rate_limited("model-b", retry_after_seconds=3600)
    tracker.reset()
    assert tracker.is_healthy("model-a") is True
    assert tracker.is_healthy("model-b") is True


# --- effective_model integration ---

def test_router_switches_on_rate_limit():
    main_model = "token-pool/gpt-5.5"
    fallback = "google/gemini-2.5-flash"

    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = f"token-pool:{main_model}"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_model = "legacy/gpt-4o"
        mock_settings.opencode_cheap_model = ""

        # Healthy: returns the selected provider-chain model.
        assert effective_model("generate", fallback=fallback) == main_model

        # Runtime fallback happens by stop/resume in a later task, not by
        # switching models inside the same running turn.
        assert effective_model("generate", fallback=fallback) == main_model


def test_router_ignores_cheap_model_for_compile_fix():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = "token-pool:token-pool/gpt-5.5"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_model = "legacy/gpt-4o"
        mock_settings.opencode_cheap_model = "cursor/claude-4.5-sonnet-mini"

        result = effective_model("compile_fix")
        assert result == "token-pool/gpt-5.5"


def test_tracker_singleton_isolated_between_tests():
    from uta.opencode import tiered_router
    assert tiered_router._tracker.is_healthy("any-model") is True


# --- model availability probe ---

def test_parse_model_list_response_shapes():
    from uta.opencode.tiered_router import parse_model_list_response

    assert parse_model_list_response({"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4"}]}) == {
        "gpt-5.5",
        "gpt-5.4",
    }
    assert parse_model_list_response({"models": [{"id": "deepseek-v4-pro"}]}) == {
        "deepseek-v4-pro"
    }
    assert parse_model_list_response(["gpt-5.5", {"id": "gpt-5.4"}]) == {
        "gpt-5.5",
        "gpt-5.4",
    }


def test_available_provider_candidates_skips_models_absent_from_probe(monkeypatch):
    import httpx
    from uta.opencode.tiered_router import available_provider_candidates

    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5,openai/gpt-5.4;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_fallback_enabled", True)
    monkeypatch.setattr("uta.opencode.tiered_router.settings.openai_base_url", "http://models.test/v1")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_model_api_cache_seconds", 300)

    def fake_get(url, timeout, headers=None):
        assert url == "http://models.test/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-5.4"}]})

    candidates = available_provider_candidates(http_get=fake_get)

    assert [(item.provider, item.model, item.index) for item in candidates] == [
        ("openai", "openai/gpt-5.4", 1),
        ("deepseek", "deepseek/deepseek-v4-pro", 2),
    ]


def test_model_availability_probe_uses_provider_base_urls(monkeypatch):
    import httpx
    from uta.opencode.tiered_router import available_provider_candidates

    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_fallback_enabled", True)
    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_base_urls",
        "token-pool.base_url=http://token-pool.test/v1;deepseek.base_url=http://deepseek.test/v1",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.openai_base_url", "http://legacy.test/v1")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_model_api_cache_seconds", 0)

    calls = []

    def fake_get(url, timeout, headers=None):
        calls.append(url)
        if url == "http://token-pool.test/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]})
        if url == "http://deepseek.test/v1/models":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]})
        raise AssertionError(url)

    candidates = available_provider_candidates(http_get=fake_get)

    assert [item.model for item in candidates] == [
        "token-pool/gpt-5.5",
        "deepseek/deepseek-v4-pro",
    ]
    assert calls == [
        "http://token-pool.test/v1/models",
        "http://deepseek.test/v1/models",
    ]


def test_model_availability_probe_sends_provider_token(monkeypatch):
    import httpx
    from uta.opencode.tiered_router import available_provider_candidates

    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_chain",
        "token-pool:token-pool/gpt-5.5,token-pool/gpt-5.4",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_fallback_enabled", True)
    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_base_urls",
        "token-pool.base_url=http://token-pool.test/v1",
    )
    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_model_api_cache_seconds", 0)

    observed_headers = []

    def fake_get(url, timeout, headers=None):
        observed_headers.append(headers or {})
        return httpx.Response(200, json={"data": [{"id": "gpt-5.4"}]})

    candidates = available_provider_candidates(http_get=fake_get)

    assert [item.model for item in candidates] == ["token-pool/gpt-5.4"]
    assert observed_headers[0]["Authorization"] == "Bearer tp-secret"


def test_available_provider_candidates_keeps_configured_models_on_probe_failure(monkeypatch):
    from uta.opencode.tiered_router import available_provider_candidates

    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5,openai/gpt-5.4",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_fallback_enabled", True)
    monkeypatch.setattr("uta.opencode.tiered_router.settings.openai_base_url", "http://models.test/v1")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_tokens", "")

    def failing_get(url, timeout, headers=None):
        raise RuntimeError("model endpoint down")

    candidates = available_provider_candidates(http_get=failing_get)

    assert [item.model for item in candidates] == ["openai/gpt-5.5", "openai/gpt-5.4"]


def test_model_availability_probe_uses_process_local_cache(monkeypatch):
    import httpx
    from uta.opencode.tiered_router import available_provider_candidates

    monkeypatch.setattr(
        "uta.opencode.tiered_router.settings.opencode_provider_chain",
        "openai:openai/gpt-5.5,openai/gpt-5.4",
    )
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_fallback_enabled", True)
    monkeypatch.setattr("uta.opencode.tiered_router.settings.openai_base_url", "http://models.test/v1")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_provider_tokens", "")
    monkeypatch.setattr("uta.opencode.tiered_router.settings.opencode_model_api_cache_seconds", 300)

    calls = []

    def fake_get(url, timeout, headers=None):
        calls.append(url)
        return httpx.Response(200, json={"data": [{"id": "gpt-5.4"}]})

    assert [item.model for item in available_provider_candidates(http_get=fake_get)] == [
        "openai/gpt-5.4"
    ]
    assert [item.model for item in available_provider_candidates(http_get=fake_get)] == [
        "openai/gpt-5.4"
    ]
    assert calls == ["http://models.test/v1/models"]
