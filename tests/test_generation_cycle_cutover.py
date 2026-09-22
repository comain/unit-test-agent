import json
import subprocess
from pathlib import Path

from uta.tasks.manager import TaskManager
import pytest

from uta.testgen.cutover import production_cycle_context, workspace_fingerprint
from uta.testgen.workspace_guard import TaskUnsafeDiffError
from uta.testgen.graph import durable_cycle, nodes


def test_outer_node_always_runs_the_durable_child(monkeypatch):
    calls = []
    monkeypatch.setattr(
        durable_cycle,
        "run_durable_generation_cycle",
        lambda state: calls.append("v2") or {"results": {}},
    )

    result = nodes.run_generation_cycle({"generation_cycle_v2_enabled": False})

    assert calls == ["v2"]
    assert "generation_engine" not in result


def test_engine_selection_is_audited_once_in_outer_state(tmp_path, monkeypatch):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"),
        class_fqns=["pkg.Target"],
        config_snapshot={},
    )
    monkeypatch.setattr(
        durable_cycle, "run_durable_generation_cycle", lambda state: {"results": {}}
    )
    base = {
        "task_id": task_id,
        "task_db_path": str(manager.db_path),
    }
    first = nodes.run_generation_cycle(base)
    nodes.run_generation_cycle({**base, **first})

    selected = [
        row
        for row in manager.db.events_since(task_id)
        if row["event_type"] == "generation_engine_selected"
    ]
    assert len(selected) == 1
    assert json.loads(selected[0]["payload_json"]) == {
        "engine": "durable_v2",
    }


def test_task_snapshot_creator_leaves_compatibility_dual_write_to_task_db():
    from uta.app.cli import _task_config_snapshot

    snapshot = _task_config_snapshot()

    assert "generation_cycle_v2_enabled" not in snapshot
    json.dumps(snapshot)


