"""Unit tests for compile verification + agent fix loop."""

from unittest.mock import MagicMock

import pytest

from uta.graph.nodes import _run_generation_compile_gate, run_compile_fix_loop


def test_compile_fix_loop_retries_then_succeeds(monkeypatch):
    attempts = []

    def fake_compile(repo, mod, timeout=300):
        attempts.append(1)
        if len(attempts) < 3:
            return False, "[ERROR] cannot find symbol\n  bad compile"
        return True, ""

    monkeypatch.setattr("uta.graph.nodes._compile_test", fake_compile)

    client = MagicMock()
    client.create_session = MagicMock(return_value="compile-fix-session")
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.DemoService"],
        session_id="sess-1",
        client=client,
        maven_module_flag=" -pl biz -am",
        target_context_paths={
            "com.example.DemoService": {
                "context_abs": "/tmp/DemoService.context.md",
                "symbols_abs": "/tmp/DemoService.symbols.md",
            }
        },
        max_fix_attempts=3,
    )

    assert ok is True
    assert repair_session_id == "compile-fix-session"
    assert len(attempts) == 3
    assert client.send_message_split.call_count == 2
    assert client.poll_completion.call_count == 2
    # volatile tail (3rd positional arg) contains the per-call context paths
    volatile_tail = client.send_message_split.call_args_list[0].args[2]
    assert "/tmp/DemoService.context.md" in volatile_tail
    assert "/tmp/DemoService.symbols.md" in volatile_tail


def test_compile_fix_loop_fails_after_max(monkeypatch):
    def always_fail(repo, mod, timeout=300):
        return False, "[ERROR] broken"

    monkeypatch.setattr("uta.graph.nodes._compile_test", always_fail)

    client = MagicMock()
    client.create_session = MagicMock(return_value="compile-fix-session")
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module=None,
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag="",
        max_fix_attempts=2,
    )

    assert ok is False
    assert repair_session_id == "compile-fix-session"
    assert client.send_message_split.call_count == 2


def test_compile_fix_loop_ignores_unrelated_errors_in_ci_incremental(monkeypatch):
    unrelated_error = (
        "[ERROR] /repo/biz/src/test/java/com/example/OtherTest.java:[5,1] cannot find symbol\n"
        "  symbol:   class MissingOther\n"
    )

    monkeypatch.setattr("uta.graph.nodes._compile_test", lambda *args, **kwargs: (False, unrelated_error))

    client = MagicMock()
    client.create_session = MagicMock(return_value="compile-fix-session")
    client.send_message_split = MagicMock()

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=1,
        state={"quality_mode": "ci_incremental"},
    )

    assert ok is True
    assert repair_session_id is None
    client.send_message_split.assert_not_called()


def test_compile_fix_loop_keeps_related_errors_in_ci_incremental(monkeypatch):
    related_error = (
        "[ERROR] /repo/biz/src/test/java/com/example/FooTest.java:[5,1] cannot find symbol\n"
        "  symbol:   class MissingFoo\n"
    )

    monkeypatch.setattr("uta.graph.nodes._compile_test", lambda *args, **kwargs: (False, related_error))

    client = MagicMock()
    client.create_session = MagicMock(return_value="compile-fix-session")
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=1,
        state={"quality_mode": "ci_incremental"},
    )

    assert ok is False
    assert repair_session_id == "compile-fix-session"
    assert client.send_message_split.call_count == 1


def test_hopeless_loop_guard_stops_on_identical_errors(monkeypatch):
    """Strategy G: when two consecutive attempts yield identical error signatures, bail early."""
    # Same error output on both attempts → identical signatures → stop after attempt 2.
    error_out = "[ERROR] /repo/src/test/java/com/example/FooTest.java:[5,1] package com.example.svc does not exist"
    call_count = []

    def always_same_error(repo, mod, timeout=300):
        call_count.append(1)
        return False, error_out

    monkeypatch.setattr("uta.graph.nodes._compile_test", always_same_error)

    client = MagicMock()
    client.create_session = MagicMock(return_value="hopeless-session")
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module=None,
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag="",
        max_fix_attempts=5,  # would run 5 times without guard
    )

    assert ok is False
    # Guard should fire after attempt 2: compile called twice, message sent once
    assert len(call_count) == 2
    assert client.send_message_split.call_count == 1


