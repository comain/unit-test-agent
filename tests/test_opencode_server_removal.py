"""The run pipeline depends on no concrete harness.

Originally about OpenCodeServer. The probe seam these tests patch has since
moved from `_probe_openai_auth_ready_with_retry` -- which knew how to read a
provider's error payload -- to `_probe_harness_readiness`, which asks the
configured harness and gets a normalised answer back. The behaviour asserted is
the same: the provider gate, the skip switch, and the hard failure.
"""

import pytest

from agent_core.harness.lifecycle import HarnessReadiness, ReadinessStatus


def _ready():
    return HarnessReadiness(ready=True, status=ReadinessStatus.READY)


def _needs_auth():
    return HarnessReadiness(
        ready=False, status=ReadinessStatus.AUTHENTICATION_REQUIRED, detail="not signed in"
    )


# ---------------------------------------------------------------------------
# _ensure_model_auth — non-openai provider is a no-op (no client needed)
# ---------------------------------------------------------------------------

def test_ensure_model_auth_noop_for_non_openai(monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "deepseek/deepseek-v4-pro")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "deepseek")

    from uta.app.cli import _ensure_model_auth

    # Must succeed without a client and without any HTTP server running
    _ensure_model_auth("/fake/repo")  # no exception = pass


def test_ensure_model_auth_succeeds_when_openai_probe_passes(monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "openai")

    # Probe returns True (auth ok) without HTTP
    monkeypatch.setattr("uta.app.cli._probe_harness_readiness", lambda repo: _ready())

    from uta.app.cli import _ensure_model_auth

    _ensure_model_auth("/fake/repo")  # no exception = pass


def test_ensure_model_auth_can_skip_startup_probe(monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_auth_probe_enabled", False)
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "openai")
    monkeypatch.setattr(
        "uta.app.cli._probe_harness_readiness",
        lambda repo: (_ for _ in ()).throw(AssertionError("probe should be skipped")),
    )

    from uta.app.cli import _ensure_model_auth

    _ensure_model_auth("/fake/repo")


def test_ensure_model_auth_raises_with_instructions_when_openai_probe_fails(monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_auth_probe_enabled", True)
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "openai")

    monkeypatch.setattr("uta.app.cli._probe_harness_readiness", lambda repo: _needs_auth())

    from uta.app.cli import _ensure_model_auth

    with pytest.raises(RuntimeError, match="authentication is required"):
        _ensure_model_auth("/fake/repo")


# ---------------------------------------------------------------------------
# OpenCodeServer must NOT be imported in cli.py run pipeline
# ---------------------------------------------------------------------------

def test_opencode_server_not_used_in_run_pipeline():
    """Verify the run command no longer instantiates OpenCodeServer."""
    import uta.app.cli as cli_module
    import inspect

    # cli_module.run is a Click Command; .callback is the underlying Python function
    src = inspect.getsource(cli_module.run.callback)
    assert "OpenCodeServer" not in src, (
        "OpenCodeServer still referenced in the `run` command body. Remove server.start/stop."
    )
