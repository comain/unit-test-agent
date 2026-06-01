"""Tests for UTA project summary generation."""

import os
from pathlib import Path

from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.graph_builder import GraphBuilder
from uta.engine.project_summary_artifacts import (
    COMPILE_FACTS_FILENAME,
    REPO_SUMMARY_FILENAME,
    CONTEXT_SUMMARY_FILENAME,
    TEST_GUIDANCE_FILENAME,
    OPENCODE_INIT_OUTPUT_FILENAME,
    SESSION_RETROSPECT_FILENAME,
    STAGE_INTROSPECT_FILENAME,
    UTA_GENERATED_MARKER,
    OPENCODE_INIT_MERGE_HEADER,
    sync_project_summaries,
    prompt_template_paths,
    maybe_run_project_init_command,
    maybe_run_opencode_init_slash,
    merge_compile_fix_facts,
    write_session_retrospect,
    ensure_stage_introspect_file,
    append_stage_introspect,
)
from uta.engine.project_summary import ProjectSummaryProvider, make_project_summary_provider


def test_sync_writes_context_and_repo_summary(fixtures_dir, tmp_path):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")
    results = [parser.parse_file(service_path), parser.parse_file(mapper_path)]
    graph = GraphBuilder().build(results)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        "<project><groupId>com.ex</groupId><artifactId>demo</artifactId>"
        "<version>1.0</version><java.version>1.8</java.version></project>",
        encoding="utf-8",
    )

    out = sync_project_summaries(str(repo), graph, None)
    ctx_file = repo / ".uta_cache" / "context" / CONTEXT_SUMMARY_FILENAME
    guidance_file = repo / ".uta_cache" / "context" / TEST_GUIDANCE_FILENAME
    assert ctx_file.exists()
    assert guidance_file.exists()
    body = ctx_file.read_text(encoding="utf-8")
    assert "SampleService" in body or "class" in body.lower()
    guidance = guidance_file.read_text(encoding="utf-8")
    assert "Source-of-Truth Lookup Order" in guidance
    assert "Test Construction Constraints" in guidance

    rs = repo / REPO_SUMMARY_FILENAME
    assert rs.exists()
    assert UTA_GENERATED_MARKER in rs.read_text(encoding="utf-8")

    pp = prompt_template_paths(str(repo), ctx_file.parent)
    assert pp["repo_summary_exists"] is True
    assert str(ctx_file.resolve()) == pp["context_summary_abs"]
    assert str(guidance_file.resolve()) == pp["test_guidance_abs"]


def test_project_summary_facade_writes_python_artifacts(tmp_path):
    repo = tmp_path / "pyrepo"
    (repo / "jobs").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo / "jobs" / "forecast.py").write_text(
        "def forecast_for_store(store_id):\n    return store_id\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import forecast_for_store\n\n"
        "def test_forecast_for_store():\n"
        "    assert forecast_for_store(3) == 3\n",
        encoding="utf-8",
    )

    out = sync_project_summaries(str(repo), None, None, language="python", max_files=10)

    assert Path(out["context_summary_abs"]).is_file()
    assert Path(out["test_guidance_abs"]).is_file()
    assert Path(out["repo_summary_abs"]).is_file()
    context_body = Path(out["context_summary_abs"]).read_text(encoding="utf-8")
    guidance = Path(out["test_guidance_abs"]).read_text(encoding="utf-8")
    repo_summary = Path(out["repo_summary_abs"]).read_text(encoding="utf-8")
    assert "Language: `python`" in context_body
    assert "forecast_for_store" in (repo / ".uta_cache" / "python_context" / "index.json").read_text(encoding="utf-8")
    assert "Preserve Python 2 compatibility" in guidance
    assert "**Language**: `python`" in repo_summary


def test_project_summary_provider_factory_keeps_backend_contract(fixtures_dir, tmp_path):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    graph = GraphBuilder().build([parser.parse_file(service_path)])

    java_provider = make_project_summary_provider("java", tmp_path / "java", graph=graph)
    python_provider = make_project_summary_provider("python", tmp_path / "python")

    assert isinstance(java_provider, ProjectSummaryProvider)
    assert isinstance(python_provider, ProjectSummaryProvider)
    assert java_provider.language == "java"
    assert python_provider.language == "python"


