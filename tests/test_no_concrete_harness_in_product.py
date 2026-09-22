"""UTA product code names no concrete harness.

Success criterion 9. Eight call sites reached past the neutral API into
OpenCode's process, config and auth client. They are replaced by the lifecycle
helpers agent-core released for exactly this: prepare, readiness, bootstrap.

`uta assess` consumes the optional neutral diagnostics capability. Provider
database access stays beside the corresponding adapter in agent-core.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _policy():
    if "package_dependency_policy" in sys.modules:
        return sys.modules["package_dependency_policy"]
    spec = importlib.util.spec_from_file_location(
        "package_dependency_policy", REPO_ROOT / "scripts" / "package_dependency_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_production_module_names_a_concrete_harness():
    module = _policy()
    hits = module.find_concrete_harness_names(REPO_ROOT / "uta", relative_to=REPO_ROOT)
    assert hits == [], [(h.path, h.line, h.name) for h in hits]


def test_the_exemption_list_is_empty():
    """Every entry that was a migration seam should be gone once SC9 is met."""
    module = _policy()
    assert module.CONCRETE_HARNESS_EXCEPTIONS == {}, module.CONCRETE_HARNESS_EXCEPTIONS


@pytest.mark.parametrize(
    "relative, helper",
    [
        # Harness startup left `cli.py` for `harness_startup.py`; the site is
        # what matters here, not which file it happens to sit in.
        ("uta/app/harness_startup.py", "prepare_harness_workspace"),
        ("uta/app/generation_commands.py", "from uta.app.harness_startup import prepare_workspace"),
        ("uta/app/harness_startup.py", "check_harness_readiness"),
        ("uta/testgen/project_summary_artifacts.py", "bootstrap_harness_workspace"),
    ],
)
def test_each_lifecycle_site_uses_the_neutral_helper(relative, helper):
    """Presence of the helper, and -- via the AST check above -- absence of the
    concrete name. Deliberately not a substring search for the old names: a
    comment explaining why the ban exists would trip it, which is the same trap
    the credential scanner fell into."""
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert helper in source, relative


def test_a_second_harness_needs_no_uta_change():
    """The point of the criterion: configuration selects the harness."""
    from agent_core.harness.lifecycle import (
        HarnessReadiness,
        ReadinessStatus,
        prepare_harness_workspace,
    )

    class Pi:
        name = "pi"

        def prepare_workspace(self, *, repo_path):
            self.prepared = repo_path

        def check_readiness(self, *, repo_path, timeout_seconds):
            return HarnessReadiness(ready=True, status=ReadinessStatus.READY)

        def run_turn(self, **kwargs):  # pragma: no cover - not exercised here
            raise NotImplementedError

    harness = Pi()
    prepare_harness_workspace(harness, repo_path=REPO_ROOT)
    assert harness.prepared == REPO_ROOT
