from pathlib import Path

from uta.graph.nodes import generate_and_validate, _write_generation_plan


class DummyContextBuilder:
    def __init__(self, repo_path, graph, flows):
        self.repo_path = repo_path

    def export_context_files(self):
        ctx = Path(self.repo_path) / ".uta_cache" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        return ctx

    def get_class_source_path(self, class_fqn: str) -> str:
        return str(Path(self.repo_path) / "src" / "main" / "java" / "Demo.java")


class DummyClient:
    def __init__(self, *_a, **_kw):
        self.sent = []
        self.timeouts = []
        self.created = []

    def create_session(self, model_id=None, permission=None):
        session_id = f"created-{len(self.created) + 1}"
        self.created.append((session_id, model_id, permission))
        return session_id

    def send_message(self, session_id, prompt, model_id=None):
        self.sent.append((session_id, model_id, prompt))

    def send_message_split(self, session_id, stable, volatile, model_id=None):
        self.send_message(session_id, stable + volatile, model_id=model_id)

    def analyze_session_retrospect(self, session_id):
        return {"session_id": session_id, "hints": [], "observations": []}

    def analyze_session_tokens(self, session_id):
        return {
            "assistant_messages": 1,
            "main_model_tokens": {"input": 1, "output": 1, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 2},
            "small_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "total_tokens": {"input": 1, "output": 1, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 2},
            "by_model": {"openai/gpt-5.4": {"input": 1, "output": 1, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 2}},
        }

    def poll_completion(self, session_id, timeout=0, on_update=None):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            return {"type": "completed", "result": "PUBLIC METHODS\n- demo\nBRANCH AXES\n- happy/guard"}
        return {"type": "timeout", "result": ""}

    def analyze_session_retrospect(self, session_id):
        return {"session_id": session_id, "hints": ["Avoid broad root-level searches."], "observations": []}


class DummyGenerationCompleteClient(DummyClient):
    def poll_completion(self, session_id, timeout=0, on_update=None):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            return {"type": "completed", "result": "PUBLIC METHODS\n- demo\nBRANCH AXES\n- happy/guard"}
        return {"type": "completed", "result": "Generation complete"}


class DummyPlanFinalizeClient(DummyClient):
    def poll_completion(self, session_id, timeout=0, on_update=None):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            return {"type": "completed", "result": ""}
        return {"type": "completed", "result": "PUBLIC METHODS\n- demo\nBRANCH AXES\n- happy/guard\nPLANNED TESTS\n- demo path\nCOVERAGE RISKS\n- none"}


def test_generate_and_validate_returns_generation_timeout_and_retrospect(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda repo_path, graph, module, **kwargs: {})
    monkeypatch.setattr(
        "uta.graph.nodes.prompt_template_paths",
        lambda repo_path, context_dir: {
            "repo_summary_abs": str(Path(repo_path) / ".uta_summary.md"),
            "context_summary_abs": str(Path(context_dir) / "project_summary.md"),
            "test_guidance_abs": str(Path(context_dir) / "test_generation_guidance.md"),
            "compile_facts_abs": str(Path(context_dir) / "compile_fix_facts.md"),
            "compile_facts_exists": False,
            "repo_summary_exists": False,
            "test_guidance_exists": False,
        },
    )
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", DummyClient)
    monkeypatch.setattr("uta.graph.nodes.write_session_retrospect", lambda repo_path, retrospect: str(Path(repo_path) / ".uta_cache" / "context" / "session_retrospect.md"))
    monkeypatch.setattr("uta.config.settings.opencode_generation_timeout_ratio", 0.01)

    state = {
        "repo_path": str(repo),
        "module": None,
        "days": 1,
        "max_files": 1,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen",
        "started_at": 0.0,
        "candidates": ["com.example.DemoService"],
        "current_class": "com.example.DemoService",
        "current_batch": ["com.example.DemoService"],
        "graph": object(),
        "flows": [],
        "session_id": "sess-1",
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
    }

    out = generate_and_validate(state)  # type: ignore[arg-type]
    res = out["results"]["com.example.DemoService"]
    assert res["status"] == "GENERATION_TIMEOUT"
    assert "timed out" in res["output"]
    assert out["stopped_early"] is True
    # session_id is the last created session (plan + generate each get one)
    retrospect = out["session_retrospect"]
    assert retrospect.get("session_id", "").startswith("created-")
    assert retrospect.get("hints") == ["Avoid broad root-level searches."]


