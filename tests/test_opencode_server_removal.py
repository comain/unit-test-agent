"""Tests that the run pipeline no longer depends on OpenCodeServer.

RED: these tests should fail before the Task 1 implementation.
"""
import importlib
import types
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _ensure_model_auth — non-openai provider is a no-op (no client needed)
# ---------------------------------------------------------------------------

def test_ensure_model_auth_noop_for_non_openai(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "deepseek/deepseek-v4-pro")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "deepseek")

    from uta.cli import _ensure_model_auth

    # Must succeed without a client and without any HTTP server running
    _ensure_model_auth("/fake/repo")  # no exception = pass


def test_ensure_model_auth_succeeds_when_openai_probe_passes(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")

    # Probe returns True (auth ok) without HTTP
    monkeypatch.setattr(
        "uta.cli._probe_openai_auth_ready_with_retry",
        lambda *args, **kwargs: True,
    )

    from uta.cli import _ensure_model_auth

    _ensure_model_auth("/fake/repo")  # no exception = pass


def test_ensure_model_auth_raises_with_instructions_when_openai_probe_fails(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")

    monkeypatch.setattr(
        "uta.cli._probe_openai_auth_ready_with_retry",
        lambda *args, **kwargs: False,
    )

    from uta.cli import _ensure_model_auth

    with pytest.raises(RuntimeError, match="opencode /connect"):
        _ensure_model_auth("/fake/repo")


# ---------------------------------------------------------------------------
# OpenCodeServer must NOT be imported in cli.py run pipeline
# ---------------------------------------------------------------------------

def test_opencode_server_not_used_in_run_pipeline():
    """Verify the run command no longer instantiates OpenCodeServer."""
    import uta.cli as cli_module
    import inspect

    # cli_module.run is a Click Command; .callback is the underlying Python function
    src = inspect.getsource(cli_module.run.callback)
    assert "OpenCodeServer" not in src, (
        "OpenCodeServer still referenced in the `run` command body. Remove server.start/stop."
    )


def test_opencode_server_not_used_in_resume_gates_pipeline():
    """Verify resume-gates no longer instantiates OpenCodeServer."""
    import uta.cli as cli_module
    import inspect

    # find the resume-gates command (registered as resume_gates)
    fn = getattr(cli_module, "resume_gates", None)
    if fn is None:
        pytest.skip("resume_gates not found — function may be renamed")

    src = inspect.getsource(fn.callback if hasattr(fn, "callback") else fn)
    assert "OpenCodeServer" not in src, (
        "OpenCodeServer still referenced in resume-gates body. Remove server.start/stop."
    )