def test_durable_cycle_rejects_direct_library_invocation_of_legacy_task(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    manager.db.update_repo_task(task_id, config_snapshot_json="{}")

    with pytest.raises(RuntimeError, match="submit a new task"):
        durable_cycle.run_durable_generation_cycle(
            {
                "task_id": task_id,
                "task_db_path": str(manager.db_path),
                "repo_path": str(tmp_path / "repo"),
                "candidates": ["pkg.Target"],
            }
        )

    assert manager.db.find_class_task(task_id, "pkg.Target")["batch_key"] is None


def test_python_result_is_always_committed_by_outer_delivery(monkeypatch):
    from uta.language.python import generation_backend

    observed = []

    def commit(state):
        observed.append(list(state["current_batch"]))
        return {"current_stage": "commit_to_branch"}

    monkeypatch.setattr(
        "uta.testgen.delivery.commit_to_branch",
        commit,
    )

    durable = generation_backend.deliver_target(
        {"current_batch": ["pyfile:pkg/module.py"]}
    )

    assert observed == [["pyfile:pkg/module.py"]]
    assert durable["current_stage"] == "commit_to_branch"


def test_durable_child_does_not_project_terminal_product_results_before_delivery():
    source = (Path("uta/testgen/graph/durable_cycle.py")).read_text(encoding="utf-8")

    assert "sync_results(" not in source


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def test_workspace_fingerprint_includes_clean_relevant_source_bytes(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    state = {
        "repo_path": str(repo),
        "language": "python",
        "batch": ["pyfile:pkg/module.py"],
        "target": {"source_path": "pkg/module.py"},
        "coverage_gate": 80,
        "prerequisite_operation_ids": ["prior-operation"],
    }
    before = workspace_fingerprint(state, "generate_tests", "turn")

    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change tracked source")

    assert workspace_fingerprint(state, "generate_tests", "turn") != before


def test_durable_turn_rejects_an_agent_created_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(repo), class_fqns=["pkg.Target"]
    )
    state = {
        "repo_path": str(repo),
        "task_id": task_id,
        "task_db_path": str(manager.db_path),
        "batch": ["pkg.Target"],
    }
    monkeypatch.setattr(
        "uta.testgen.cutover.llm_guard_before",
        lambda turn_state, batch, phase: {"repo_path": str(repo)},
    )
    monkeypatch.setattr("uta.testgen.cutover.llm_guard_after", lambda state, token: None)

    context = production_cycle_context(
        state=state,
        batch=type("Batch", (), {"workflow_run_id": "run", "unit_id": "unit"})(),
        backend=type("Backend", (), {})(),
        runner=object(),
    )
    token = context["before_turn"](state, {"label": "generate_tests"})
    (repo / "README.md").write_text("committed by agent\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "agent commit")

    with pytest.raises(TaskUnsafeDiffError, match="repository HEAD"):
        context["after_turn"](state, {"label": "generate_tests"}, token)


def test_workspace_fingerprint_ignores_utas_own_runtime_residue(tmp_path):
    """Observed on beta: terminal validation rejected a successful run.

    `complete_generation` records the workspace fingerprint in its result
    envelope, and `validate_terminal` recomputes it moments later. In between,
    UTA writes its own report -- `.uta_reports/status.html` and
    `live_status.json` are refreshed continuously during a run -- so the
    fingerprint changed and the run was rejected with "terminal generation
    workspace no longer matches its result", stranding a task that had done all
    its work correctly.

    These paths are already enumerated by the workspace policy as UTA's own
    allowed residue, so they are not evidence of anything an agent did. They
    must not take part in workspace identity, or the durable path fails against
    its own writes.
    """
    repo = tmp_path / "repo"
    source = repo / "pkg" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    state = {
        "repo_path": str(repo),
        "language": "python",
        "batch": ["pyfile:pkg/module.py"],
        "target": {"source_path": "pkg/module.py"},
    }
    before = workspace_fingerprint(state, "complete_generation", "deterministic")

    reports = repo / ".uta_reports"
    reports.mkdir()
    (reports / "status.html").write_text("<html>running</html>", encoding="utf-8")
    (repo / "opencode.json").write_text("{}", encoding="utf-8")
    cache = repo / ".uta_cache"
    cache.mkdir()
    (cache / "context.json").write_text("{}", encoding="utf-8")

    assert workspace_fingerprint(state, "complete_generation", "deterministic") == before


def test_workspace_fingerprint_still_notices_a_generated_test(tmp_path):
    """The residue exclusion must not blind the fingerprint to real output."""
    repo = tmp_path / "repo"
    source = repo / "pkg" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    state = {
        "repo_path": str(repo),
        "language": "python",
        "batch": ["pyfile:pkg/module.py"],
        "target": {"source_path": "pkg/module.py"},
    }
    before = workspace_fingerprint(state, "complete_generation", "deterministic")

    generated = repo / "tests" / "uta_generated"
    generated.mkdir(parents=True)
    (generated / "test_module.py").write_text("def test_x(): pass\n", encoding="utf-8")

    assert workspace_fingerprint(state, "complete_generation", "deterministic") != before


def test_workspace_fingerprint_ignores_operation_bookkeeping(tmp_path):
    """Java completed all ten operations and was still rejected with
    "terminal generation workspace no longer matches its result".

    The fingerprint mixed operation bookkeeping into workspace identity:
    `prerequisite_operation_ids` is part of the hashed configuration, and it
    grows as each operation completes. `complete_generation` records the
    fingerprint during its own execution; `validate_terminal` recomputes it
    from the final state, by which point that list has moved on. The workspace
    is byte-identical either side -- only the bookkeeping changed.

    Python did not hit this because its simpler state never carried the key.

    A fingerprint named for the workspace must describe the workspace. The
    prerequisite chain is real evidence, but it is validated separately by
    `_validate_prerequisites`, which checks the artifacts actually exist and
    belong to this run.
    """
    repo = tmp_path / "repo"
    source = repo / "pkg" / "Thing.java"
    source.parent.mkdir(parents=True)
    source.write_text("class Thing {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    state = {
        "repo_path": str(repo),
        "language": "java",
        "batch": ["pkg.Thing"],
        "target": {"source_path": "pkg/Thing.java"},
        "prerequisite_operation_ids": ["op-precheck"],
    }
    before = workspace_fingerprint(state, "complete_generation", "deterministic")

    # Later operations completed; the chain grew. The workspace did not change.
    state["prerequisite_operation_ids"] = [
        "op-precheck", "op-plan", "op-generate", "op-verify", "op-measure",
    ]

    assert workspace_fingerprint(state, "complete_generation", "deterministic") == before


def test_workspace_fingerprint_still_tracks_the_gates_that_define_a_run(tmp_path):
    """Dropping bookkeeping must not drop real configuration: a different
    coverage gate is a different run and must not reuse another's evidence."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    state = {
        "repo_path": str(repo),
        "language": "java",
        "batch": ["pkg.Thing"],
        "coverage_gate": 80,
    }
    before = workspace_fingerprint(state, "complete_generation", "deterministic")

    state["coverage_gate"] = 95

    assert workspace_fingerprint(state, "complete_generation", "deterministic") != before


def test_workspace_fingerprint_is_stable_when_an_optional_key_is_absent(tmp_path):
    """Java kept failing terminal validation with a byte-identical workspace.

    The configuration block was built with `if key in state`, so a key merely
    being absent at one call and present at the other changed the hash --
    `complete_generation` records the fingerprint from its own node state, and
    `validate_terminal` recomputes it from the final state, which need not
    carry exactly the same optional keys.

    An unset value and a missing key describe the same run, so they must hash
    the same. A *changed* value still must not.
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    base = {"repo_path": str(repo), "language": "java", "batch": ["pkg.Thing"]}
    with_absent = dict(base)
    with_none = dict(base, quality_gate_command=None, module=None)

    assert (
        workspace_fingerprint(with_none, "complete_generation", "deterministic")
        == workspace_fingerprint(with_absent, "complete_generation", "deterministic")
    )


def test_workspace_fingerprint_still_separates_different_modules(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    base = {"repo_path": str(repo), "language": "java", "batch": ["pkg.Thing"]}

    assert (
        workspace_fingerprint(dict(base, module="service"), "complete_generation", "deterministic")
        != workspace_fingerprint(dict(base, module="biz"), "complete_generation", "deterministic")
    )


def test_workspace_fingerprint_ignores_accumulated_result_paths(tmp_path):
    """A result recording a path the task definition never named must not grow
    the hashed set.

    This is what actually kept Java failing. `precheck_existing_tests` records
    a result whose `test_file_path` is the existing hand-written test -- a path
    `generated_test_path` does not cover -- so the set hashed by
    `complete_generation` had one entry and the set recomputed moments later
    had two.

    My first version of this test set `generated_test_path` to the same path,
    so the set could not grow and the test passed against the bug. It now uses
    a distinct path, which is the case that occurs.
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    (repo / "pkg" / "HandWrittenThingTest.java").write_text("class T {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    base = {
        "repo_path": str(repo),
        "language": "java",
        "batch": ["pkg.Thing"],
        "target": {"source_path": "pkg/Thing.java"},
        "generated_test_path": "pkg/ThingTest.java",
    }
    before = workspace_fingerprint(base, "complete_generation", "deterministic")

    discovered = dict(
        base,
        results={"pkg.Thing": {"test_file_path": "pkg/HandWrittenThingTest.java"}},
    )

    assert workspace_fingerprint(discovered, "complete_generation", "deterministic") == before


def test_workspace_fingerprint_still_notices_the_generated_test_changing(tmp_path):
    """Excluding results bookkeeping must not stop the fingerprint seeing the
    test file itself change."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    test = repo / "pkg" / "ThingTest.java"
    test.write_text("class ThingTest {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    state = {
        "repo_path": str(repo),
        "language": "java",
        "batch": ["pkg.Thing"],
        "target": {"source_path": "pkg/Thing.java"},
        "generated_test_path": "pkg/ThingTest.java",
    }
    before = workspace_fingerprint(state, "complete_generation", "deterministic")

    test.write_text("class ThingTest { void t() {} }\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change the test")

    assert workspace_fingerprint(state, "complete_generation", "deterministic") != before


def test_workspace_fingerprint_ignores_paths_discovered_during_the_run(tmp_path):
    """The actual cause of the Java terminal-validation failures.

    Measured on beta with per-component logging:

        record   files=[TemplateConflictException.java]
        validate files=[TemplateConflictException.java,
                        TemplateConflictExceptionTest.java]

    `_relevant_workspace_paths` included `existing_test_path`, which
    `precheck_existing_tests` *discovers* during the run. So the set hashed by
    `complete_generation` had one entry and the set recomputed by
    `validate_terminal` had two, and a run where every file on disk was
    identical was discarded.

    The hashed set must be derivable from the task definition -- the target's
    source and its canonical test path -- not from what a phase has discovered
    so far. A discovered file that actually changed still shows up in `dirty`.
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "Thing.java").write_text("class Thing {}\n", encoding="utf-8")
    (repo / "pkg" / "ThingTest.java").write_text("class ThingTest {}\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "uta@example.invalid")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    before_discovery = {
        "repo_path": str(repo),
        "language": "java",
        "batch": ["pkg.Thing"],
        "target": {"source_path": "pkg/Thing.java"},
    }
    recorded = workspace_fingerprint(
        before_discovery, "complete_generation", "deterministic"
    )

    after_discovery = dict(before_discovery, existing_test_path="pkg/ThingTest.java")

    assert (
        workspace_fingerprint(after_discovery, "complete_generation", "deterministic")
        == recorded
    )