def test_generate_and_validate_can_stop_after_plan_stage(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda repo_path, graph, module, **kwargs: {})
    monkeypatch.setattr(
        "uta.graph.nodes.prompt_template_paths",
        lambda repo_path, context_dir: {
            "repo_summary_abs": str(Path(repo_path) / ".uta_summary.md"),
            "context_summary_abs": str(Path(context_dir) / "project_summary.md"),
            "test_guidance_abs": str(Path(context_dir) / "test_generation_guidance.md"),
            "compile_facts_abs": str(Path(context_dir) / "compile_fix_facts.md"),
            "compile_facts_exists": False,
            "repo_summary_exists": False,
            "test_guidance_exists": False,
        },
    )
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", DummyClient)
    monkeypatch.setattr("uta.graph.nodes.write_session_retrospect", lambda repo_path, retrospect: str(Path(repo_path) / ".uta_cache" / "context" / "session_retrospect.md"))

    state = {
        "repo_path": str(repo),
        "module": None,
        "days": 1,
        "max_files": 1,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen",
        "started_at": 0.0,
        "stop_after_stage": "plan_tests",
        "candidates": ["com.example.DemoService"],
        "current_class": "com.example.DemoService",
        "current_batch": ["com.example.DemoService"],
        "graph": object(),
        "flows": [],
        "session_id": "sess-1",
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
    }

    out = generate_and_validate(state)  # type: ignore[arg-type]
    assert out["finished"] is True
    assert out["stopped_early"] is True
    assert out["current_stage"] == "plan_tests"
    assert out["current_batch"] == []
    assert out["results"] == {}


def test_generate_and_validate_forces_final_plan_when_initial_plan_text_missing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda repo_path, graph, module, **kwargs: {})
    monkeypatch.setattr(
        "uta.graph.nodes.prompt_template_paths",
        lambda repo_path, context_dir: {
            "repo_summary_abs": str(Path(repo_path) / ".uta_summary.md"),
            "context_summary_abs": str(Path(context_dir) / "project_summary.md"),
            "test_guidance_abs": str(Path(context_dir) / "test_generation_guidance.md"),
            "compile_facts_abs": str(Path(context_dir) / "compile_fix_facts.md"),
            "compile_facts_exists": False,
            "repo_summary_exists": False,
            "test_guidance_exists": False,
        },
    )
    client = DummyPlanFinalizeClient()
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes.write_session_retrospect", lambda repo_path, retrospect: str(Path(repo_path) / ".uta_cache" / "context" / "session_retrospect.md"))

    state = {
        "repo_path": str(repo),
        "module": None,
        "days": 1,
        "max_files": 1,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen",
        "started_at": 0.0,
        "stop_after_stage": "plan_tests",
        "candidates": ["com.example.DemoService"],
        "current_class": "com.example.DemoService",
        "current_batch": ["com.example.DemoService"],
        "graph": object(),
        "flows": [],
        "session_id": "sess-1",
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
    }

    out = generate_and_validate(state)  # type: ignore[arg-type]
    assert out["finished"] is True
    assert out["stopped_early"] is True
    assert out["current_stage"] == "plan_tests"
    assert len(client.sent) == 2
    assert "Emit the final plan now" in client.sent[1][2]


