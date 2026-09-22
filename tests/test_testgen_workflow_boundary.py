from pathlib import Path


def test_neutral_testgen_workflow_does_not_import_language_implementations():
    neutral_files = (
        Path("uta/testgen/backend.py"),
        Path("uta/testgen/delivery.py"),
        Path("uta/testgen/graph/nodes.py"),
        Path("uta/testgen/graph/state.py"),
        Path("uta/testgen/graph/workflow.py"),
        Path("uta/testgen/git.py"),
        Path("uta/testgen/progress.py"),
        Path("uta/testgen/workspace_setup.py"),
    )

    for path in neutral_files:
        source = path.read_text(encoding="utf-8")
        assert "uta.language." not in source, f"{path} bypasses the configured language backend"


def _java_generation_source() -> str:
    """Java generation is a package of focused modules; read all of it."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path("uta/language/java/generation").glob("*.py"))
    )


def test_java_generation_is_a_backend_not_a_workflow_module():
    assert not Path("uta/language/java/workflow.py").exists()
    assert Path("uta/language/java/generation/__init__.py").is_file()
    assert "_JavaWorkflowContextProvider" not in _java_generation_source()


def test_python_batch_is_a_thin_facade_over_its_generation_backend():
    batch_source = Path("uta/language/python/batch.py").read_text(encoding="utf-8")

    assert Path("uta/language/python/generation.py").is_file()
    assert len(batch_source.splitlines()) < 30
    assert "run_python_batch_generation" in batch_source


def test_graph_resolves_its_backend_from_language_config():
    from uta.testgen.graph import nodes

    backend = nodes._backend({"language": "java", "candidates": []})

    assert backend.__name__ == "uta.language.java.generation_backend"
    for operation in (
        "prepare_workspace",
        "baseline_validate",
        "select_targets",
        "prepare_context",
        "select_next_target",
        "deliver_target",
        "finalize",
    ):
        assert callable(getattr(backend, operation))


def test_graph_resolves_python_backend_from_language_config():
    from uta.testgen.graph import nodes

    backend = nodes._backend({"language": "python", "candidates": []})

    assert backend.__name__ == "uta.language.python.generation_backend"


def test_delivery_runs_git_through_agent_core_with_product_credentials(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_core.git import GitWorkspace
    from uta.shared.config import settings
    from uta.testgen import delivery

    observed = {}

    def execute(workspace, path, *args, **kwargs):
        observed.update(
            path=path,
            args=args,
            check=kwargs["check"],
            credentials=workspace.credentials,
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(settings, "ci_git_ssh_key_path", "/deploy/key")
    monkeypatch.setattr(settings, "ci_git_access_token", "")
    monkeypatch.setattr(GitWorkspace, "execute", execute)

    result = delivery._git_run(str(tmp_path), "status", capture_output=True, check=False)

    assert result.stdout == "ok"
    assert observed["path"] == tmp_path
    assert observed["args"] == ("status",)
    assert observed["check"] is False
    assert observed["credentials"].ssh_key_path == "/deploy/key"


def test_durable_cutover_stays_language_and_legacy_runtime_neutral():
    for path in (
        Path("uta/testgen/cutover.py"),
        Path("uta/testgen/graph/durable_cycle.py"),
        Path("uta/testgen/graph/nodes.py"),
        Path("uta/testgen/graph/cycle.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "uta.language." not in source
        assert "run_agent_node(" not in source
        assert "AgentRuntime(" not in source


def test_legacy_runtime_implementations_are_deleted():
    for path in (
        Path("uta/testgen/agent_runtime.py"),
        Path("uta/testgen/llm_session.py"),
    ):
        assert not path.exists()
    assert Path("uta/testgen/harness.py").is_file()
    neutral_contract = Path("uta/testgen/backend.py").read_text(encoding="utf-8")
    nodes_source = Path("uta/testgen/graph/nodes.py").read_text(encoding="utf-8")
    assert "def run_generation_cycle" not in neutral_contract
    assert ".run_generation_cycle(" not in nodes_source


def test_durable_only_runtime_has_no_compatibility_flag_reads_or_composite_exports():
    for path in (
        Path("uta/app/cli.py"),
        Path("uta/shared/config.py"),
        Path("uta/language/java/batch.py"),
        Path("uta/language/python/generation_backend.py"),
        Path("uta/testgen/cutover.py"),
        Path("uta/testgen/graph/durable_cycle.py"),
        Path("uta/testgen/graph/state.py"),
    ):
        assert "generation_cycle_v2_enabled" not in path.read_text(encoding="utf-8")

    from uta.language.java import generation_backend as java_backend
    from uta.language.python import generation_backend as python_backend

    assert "run_generation_cycle" not in java_backend.__all__
    assert "run_generation_cycle" not in python_backend.__all__


def test_java_durable_backend_does_not_depend_on_legacy_orchestration():
    generation_source = _java_generation_source()
    backend_source = Path("uta/language/java/generation_backend.py").read_text(
        encoding="utf-8"
    )

    for symbol in (
        "def generate_and_validate",
        "def _create_java_agent_runtime",
        "run_agent_node",
        "AgentRuntime",
        "llm_session",
    ):
        assert symbol not in generation_source
    assert "from uta.language.java.generation import" not in backend_source
    assert Path("uta/language/java/phases/ports.py").is_file()


def test_java_cli_does_not_expose_concrete_client_resume_gates():
    source = Path("uta/app/generation_commands.py").read_text(encoding="utf-8")

    assert '@main.command("resume-gates")' not in source
    assert "OpenCodeClient" not in source


def test_python_durable_backend_does_not_depend_on_legacy_orchestration():
    generation_source = Path("uta/language/python/generation.py").read_text(
        encoding="utf-8"
    )
    backend_source = Path(
        "uta/language/python/generation_backend.py"
    ).read_text(encoding="utf-8")
    phases_source = Path("uta/language/python/phases.py").read_text(
        encoding="utf-8"
    )

    for symbol in (
        "def run_python_batch_request",
        "def _make_agent_runtime",
        "def _invoke_runtime_factory",
        "def _run_python_agent_node",
        "run_agent_node",
        "AgentRuntime",
        "runtime_factory",
        "client_factory",
    ):
        assert symbol not in generation_source
    assert "from uta.language.python.generation import" not in backend_source
    assert "from uta.language.python.generation import" not in phases_source
    assert Path("uta/language/python/test_artifacts.py").is_file()
    assert Path("uta/language/python/verification/generation.py").is_file()
