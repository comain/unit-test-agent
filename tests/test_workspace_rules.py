from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from uta.shared.workspace_rules import WorkspaceRulesUnavailable, validate_workspace_rules


def test_repository_without_rules_is_allowed(tmp_path):
    validate_workspace_rules(tmp_path)


def test_broken_developer_symlink_is_actionable_and_unchanged(tmp_path):
    rules = tmp_path / ".rd_rule"
    rules.symlink_to("/missing/developer/rd_rule")
    with pytest.raises(WorkspaceRulesUnavailable, match="workspace_rules_unavailable") as caught:
        validate_workspace_rules(tmp_path)
    assert "/missing/developer/rd_rule" in str(caught.value)
    assert "No LLM work" in str(caught.value)
    assert rules.readlink().as_posix() == "/missing/developer/rd_rule"


def test_valid_relative_rules_link_is_allowed(tmp_path):
    rules = tmp_path / "rules"
    (rules / "agent").mkdir(parents=True)
    (rules / "agent/agent_develop_rule.md").write_text("development rules")
    (rules / "code_rule.md").write_text("code rules")
    (tmp_path / ".rd_rule").symlink_to("rules")
    validate_workspace_rules(tmp_path)


def test_missing_required_rule_is_named(tmp_path):
    (tmp_path / ".rd_rule").mkdir()
    with pytest.raises(WorkspaceRulesUnavailable, match="agent/agent_develop_rule.md"):
        validate_workspace_rules(tmp_path)


def test_declared_rules_missing_entirely_are_rejected(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Read .rd_rule/agent/agent_develop_rule.md before work")
    with pytest.raises(WorkspaceRulesUnavailable):
        validate_workspace_rules(tmp_path)


def test_java_cli_uses_shared_harness_precheck():
    from uta.app.generation_commands import _prepare_workspace
    from uta.app.harness_startup import prepare_workspace
    assert _prepare_workspace is prepare_workspace


def test_harness_is_not_created_with_broken_rules(tmp_path, monkeypatch):
    from uta.app.harness_startup import prepare_workspace
    factory = Mock()
    monkeypatch.setattr("uta.testgen.harness.create_agent_harness", factory)
    (tmp_path / ".rd_rule").symlink_to("missing")
    with pytest.raises(WorkspaceRulesUnavailable):
        prepare_workspace(tmp_path)
    factory.assert_not_called()


@pytest.mark.parametrize("language", ["java", "python"])
def test_batch_rejects_rules_before_resolving_backend(tmp_path, monkeypatch, language):
    from uta.testgen.batch import BatchGenerationRequest
    from uta.testgen.runner import run_batch_generation
    factory = Mock()
    monkeypatch.setattr("uta.testgen.runner.batch_generator_for", factory)
    (tmp_path / ".rd_rule").symlink_to("missing")
    with pytest.raises(WorkspaceRulesUnavailable):
        run_batch_generation(BatchGenerationRequest(language=language, repo_path=tmp_path, targets=[]))
    factory.assert_not_called()


def test_repair_creation_records_error_without_creating_task(tmp_path):
    from uta.app.repair.deferred_task import DeferredRepairTaskMixin
    worker = DeferredRepairTaskMixin()
    worker._service = Mock()
    worker._refresh_repair_workspace = Mock()
    record = SimpleNamespace(workspace_path=str(tmp_path), enforcement_result={})
    session = {}
    (tmp_path / ".rd_rule").symlink_to("missing")
    worker._create_repair_task(record, session, Mock())
    assert session["status"] == "repair_task_create_failed"
    assert session["repoTaskFailureKind"] == "workspace_rules_unavailable"
    assert "No LLM work" in session["repoTaskError"]
    worker._service.task_manager.find_active_duplicate_repair_task_for_targets.assert_not_called()