def test_error_delta_keeps_recurring_clusters_visible(monkeypatch):
    """Strategy D: after a partial fix, new and recurring compile clusters stay visible."""
    first_error = (
        "[ERROR] /repo/src/test/java/com/example/FooTest.java:[5,1] package com.example.svc does not exist\n"
        "[ERROR] /repo/src/test/java/com/example/FooTest.java:[14,17] cannot find symbol\n"
        "  symbol:   class WaitingPickUp\n"
        "  location: class com.example.FooTest\n"
    )
    second_error = (
        # First error persists (same signature), second is fixed, new one appears
        "[ERROR] /repo/src/test/java/com/example/FooTest.java:[5,1] package com.example.svc does not exist\n"
        "[ERROR] /repo/src/test/java/com/example/FooTest.java:[30,9] incompatible types: String cannot be converted to Long\n"
        "  required: java.lang.Long\n"
    )
    outputs = [first_error, second_error, ""]  # third call succeeds

    def sequenced_compile(repo, mod, timeout=300):
        out = outputs.pop(0) if outputs else ""
        return (not out, out)

    monkeypatch.setattr("uta.graph.nodes._compile_test", sequenced_compile)

    prompts_sent = []
    client = MagicMock()
    client.create_session = MagicMock(return_value="delta-session")
    # send_message_split(session_id, stable, volatile, **kwargs) — capture volatile tail
    client.send_message_split = MagicMock(
        side_effect=lambda sid, stable, volatile, **kw: prompts_sent.append(volatile)
    )
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, _ = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module=None,
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag="",
        max_fix_attempts=5,
    )

    assert ok is True
    assert len(prompts_sent) == 2
    # Second volatile tail should show both the new wrong_type error and the recurring missing_import cluster.
    second_prompt = prompts_sent[1]
    assert "incompatible types" in second_prompt or "WRONG_TYPE" in second_prompt
    assert "package com.example.svc does not exist" in second_prompt
    assert "RECURRING ERROR CLUSTERS" in second_prompt


def test_compile_fix_loop_uses_effective_model_for_session_and_send(monkeypatch):
    monkeypatch.setattr("uta.graph.nodes._compile_test", lambda *args, **kwargs: (False, "[ERROR] broken"))
    monkeypatch.setattr("uta.opencode.tiered_router.effective_model", lambda phase: "ollama/qwen3:8b")

    client = MagicMock()
    client.create_session = MagicMock(return_value="cheap-session")
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "timeout", "result": ""})

    ok, repair_session_id = run_compile_fix_loop(
        repo_path="/tmp/repo",
        module=None,
        batch=["com.example.Foo"],
        session_id="sess-1",
        client=client,
        maven_module_flag="",
        max_fix_attempts=1,
    )

    assert ok is False
    assert repair_session_id is None
    client.create_session.assert_called_once_with(model_id="ollama/qwen3:8b")
    assert client.send_message_split.call_args.kwargs["model_id"] == "ollama/qwen3:8b"


def test_generation_compile_gate_wraps_fix_loop(monkeypatch):
    monkeypatch.setattr("uta.graph.nodes.run_compile_fix_loop", lambda *args, **kwargs: (True, "compile-fix-session"))

    stages = []
    monkeypatch.setattr("uta.graph.nodes._set_stage", lambda state, stage, detail=None: stages.append((stage, detail)))

    ok, seconds, repair_session_id = _run_generation_compile_gate(
        state={},
        repo_path="/tmp/repo",
        module="biz",
        batch=["com.example.DemoService"],
        session_id="sess-1",
        client=MagicMock(),
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=3,
    )

    assert ok is True
    assert repair_session_id == "compile-fix-session"
    assert seconds >= 0.0
    assert stages == [("compile_verification", "batch=1")]