def test_generate_and_validate_can_stop_after_generation_stage(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda repo_path, graph, module, **kwargs: {})
    monkeypatch.setattr(
        "uta.graph.nodes.prompt_template_paths",
        lambda repo_path, context_dir: {
            "repo_summary_abs": str(Path(repo_path) / ".uta_summary.md"),
            "context_summary_abs": str(Path(context_dir) / "project_summary.md"),
            "test_guidance_abs": str(Path(context_dir) / "test_generation_guidance.md"),
            "compile_facts_abs": str(Path(context_dir) / "compile_fix_facts.md"),
            "compile_facts_exists": False,
            "repo_summary_exists": False,
            "test_guidance_exists": False,
        },
    )
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", DummyGenerationCompleteClient)
    monkeypatch.setattr("uta.graph.nodes.write_session_retrospect", lambda repo_path, retrospect: str(Path(repo_path) / ".uta_cache" / "context" / "session_retrospect.md"))
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.5, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 2.5, None))
    monkeypatch.setattr("uta.graph.nodes._expected_test_file_rel", lambda module, class_fqn: "src/test/java/DemoServiceTest.java")
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])

    state = {
        "repo_path": str(repo),
        "module": None,
        "days": 1,
        "max_files": 1,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen",
        "started_at": 0.0,
        "stop_after_stage": "generation",
        "candidates": ["com.example.DemoService"],
        "current_class": "com.example.DemoService",
        "current_batch": ["com.example.DemoService"],
        "graph": object(),
        "flows": [],
        "session_id": "sess-1",
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
    }

    out = generate_and_validate(state)  # type: ignore[arg-type]
    assert out["finished"] is True
    assert out["stopped_early"] is True
    assert out["current_stage"] == "generation"
    assert out["current_batch"] == []


def test_generate_and_validate_resume_uses_saved_plan_and_skips_planning(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_generation_plan(str(repo), "old-session", ["com.example.DemoService"], "SAVED PLAN BODY")

    monkeypatch.setattr("uta.graph.nodes.ContextBuilder", DummyContextBuilder)
    monkeypatch.setattr("uta.graph.nodes.sync_project_summaries", lambda repo_path, graph, module, **kwargs: {})
    monkeypatch.setattr(
        "uta.graph.nodes.prompt_template_paths",
        lambda repo_path, context_dir: {
            "repo_summary_abs": str(Path(repo_path) / ".uta_summary.md"),
            "context_summary_abs": str(Path(context_dir) / "project_summary.md"),
            "test_guidance_abs": str(Path(context_dir) / "test_generation_guidance.md"),
            "compile_facts_abs": str(Path(context_dir) / "compile_fix_facts.md"),
            "compile_facts_exists": False,
            "repo_summary_exists": False,
            "test_guidance_exists": False,
        },
    )
    client = DummyGenerationCompleteClient()
    monkeypatch.setattr("uta.graph.nodes.OpenCodeClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("uta.graph.nodes.write_session_retrospect", lambda repo_path, retrospect: str(Path(repo_path) / ".uta_cache" / "context" / "session_retrospect.md"))
    monkeypatch.setattr("uta.graph.nodes._prompt_for_missing_batch_files", lambda **kwargs: [])
    monkeypatch.setattr("uta.graph.nodes._run_generation_compile_gate", lambda **kwargs: (True, 1.0, None))
    monkeypatch.setattr("uta.graph.nodes._run_generation_test_gate", lambda **kwargs: (True, 1.0, None))

    state = {
        "repo_path": str(repo),
        "module": None,
        "days": 1,
        "max_files": 1,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen",
        "started_at": 0.0,
        "stop_after_stage": "generation",
        "resume": True,
        "candidates": ["com.example.DemoService"],
        "current_class": "com.example.DemoService",
        "current_batch": ["com.example.DemoService"],
        "graph": object(),
        "flows": [],
        "session_id": "sess-1",
        "results": {},
        "phase_timings": {},
        "session_retrospect": {},
        "session_token_usage": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
    }

    out = generate_and_validate(state)  # type: ignore[arg-type]

    assert out["stopped_early"] is True
    assert out["current_stage"] == "generation"
    assert len(client.sent) == 1
    assert "SAVED PLAN BODY" in client.sent[0][2]
