"""Marker-activated pytest plugins, loaded only when they are installed.

``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` keeps plugins installed in UTA's own runtime
-- langsmith, anyio -- out of a project's test run, so a plugin the project
activates by something other than an import has to be named explicitly instead.

Naming one directly in ``PYTEST_PLUGINS`` makes its absence fatal: pytest raises
``ImportError: Error importing plugin`` during startup and the session collects
nothing, turning "the async tests fail" into "no test ran at all" -- and with
it a real coverage number into a zero. Loading through this shim degrades to
the former, which is a result the gate can reason about.

pytest reads ``pytest_plugins`` off any module plugin it registers, so the
tuple below is resolved at import time against the interpreter and overlay that
will actually run the tests.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Mapping

#: Requested when UTA names nothing, so a direct pytest run still behaves.
DEFAULT_OPTIONAL_PLUGINS = ("pytest_asyncio.plugin",)

#: Names the plugins to attempt, so the policy stays on UTA's side and this
#: module stays a resolver.
OPTIONAL_PLUGINS_ENV = "UTA_PYTEST_OPTIONAL_PLUGINS"

#: pytest-timeout reads its value from this, and ignores it when not loaded --
#: so setting it is safe whether or not the project declares the plugin.
TIMEOUT_ENV = "PYTEST_TIMEOUT"

#: Per-test seconds. A deadlocked test otherwise spends the whole enforcement
#: budget and the run is reaped with no traceback and no coverage at all.
DEFAULT_TEST_TIMEOUT_SECONDS = 120

TIMEOUT_SETTING_ENV = "UTA_PYTHON_TEST_TIMEOUT_SECONDS"

_TIMEOUT_PLUGIN = "pytest_timeout"


def _importable(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def requested_plugins(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    raw = str(env.get(OPTIONAL_PLUGINS_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_OPTIONAL_PLUGINS
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def test_timeout_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Per-test timeout; 0 disables it."""
    env = os.environ if environ is None else environ
    raw = str(env.get(TIMEOUT_SETTING_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_TEST_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TEST_TIMEOUT_SECONDS
    return value if value > 0 else 0


def optional_plugin_env(
    *,
    asyncio: bool,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The env that asks this shim for the plugins a run needs.

    ``pytest_timeout`` is always requested: a project that declares it gets its
    own safety net back, and one that does not is unaffected because the shim
    resolves the name rather than asserting it.
    """
    plugins = [_TIMEOUT_PLUGIN]
    if asyncio:
        plugins.insert(0, "pytest_asyncio.plugin")
    env = {OPTIONAL_PLUGINS_ENV: ",".join(plugins)}
    seconds = test_timeout_seconds(environ)
    if seconds:
        env[TIMEOUT_ENV] = str(seconds)
    return env


pytest_plugins = tuple(name for name in requested_plugins() if _importable(name))
