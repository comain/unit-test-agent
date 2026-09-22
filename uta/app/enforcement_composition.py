"""Where the enforcement bindings are chosen, and the only such place.

The contract can dispatch to a language binding, but it must never know which
bindings exist -- that is what keeps `uta_enforce_core` shippable without UTA
and keeps `uta.testgen` unable to reach past the contract into an
implementation. Somebody has to know, though, and that somebody is the
application.

This module is deliberately small and deliberately imports concrete things.
Every other module in the tree is forbidden from doing so; the dependency gate
enforces that, and `tests/test_enforcement_composition.py` asserts that this is
the only file naming both bindings.
"""

from __future__ import annotations

from functools import lru_cache

from uta_enforce_core.registry import EnforcementRegistry


@lru_cache(maxsize=1)
def build_enforcement_registry() -> EnforcementRegistry:
    """The immutable language-to-binding table for this deployment.

    Built once and cached: the registry is frozen at construction, so there is
    no state to go stale, and rebuilding it per call would import the Java and
    Python stacks repeatedly.

    Registering the Python proxy is the wiring; it is also now the only
    Python implementation. The legacy lane, its shadow-comparison mode and the
    `UTA_PYTHON_ENFORCEMENT_IMPL` switch are gone, so there is no longer a
    choice to make at run time.
    """
    from uta.enforcement.bindings.java import (
        JavaEnforcementBinding,  # noqa: F401  -- named here so this file is
        create_java_enforcement_binding,  # visibly the one place both bindings meet
    )
    from uta.enforcement.bindings.python_proxy import UtaPythonEnforcementProxy

    return EnforcementRegistry(
        [
            create_java_enforcement_binding(),
            UtaPythonEnforcementProxy(),
        ]
    )


__all__ = ["build_enforcement_registry"]
