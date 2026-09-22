"""The harness spec must carry credentials that actually authenticate.

`HarnessConfig.model_dump()` redacts provider tokens to "***" — right for
anything displayed, wrong for the dump this rebuilds into the harness's own
configuration. A redacted token is not obviously broken: it is a non-empty
string the harness accepts, authenticates with, and fails on. The only symptom
is a 401 from the provider, arriving several layers away.
"""

from __future__ import annotations

from agent_core.harness.tiered_router import parse_provider_tokens

from uta.shared.config import settings
from uta.testgen.harness import _configured_spec


def test_the_spec_carries_a_usable_provider_token(monkeypatch):
    monkeypatch.setattr(
        settings, "opencode_provider_tokens", "token-pool.token=secret-value"
    )

    options = _configured_spec().options

    assert options["opencode_provider_tokens"] == "token-pool.token=secret-value"
    assert parse_provider_tokens(options["opencode_provider_tokens"]) == {
        "token-pool": "secret-value"
    }


def test_a_redacted_token_would_not_parse(monkeypatch):
    """Naming what the bug looked like: present, non-empty, and useless."""
    monkeypatch.setattr(
        settings, "opencode_provider_tokens", "token-pool.token=secret-value"
    )

    redacted = settings.model_dump()["opencode_provider_tokens"]

    assert redacted == "***"
    assert parse_provider_tokens(redacted) == {}
