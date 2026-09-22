"""Shared model configuration snapshot for CLI and CI repair task creation.

Manual selection is retained for legacy tasks. Discovery intentionally leaves
selection empty: the harness resolves and reports model/effort at execution,
including resumed tasks. Creation must log deferred selection, not a blank
"selected model" or an invented pin.
"""

from __future__ import annotations

from typing import Any, Dict

from uta.shared.config import settings


def opencode_config_snapshot() -> Dict[str, Any]:
    """Snapshot manual selection, or defer discovery to core at execution."""
    if settings.model_selection_config:
        # Keep the legacy shape without resolving or pinning a historic candidate.
        return {
            "opencode_model": settings.opencode_model,
            "opencode_provider": settings.opencode_provider,
            "opencode_provider_chain": [],
            "opencode_selected_provider": "",
            "opencode_selected_model": "",
            "opencode_candidate_index": None,
            "opencode_provider_tokens": {},
        }

    from agent_core.harness.tiered_router import (
        available_provider_candidates,
        opencode_model_id,
        parse_provider_chain,
        parse_provider_tokens,
        provider_candidates,
        provider_token_statuses,
    )

    chain = parse_provider_chain(settings.opencode_provider_chain)
    selected = available_provider_candidates(fallback_enabled=False)
    if not selected:
        selected = provider_candidates(fallback_enabled=False)
    candidate = selected[0] if selected else None
    return {
        "opencode_model": settings.opencode_model,
        "opencode_provider": settings.opencode_provider,
        "opencode_provider_chain": [
            {"provider": item.provider, "model": item.model, "index": item.index}
            for item in chain
        ],
        "opencode_selected_provider": candidate.provider if candidate else "",
        "opencode_selected_model": (
            opencode_model_id(candidate) if candidate else settings.opencode_model
        ),
        "opencode_candidate_index": candidate.index if candidate else None,
        "opencode_provider_tokens": provider_token_statuses(
            chain,
            parse_provider_tokens(settings.opencode_provider_tokens),
        ),
    }


__all__ = ["opencode_config_snapshot"]
