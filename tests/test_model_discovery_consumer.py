"""UTA forwards trusted discovery inputs; agent-core owns their interpretation."""

from unittest.mock import Mock

import pytest

from uta.shared.config import Settings


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_SELECTION_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_MODEL_CODING_INDEX_MIN", raising=False)
    return Settings(_env_file=None)


def test_discovery_defaults_are_opt_in(config):
    assert config.model_selection_config == ""
    assert config.model_coding_index_min is None


def test_harness_spec_omits_empty_discovery_options(monkeypatch, config):
    from uta.testgen import harness

    monkeypatch.setattr(harness, "settings", config)

    options = harness._configured_spec().options

    assert "model_selection_config" not in options
    assert "model_coding_index_min" not in options


@pytest.mark.parametrize("threshold", [None, "", " 75.50 ", "nan", "101", "invalid"])
def test_host_aliases_reach_opaque_harness_options(monkeypatch, config, threshold):
    from uta.testgen import harness

    path = "/trusted/host/not-yet-provisioned.json"
    monkeypatch.setenv("AGENT_MODEL_SELECTION_CONFIG", path)
    if threshold is not None:
        monkeypatch.setenv("AGENT_MODEL_CODING_INDEX_MIN", threshold)
    configured = Settings(_env_file=None)
    monkeypatch.setattr(harness, "settings", configured)
    factory = Mock(return_value=object())
    monkeypatch.setattr(harness, "create_configured_harness", factory)

    assert harness.create_agent_harness() is factory.return_value

    options = factory.call_args.args[0].options
    assert options["model_selection_config"] == path
    assert options["model_coding_index_min"] == threshold


@pytest.mark.parametrize("producer", ["shared", "cli", "java", "python"])
def test_discovery_snapshot_defers_selection_without_legacy_router(
    monkeypatch, config, producer, tmp_path
):
    from agent_core.harness import tiered_router
    from uta.app import cli
    from uta.shared import opencode_snapshot

    configured = config.model_copy(update={
        "model_selection_config": "/trusted/host/missing.json",
        "opencode_provider_tokens": "old.token=secret-value",
    })
    monkeypatch.setattr(cli, "settings", configured)
    monkeypatch.setattr(opencode_snapshot, "settings", configured)
    for name in (
        "available_provider_candidates", "provider_candidates", "parse_provider_chain",
        "parse_provider_tokens", "provider_token_statuses", "opencode_model_id",
    ):
        monkeypatch.setattr(tiered_router, name, Mock(side_effect=AssertionError(name)))

    if producer in ("java", "python"):
        from uta.language.java.ci import JavaCiLanguageHandler
        from uta.language.python.ci import PythonCiLanguageHandler
        from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
        from uta.shared.fix_sessions import CreateFixSessionRequest

        handler = (
            JavaCiLanguageHandler(runner=None) if producer == "java"
            else PythonCiLanguageHandler(runner=None)
        )
        manager = Mock()
        target = "class:com.example.Thing" if producer == "java" else "pyfile:jobs/run.py"
        handler.create_repair_task(
            task_manager=manager,
            record=CiTaskRecord(
                task_id="discovery-repair", status=CiTaskStatus.failed,
                request=CiTriggerRequest(
                    app_name="app", git_url="git@example.com:group/app.git",
                    branch="feature/repair", language=producer,
                ),
            ),
            request=CreateFixSessionRequest(target_ids=[target]),
            repo_path=tmp_path, priority=1, base_ref="main",
            coverage_gate=95.0, mutation_gate=95.0,
            rdc_context={}, rdc_context_path=None,
        )
        create = manager.create_task if producer == "java" else manager.create_task_targets
        snapshot = create.call_args.kwargs["config_snapshot"]
    else:
        snapshot = (
            cli._task_config_snapshot() if producer == "cli"
            else opencode_snapshot.opencode_config_snapshot()
        )

    assert snapshot["opencode_model"] == configured.opencode_model
    assert snapshot["opencode_provider"] == configured.opencode_provider
    assert snapshot["opencode_selected_model"] == ""
    assert snapshot["opencode_selected_provider"] == ""
    assert snapshot["opencode_candidate_index"] is None
    assert snapshot["opencode_provider_chain"] == []
    assert not snapshot["opencode_provider_tokens"]
    assert not any(key.startswith("model_") for key in snapshot)
    assert "secret-value" not in str(snapshot)
    if producer == "cli":
        assert snapshot["coverage_gate"] == configured.coverage_gate
        assert snapshot["mutation_gate"] == configured.mutation_gate


@pytest.mark.parametrize("discovery", [False, True])
def test_historical_selection_only_applies_in_manual_mode(monkeypatch, config, discovery):
    from uta.app import cli

    configured = config.model_copy(update={
        "model_selection_config": "/trusted/host/policy.json" if discovery else "",
        "opencode_model": "current/model",
        "opencode_small_model": "current/small",
        "opencode_provider": "current",
    })
    monkeypatch.setattr(cli, "settings", configured)
    before = configured.model_dump()

    cli._apply_task_opencode_selection({
        "opencode_selected_model": "historic/model",
        "opencode_selected_provider": "historic",
        "opencode_candidate_index": 99,
        "model_selection_config": "/untrusted/task/policy.json",
        "model_coding_index_min": "0",
    })

    if discovery:
        assert configured.model_dump() == before
    else:
        assert configured.opencode_model == "historic/model"
        assert configured.opencode_small_model == "historic/model"
        assert configured.opencode_provider == "historic"
        assert configured.model_selection_config == ""