def test_sync_guidance_discovers_named_sibling_api_repos(fixtures_dir, tmp_path):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    results = [parser.parse_file(service_path)]
    graph = GraphBuilder().build(results)

    workspace = tmp_path / "services"
    repo = workspace / "sample-service"
    api_repo = workspace / "example-services-outbound-core-api"
    repo.mkdir(parents=True)
    (api_repo / "model" / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (api_repo / "model" / "src" / "main" / "java" / "com" / "example" / "RemoteDto.java").write_text(
        "package com.example; public class RemoteDto {}",
        encoding="utf-8",
    )
    (repo / "pom.xml").write_text(
        "<project><artifactId>demo</artifactId></project>",
        encoding="utf-8",
    )

    sync_project_summaries(str(repo), graph, None)

    guidance = (repo / ".uta_cache" / "context" / TEST_GUIDANCE_FILENAME).read_text(encoding="utf-8")
    assert str(api_repo.resolve()) in guidance
    assert "Do not unpack jars, run decompilers, or inspect `.class` files" in guidance


def test_maybe_run_init_skips_when_summary_present(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / REPO_SUMMARY_FILENAME).write_text(
        "# manual project summary\n\n" + "x" * 40,
        encoding="utf-8",
    )
    called = []

    def fake_run(*a, **k):
        called.append(True)
        raise AssertionError("should not run")

    monkeypatch.setattr("uta.engine.project_summary_artifacts.subprocess.run", fake_run)
    assert maybe_run_project_init_command(str(repo), "echo hi") is False
    assert not called


def test_opencode_init_slash_harvests_agents(tmp_path, monkeypatch):
    repo = tmp_path / "oc"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Project\n\n" + "y" * 60, encoding="utf-8")
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            return "init-s1"

        def delete_session(self, sid):
            assert sid == "init-s1"

        def send_message_and_get_user_info(self, sid, msg, timeout=30, model_id=None):
            assert sid == "init-s1"
            assert "create AGENTS.md" in msg
            assert model_id == "openai/gpt-5.4"
            return {
                "id": "msg-1",
                "model": {"providerID": "google", "modelID": "gemini-3.1-pro-preview"},
            }

        def init_session(self, sid, message_id, provider_id, model_id):
            assert (sid, message_id, provider_id, model_id) == (
                "init-s1",
                "msg-1",
                "openai",
                "gpt-5.4",
            )

        def latest_completion(self, sid):
            return {"type": "completed", "result": "ok"}

        def get_messages(self, sid):
            return []

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=30) is True
    out = (repo / REPO_SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert OPENCODE_INIT_MERGE_HEADER in out
    assert "# Project" in out
    assert (repo / ".uta_cache" / "context" / OPENCODE_INIT_OUTPUT_FILENAME).exists()


def test_opencode_init_prefers_provider_derived_from_model(tmp_path, monkeypatch):
    repo = tmp_path / "oc_provider"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Project\n\n" + "y" * 60, encoding="utf-8")
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openrouter")
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    calls = []

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            calls.append(("create", model_id, provider_id))
            return "init-s1"

        def delete_session(self, sid):
            assert sid == "init-s1"

        def send_message_and_get_user_info(self, sid, msg, timeout=30, model_id=None):
            calls.append(("user_msg", sid, model_id))
            return {
                "id": "msg-1",
                "model": {"providerID": "google", "modelID": "gemini-3.1-pro-preview"},
            }

        def init_session(self, sid, message_id, provider_id, model_id):
            calls.append(("init", sid, message_id, provider_id, model_id))

        def latest_completion(self, sid):
            return {"type": "completed", "result": "ok"}

        def get_messages(self, sid):
            return []

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=30) is True
    assert calls == [
        ("create", "openai/gpt-5.4", "openai"),
        ("user_msg", "init-s1", "openai/gpt-5.4"),
        ("init", "init-s1", "msg-1", "openai", "gpt-5.4"),
    ]


