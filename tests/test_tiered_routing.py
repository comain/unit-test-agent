"""Tests for uta.opencode.tiered_router (token_opt_phase2 strategy H)."""

import pytest
from unittest.mock import patch


def test_parse_provider_chain_preserves_provider_and_model_order():
    from uta.opencode.tiered_router import parse_provider_chain

    chain = parse_provider_chain(
        "token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;"
        "openai:openai/gpt-5.5,openai/gpt-5.4;"
        "deepseek:deepseek/deepseek-v4-pro"
    )

    assert [(item.provider, item.model, item.index) for item in chain] == [
        ("token-pool", "token-pool/gpt-5.5", 0),
        ("token-pool", "token-pool/gpt-5.5-mini", 1),
        ("openai", "openai/gpt-5.5", 2),
        ("openai", "openai/gpt-5.4", 3),
        ("deepseek", "deepseek/deepseek-v4-pro", 4),
    ]


def test_parse_provider_chain_ignores_invalid_entries():
    from uta.opencode.tiered_router import parse_provider_chain

    chain = parse_provider_chain(
        "missing-colon;openai:openai/gpt-5.5,,openai/gpt-5.4;"
        "empty:;:model;deepseek:deepseek/deepseek-v4-pro"
    )

    assert [(item.provider, item.model, item.index) for item in chain] == [
        ("openai", "openai/gpt-5.5", 0),
        ("openai", "openai/gpt-5.4", 1),
        ("deepseek", "deepseek/deepseek-v4-pro", 2),
    ]


def test_provider_token_statuses_do_not_expose_token_values():
    from uta.opencode.tiered_router import (
        parse_provider_chain,
        parse_provider_tokens,
        provider_token_statuses,
    )

    chain = parse_provider_chain(
        "token-pool:token-pool/gpt-5.5;"
        "openai:openai/gpt-5.5;"
        "deepseek:deepseek/deepseek-v4-pro"
    )
    tokens = parse_provider_tokens(
        "token-pool.token=tp-secret;openai.token=openai-secret"
    )

    assert tokens == {"token-pool": "tp-secret", "openai": "openai-secret"}
    assert provider_token_statuses(chain, tokens) == {
        "token-pool": "configured",
        "openai": "configured",
        "deepseek": "missing",
    }
    assert "secret" not in repr(provider_token_statuses(chain, tokens))


def test_parse_provider_base_urls_accepts_provider_scoped_entries():
    from uta.opencode.tiered_router import parse_provider_base_urls

    urls = parse_provider_base_urls(
        "token-pool.base_url=https://token-pool.example/v1/;"
        "openai.baseURL=https://openai.example/v1;"
        "deepseek.base-url=https://deepseek.example/v1;"
        "bad.token=secret"
    )

    assert urls == {
        "token-pool": "https://token-pool.example/v1",
        "openai": "https://openai.example/v1",
        "deepseek": "https://deepseek.example/v1",
    }


def test_provider_candidates_use_first_chain_model_when_fallback_disabled():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = (
            "token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;"
            "openai:openai/gpt-5.5"
        )
        mock_settings.opencode_provider_fallback_enabled = False
        from uta.opencode.tiered_router import provider_candidates

        candidates = provider_candidates()

    assert [(item.provider, item.model, item.index) for item in candidates] == [
        ("token-pool", "token-pool/gpt-5.5", 0)
    ]


def test_provider_candidates_include_chain_when_fallback_enabled():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = (
            "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.5"
        )
        mock_settings.opencode_provider_fallback_enabled = True
        from uta.opencode.tiered_router import provider_candidates

        candidates = provider_candidates()

    assert [(item.provider, item.model, item.index) for item in candidates] == [
        ("token-pool", "token-pool/gpt-5.5", 0),
        ("openai", "openai/gpt-5.5", 1),
    ]


def test_cheap_model_override_is_ignored_for_compile_fix():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = "token-pool:token-pool/gpt-5.5"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_cheap_model = "ollama/qwen3:8b"
        from uta.opencode.tiered_router import cheap_model_for_phase

        assert cheap_model_for_phase("compile_fix") is None


def test_effective_model_uses_chain_for_compile_fix_even_when_cheap_model_set():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = "token-pool:token-pool/gpt-5.5"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_cheap_model = "ollama/qwen3:8b"
        mock_settings.opencode_model = "legacy/gpt-4o"
        from uta.opencode.tiered_router import effective_model

        assert effective_model("compile_fix") == "token-pool/gpt-5.5"


def test_effective_model_uses_chain_for_generation():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = "token-pool:token-pool/gpt-5.5"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_cheap_model = "ollama/qwen3:8b"
        mock_settings.opencode_model = "legacy/gpt-4o"
        from uta.opencode.tiered_router import effective_model

        assert effective_model("generate") == "token-pool/gpt-5.5"


def test_effective_model_uses_same_chain_model_for_all_phases():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = "token-pool:token-pool/gpt-5.5"
        mock_settings.opencode_provider_fallback_enabled = False
        mock_settings.opencode_cheap_model = "ollama/qwen3:8b"
        mock_settings.opencode_model = "legacy/gpt-4o"
        from uta.opencode.tiered_router import effective_model

        assert effective_model("generate") == "token-pool/gpt-5.5"
        assert effective_model("compile_fix") == "token-pool/gpt-5.5"
        assert effective_model("coverage_fix") == "token-pool/gpt-5.5"
        assert effective_model("mutation_fix") == "token-pool/gpt-5.5"


def test_effective_model_honors_task_selected_model_when_in_chain():
    with patch("uta.opencode.tiered_router.settings") as mock_settings:
        mock_settings.opencode_provider_chain = (
            "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.4"
        )
        mock_settings.opencode_provider_fallback_enabled = True
        mock_settings.opencode_model = "openai/gpt-5.4"
        from uta.opencode.tiered_router import effective_model

        assert effective_model("generate") == "openai/gpt-5.4"
