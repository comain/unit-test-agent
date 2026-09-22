"""The one supported agent-core version, checked before any run starts.

UTA's pin has been a released tag for a while, for the reason recorded in
`pyproject.toml`: a moving agent-core underneath a large rewrite makes every
failure ambiguous. What the declared pin cannot do is govern an environment
that was upgraded by hand after install. A mismatched pair has to fail at
startup rather than
partway through a run that has already spent model budget.

It lives in `app/` rather than `shared/` because only the composition
roots ask it: the lane gate refuses a shared module with one consumer, and
it is right to. Startup readiness is a delivery-layer concern.

Exact equality, not a floor: "whatever resolved" is precisely the state this
guard exists to rule out.
"""

from __future__ import annotations

from importlib import metadata
from typing import Optional

#: The distribution version the pinned tag actually installs -- what the
#: startup guard compares against. Keep this explicit because a historical
#: tag and its package metadata once differed.
REQUIRED_AGENT_CORE_VERSION = "0.8.52"

#: The git tag `pyproject.toml` pins. Bump these two and the requirement
#: together, never separately, and read the installed version off the tag
#: rather than assuming it matches.
REQUIRED_AGENT_CORE_TAG = "v0.8.52"


class AgentCorePinError(RuntimeError):
    """Raised when the installed agent-core is not the pinned pair member."""


def installed_agent_core_version() -> Optional[str]:
    """The distribution version, or None when agent-core is not installed."""
    try:
        return metadata.version("agent-core")
    except metadata.PackageNotFoundError:
        return None


def verify_agent_core_pin(version: Optional[str] = None) -> None:
    """Refuse to continue unless the installed core is the pinned one.

    Takes the version as an argument so the mismatch paths are testable
    without installing a different agent-core; callers pass nothing.
    """
    if version is None:
        version = installed_agent_core_version()
    if version == REQUIRED_AGENT_CORE_VERSION:
        return
    found = version or "not installed"
    raise AgentCorePinError(
        f"unit-test-agent requires agent-core {REQUIRED_AGENT_CORE_VERSION} "
        f"exactly, found {found}. UTA and agent-core are released as a matched "
        f"pair; reinstall the pinned pair member before running."
    )