def test_opencode_init_replaces_uta_generated_stub(tmp_path, monkeypatch):
    repo = tmp_path / "oc_stub"
    repo.mkdir()
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    (repo / REPO_SUMMARY_FILENAME).write_text(
        UTA_GENERATED_MARKER + "\n\n# Stub\n\n" + "s" * 40,
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Project\n\n" + "y" * 60, encoding="utf-8")
    called = []

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            return "init-s1"

        def delete_session(self, sid):
            assert sid == "init-s1"

        def send_message_and_get_user_info(self, sid, msg, timeout=30, model_id=None):
            called.append((sid, msg, model_id))
            return {
                "id": "msg-1",
                "model": {"providerID": "google", "modelID": "gemini-3.1-pro-preview"},
            }

        def init_session(self, sid, message_id, provider_id, model_id):
            called.append((sid, message_id, provider_id, model_id))

        def latest_completion(self, sid):
            return {"type": "completed", "result": "ok"}

        def get_messages(self, sid):
            return []

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=30) is True
    assert called == [
        ("init-s1", "Initialize this repository and create AGENTS.md with project-specific guidance.", "openai/gpt-5.4"),
        ("init-s1", "msg-1", "openai", "gpt-5.4"),
    ]
    out = (repo / REPO_SUMMARY_FILENAME).read_text(encoding="utf-8")
    assert OPENCODE_INIT_MERGE_HEADER in out
    assert "# Project" in out


def test_opencode_init_best_effort_when_no_completion_or_artifact(tmp_path, monkeypatch):
    repo = tmp_path / "oc_timeout"
    repo.mkdir()
    calls = []
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openrouter")
    monkeypatch.setattr("uta.config.settings.opencode_model", "openrouter/z-ai/glm-5.1")

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            return "init-s1"

        def delete_session(self, sid):
            assert sid == "init-s1"

        def send_message_and_get_user_info(self, sid, msg, timeout=30, model_id=None):
            calls.append((sid, msg, model_id))
            return {
                "id": "msg-1",
                "model": {"providerID": "google", "modelID": "gemini-3.1-pro-preview"},
            }

        def init_session(self, sid, message_id, provider_id, model_id):
            calls.append((sid, message_id, provider_id, model_id))

        def latest_completion(self, sid):
            return None

        def get_messages(self, sid):
            return []

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    monkeypatch.setattr("uta.engine.project_summary_artifacts.time.sleep", lambda _: None)
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=0) is True
    assert calls == [
        ("init-s1", "Initialize this repository and create AGENTS.md with project-specific guidance.", "openrouter/z-ai/glm-5.1"),
        ("init-s1", "msg-1", "openrouter", "z-ai/glm-5.1"),
    ]
    assert not (repo / REPO_SUMMARY_FILENAME).exists()


def test_opencode_init_enabled_by_default(tmp_path):
    repo = tmp_path / "oc_enabled"
    repo.mkdir()
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=0) is True


def test_opencode_init_skips_when_summary_exists(tmp_path, monkeypatch):
    repo = tmp_path / "oc2"
    repo.mkdir()
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    (repo / REPO_SUMMARY_FILENAME).write_text("# ok\n\n" + "z" * 40, encoding="utf-8")
    called = []

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            called.append("create")
            return "init-s1"

        def send_message_and_get_user_info(self, *a, **k):
            called.append(True)

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    assert maybe_run_opencode_init_slash(str(repo), "s1") is False
    assert not called


