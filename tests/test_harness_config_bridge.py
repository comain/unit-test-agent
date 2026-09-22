"""UTA's settings are the shared harness's settings.

`uta/opencode/` was replaced by agent-core, whose harness resolves every
setting through its own active configuration. UTA already declared 39 of
those 44 settings, under the same `UTA_` prefix, so the risk in the swap was
never that the two would disagree at startup -- they load the same
environment -- but that they would diverge the moment anything set one at
runtime. The suite does that constantly; so does a daemon retuning a provider
mid-run.

These tests pin the property that makes that impossible: there is one object.
"""

from __future__ import annotations

import threading

import pytest

from agent_core.config import HarnessConfig, current_config, use_config
from uta.shared.config import Settings, settings


@pytest.fixture
def restore(request):
    """Put back any setting a test changes on the shared object."""
    saved = {}

    def remember(name):
        saved[name] = getattr(settings, name)

    yield remember

    for name, value in saved.items():
        setattr(settings, name, value)


def test_utas_settings_are_a_harness_config():
    """The join. Everything else here follows from it."""
    assert isinstance(settings, HarnessConfig)
    assert issubclass(Settings, HarnessConfig)


def test_the_harness_reads_exactly_this_object():
    assert current_config() is settings


def test_a_runtime_change_is_visible_to_the_harness(restore):
    """The failure mode two config objects would have had, and the reason for one."""
    restore("opencode_bin")
    from agent_core.config import settings as harness_settings

    settings.opencode_bin = "/opt/app/unit_test_agent/opencode"

    assert harness_settings.opencode_bin == "/opt/app/unit_test_agent/opencode"


def test_it_reaches_worker_threads(restore):
    """UTA fans work out across threads; a scoped config would not reach them."""
    restore("opencode_bin")
    settings.opencode_bin = "/opt/app/unit_test_agent/opencode"
    seen = []

    thread = threading.Thread(target=lambda: seen.append(current_config().opencode_bin))
    thread.start()
    thread.join()

    assert seen == ["/opt/app/unit_test_agent/opencode"]


def test_utas_own_declaration_wins_over_the_inherited_one():
    """Subclassing must not quietly re-default a setting this project owns."""
    assert Settings.model_fields["opencode_port"].default == 4096
    assert settings.opencode_model.startswith("token-pool/")


def test_the_settings_the_harness_adds_are_available():
    """The five UTA never declared, inherited with agent-core's defaults."""
    for name in (
        "opencode_permissions",
        "opencode_permission_dirs",
        "opencode_default_external_dirs",
        "opencode_pass_model_flag",
        "skip_permission_prompts",
    ):
        assert hasattr(settings, name), name


def test_unrelated_uta_settings_survive_the_subclassing():
    """Hundreds of enforcement and language settings share this object."""
    assert hasattr(settings, "quarantine_threshold")
    assert hasattr(settings, "batch_cap_usd")


def test_deployed_paths_keep_the_names_already_on_disk():
    """Nodes have caches and logs under these names; agent-core defaults elsewhere.

    Applied by `Settings.model_post_init`, which is why importing `uta.config`
    is all it takes -- there is nothing for an entry point to remember to call.
    """
    config = current_config()

    assert config.agent_cache_dir == ".uta_cache"
    assert config.opencode_turn_log_dir == ".uta_cache/opencode_turns"
    assert config.agent_debug_log_dir == "uta-run-logs"


def test_the_debug_log_directory_matches_where_the_cli_writes():
    """cli.py writes here and the harness reads it back hunting for a 429."""
    import tempfile
    from pathlib import Path

    from agent_core.harness.rate_limit import debug_log_dir

    assert debug_log_dir() == Path(tempfile.gettempdir()) / "uta-run-logs"


def test_a_deployed_path_set_at_runtime_is_not_clobbered(restore):
    """`model_fields_set` is what separates a configured value from a default."""
    restore("agent_cache_dir")
    settings.agent_cache_dir = "/set/at/runtime"

    from uta.shared.config import Settings

    Settings()  # constructing another must not reach into this one

    assert settings.agent_cache_dir == "/set/at/runtime"


def test_a_scoped_override_still_wins_while_active():
    with use_config(HarnessConfig(opencode_bin="/scoped")):
        assert current_config().opencode_bin == "/scoped"

    assert current_config() is settings
