"""Agent-agnostic harness construction for durable test generation."""

from __future__ import annotations

from agent_core.harness import (
    Harness,
    HarnessSpec,
    SessionDiagnosticsProvider,
    create_configured_diagnostics_provider,
    create_configured_harness,
)

from uta.shared.config import settings


def _configured_spec(*, harness_name=None) -> HarnessSpec:
    options = settings.model_dump(reveal_secrets=True)
    # agent-core treats presence of this key as an explicit request for model
    # discovery. Empty UTA defaults must therefore be omitted, not forwarded.
    discovery_configured = bool(options.get("model_selection_config"))
    if not discovery_configured:
        options.pop("model_selection_config", None)
    if not discovery_configured and options.get("model_coding_index_min") is None:
        options.pop("model_coding_index_min", None)
    return HarnessSpec(
        name=str(harness_name or settings.agent_harness),
        # `reveal_secrets=True` because this dump is rebuilt into the harness's
        # own configuration, not shown to anyone. The default redacts provider
        # tokens to "***", which is a non-empty string the harness accepts and
        # then fails to authenticate with -- surfacing only as a 401 from the
        # provider several layers away.
        options=options,
        timeout_seconds=max(
            int(settings.opencode_planning_timeout_seconds or 0),
            int(settings.opencode_repair_timeout_seconds or 0),
            3600,
        ),
        cache_dir=settings.agent_cache_dir,
    )


def create_agent_harness() -> Harness:
    """Build the configured neutral harness used by ``agent_turn`` nodes."""
    return create_configured_harness(_configured_spec())


def create_agent_diagnostics_provider(
    *, harness_name=None, database_path=None
) -> SessionDiagnosticsProvider | None:
    """Resolve optional offline diagnostics without naming an implementation."""
    options = {"database_path": database_path} if database_path is not None else {}
    return create_configured_diagnostics_provider(
        _configured_spec(harness_name=harness_name), **options
    )


__all__ = ["create_agent_diagnostics_provider", "create_agent_harness"]