def test_opencode_init_follows_task_subsession_and_rejects_generic_output(tmp_path, monkeypatch):
    repo = tmp_path / "oc_nested"
    repo.mkdir()
    monkeypatch.setattr("uta.config.settings.opencode_init_slash_enabled", True)
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openrouter")
    monkeypatch.setattr("uta.config.settings.opencode_model", "openrouter/z-ai/glm-5.1")

    class FakeClient:
        def create_session(self, model_id=None, provider_id=None):
            return "init-s1"

        def delete_session(self, sid):
            assert sid == "init-s1"

        def send_message_and_get_user_info(self, sid, msg, timeout=30, model_id=None):
            assert sid == "init-s1"
            assert "create AGENTS.md" in msg
            assert model_id == "openrouter/z-ai/glm-5.1"
            return {
                "id": "msg-1",
                "model": {"providerID": "google", "modelID": "gemini-3.1-pro-preview"},
            }

        def init_session(self, sid, message_id, provider_id, model_id):
            assert (sid, message_id, provider_id, model_id) == (
                "init-s1",
                "msg-1",
                "openrouter",
                "z-ai/glm-5.1",
            )

        def latest_completion(self, sid):
            return {"type": "completed", "result": ""}

        def get_messages(self, sid):
            if sid == "init-s1":
                return [
                    {
                        "info": {"sessionID": "init-s1", "role": "assistant"},
                        "parts": [
                            {
                                "type": "tool",
                                "state": {"metadata": {"sessionId": "child-s1"}},
                            }
                        ],
                    }
                ]
            if sid == "child-s1":
                return [
                    {
                        "info": {"sessionID": "child-s1", "role": "assistant"},
                        "parts": [
                            {
                                "type": "text",
                                "text": "I am initialized and ready to help. Execute /init command.",
                            }
                        ],
                    }
                ]
            return []

    monkeypatch.setattr("uta.opencode.client.OpenCodeAuthClient", lambda *_a, **_kw: FakeClient())
    assert maybe_run_opencode_init_slash(str(repo), "s1", timeout=0) is True
    out = (repo / ".uta_cache" / "context" / OPENCODE_INIT_OUTPUT_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "did not yield meaningful project bootstrap output" in out


def test_maybe_run_init_when_missing(tmp_path, monkeypatch):
    repo = tmp_path / "r2"
    repo.mkdir()

    def fake_run(*a, **k):
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("uta.engine.project_summary_artifacts.subprocess.run", fake_run)
    assert maybe_run_project_init_command(str(repo), "true") is True


def test_write_session_retrospect_persists_markdown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = write_session_retrospect(
        str(repo),
        {
            "session_id": "ses-1",
            "tool_count": 12,
            "patch_count": 3,
            "hints": ["Prefer sibling api repos before jars."],
            "observations": ["Session entered mutation-driven repair loops."],
        },
    )
    path = repo / ".uta_cache" / "context" / SESSION_RETROSPECT_FILENAME
    assert str(path.resolve()) == out
    body = path.read_text(encoding="utf-8")
    assert "Prompt Improvements" in body
    assert "Prefer sibling api repos before jars." in body


def test_stage_introspect_persists_per_stage_and_dedupes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    out = ensure_stage_introspect_file(str(repo), "mutation_fix")
    path = repo / ".uta_cache" / "context" / "introspect" / "mutation_fix" / STAGE_INTROSPECT_FILENAME
    assert str(path.resolve()) == out

    append_stage_introspect(
        str(repo),
        "mutation_fix",
        ["Bias first-round tests toward mutation resistance.", "Bias first-round tests toward mutation resistance."],
    )
    body = path.read_text(encoding="utf-8")
    assert body.count("Bias first-round tests toward mutation resistance.") == 1
    assert "No prior stage-specific lessons" not in body


def test_merge_compile_fix_facts_persists_and_dedupes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = merge_compile_fix_facts(
        str(repo),
        [
            "DiffEvent.getOrderNo() returns Long",
            "ActorMessage is an interface",
            "DiffEvent.getOrderNo() returns Long",
        ],
    )
    path = repo / ".uta_cache" / "context" / COMPILE_FACTS_FILENAME
    assert str(path.resolve()) == out
    body = path.read_text(encoding="utf-8")
    assert body.count("DiffEvent.getOrderNo() returns Long") == 1
    assert "ActorMessage is an interface" in body
