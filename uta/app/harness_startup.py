"""Everything UTA does to an agent harness before a run starts.

Three questions get asked at startup, and none of them is about OpenCode:
can the harness prepare this repository, can it answer at all, and should a
run that cannot confirm the provider proceed. UTA used to answer all three by
name -- `OpenCodeProcess().run_turn("Reply with only: OK")` with its own retry
loop, and `generate_opencode_config` for the workspace. Both are agent-core's
now, behind neutral lifecycle calls, so this module names a harness nowhere.

It sits beside `cli.py` rather than inside it because startup readiness is not
a command. The CLI is one caller; the daemon and the tests are others, and a
probe with a policy attached to it is easier to review on its own than as
three functions between a log formatter and a Maven settings parser.

The provider gate is kept deliberately. Only openai/* was ever probed, and
probing every harness would add a startup round-trip and a new failure mode
for deployments that never had one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from uta.shared.config import settings

LOGGER = logging.getLogger("uta")


def provider_from_model(model_id: str) -> Optional[str]:
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0]


def prepare_workspace(repo) -> None:
    """Let the configured harness set the repository up, whatever it is.

    This used to call `generate_opencode_config` directly -- one harness's
    answer to a question every harness has. The neutral helper is a no-op for a
    harness needing no repo-local preparation, so nothing here branches on which
    one is configured.
    """
    from pathlib import Path as _Path

    from agent_core.harness.lifecycle import prepare_harness_workspace

    from uta.testgen.harness import create_agent_harness
    from uta.shared.workspace_rules import validate_workspace_rules

    validate_workspace_rules(_Path(repo))
    prepare_harness_workspace(create_agent_harness(), repo_path=_Path(repo))


def probe_harness_readiness(repo: str) -> "object":
    """Ask the configured harness whether it can run a turn here.

    This was a hand-rolled `OpenCodeProcess().run_turn("Reply with only: OK")`
    with its own retry loop and its own parsing of provider error payloads.
    All of that is agent-core's now: the retry policy carries the same three
    attempts with 3s/6s backoff, and provider errors arrive already normalised,
    so nothing here reads a provider's error shape.
    """
    from agent_core.harness.lifecycle import check_harness_readiness

    from uta.testgen.harness import create_agent_harness

    return check_harness_readiness(
        create_agent_harness(),
        repo_path=Path(repo),
        timeout_seconds=120,
    )


def ensure_model_auth(repo: str, *, probe=None) -> None:
    """Refuse to start against a harness that cannot answer.

    The provider gate is kept deliberately. Only openai/* was ever probed, and
    probing every harness would add a startup round-trip and a new failure mode
    for deployments that never had one. What changed is that UTA no longer knows
    *how* to probe -- only whether it wants to, and what to do about the answer.
    """
    from agent_core.harness.lifecycle import ReadinessStatus

    if not settings.opencode_auth_probe_enabled:
        LOGGER.info("Harness startup readiness probe disabled by configuration")
        return
    provider_id = provider_from_model(settings.opencode_model) or settings.opencode_provider
    if provider_id != "openai":
        return

    readiness = (probe or probe_harness_readiness)(repo)
    if readiness.ready:
        return
    if readiness.status == ReadinessStatus.AUTHENTICATION_REQUIRED:
        raise RuntimeError(
            "Harness authentication is required for openai/* models. "
            "Authenticate with the configured agent, then rerun UTA."
        )
    # Retry-exhausted `unavailable` aborts too: a run that could not confirm the
    # provider is a run that should not spend money finding out.
    raise RuntimeError(f"Harness readiness probe failed: {readiness.detail or readiness.status}")


__all__ = [
    "ensure_model_auth",
    "prepare_workspace",
    "probe_harness_readiness",
    "provider_from_model",
]
