"""UTA and agent-core are one deployable pair, and this proves it stays one.

UTA already pinned a released tag rather than a branch, so unlike CR there is
no drift to undo here -- what is missing is the second half. The declared pin
governs a fresh install; it says nothing about an environment somebody
upgraded by hand or a source/runtime pair deployed at different versions.
An unsupported pair has to fail at startup, before a run acquires work and
starts spending model budget.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from uta.app.agent_core_pin import (
    AgentCorePinError,
    REQUIRED_AGENT_CORE_TAG,
    REQUIRED_AGENT_CORE_VERSION,
    installed_agent_core_version,
    verify_agent_core_pin,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _agent_core_requirement() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        requirement
        for requirement in pyproject["project"]["dependencies"]
        if requirement.split("[")[0].split(" ")[0].strip() == "agent-core"
    ]
    assert len(requirements) == 1, requirements
    return requirements[0]


# -- what the package declares ------------------------------------------------


def test_release_pair_uses_agent_core_0_8_52():
    assert REQUIRED_AGENT_CORE_VERSION == "0.8.52"
    assert REQUIRED_AGENT_CORE_TAG == "v0.8.52"


def test_the_declared_requirement_pins_the_released_tag():
    assert _agent_core_requirement().endswith(f"@{REQUIRED_AGENT_CORE_TAG}")


def test_the_declared_requirement_does_not_track_a_moving_branch():
    requirement = _agent_core_requirement()
    _, _, ref = requirement.rpartition("@")
    assert ref not in {"main", "master", "HEAD"}, requirement


# -- what the environment holds -----------------------------------------------


def test_the_installed_version_is_exactly_the_pinned_one():
    assert installed_agent_core_version() == REQUIRED_AGENT_CORE_VERSION


def test_the_pin_accepts_the_supported_version():
    verify_agent_core_pin(REQUIRED_AGENT_CORE_VERSION)


def test_the_pin_refuses_the_breaking_next_major_contract():
    with pytest.raises(AgentCorePinError) as excinfo:
        verify_agent_core_pin("0.8.0")
    assert "0.8.0" in str(excinfo.value)
    assert REQUIRED_AGENT_CORE_VERSION in str(excinfo.value)


def test_the_pin_refuses_an_older_release_too():
    with pytest.raises(AgentCorePinError):
        verify_agent_core_pin("0.6.1")


def test_the_pin_refuses_a_missing_agent_core(monkeypatch):
    import uta.app.agent_core_pin as pin

    monkeypatch.setattr(pin, "installed_agent_core_version", lambda: None)
    with pytest.raises(AgentCorePinError) as excinfo:
        verify_agent_core_pin()
    assert "not installed" in str(excinfo.value)


# -- where the refusal happens ------------------------------------------------
#
# Both composition roots, because a workflow started from either one runs the
# same agent-core code. Checking only the CLI would leave the API able to
# accept a trigger on a pair that cannot finish it.


def test_the_cli_refuses_to_start_on_a_mismatched_core(monkeypatch):
    """The group callback, not a subcommand: it is the one place every `uta`
    invocation passes through, and it is where composition already happens."""
    import uta.app.agent_core_pin as pin

    from uta.app.cli import main

    monkeypatch.setattr(pin, "installed_agent_core_version", lambda: "0.8.0")

    with pytest.raises(AgentCorePinError):
        main.callback()


def test_the_api_refuses_to_start_on_a_mismatched_core(monkeypatch):
    import uta.app.agent_core_pin as pin

    from uta.app.app import create_app

    monkeypatch.setattr(pin, "installed_agent_core_version", lambda: "0.8.0")

    with pytest.raises(AgentCorePinError):
        create_app()
