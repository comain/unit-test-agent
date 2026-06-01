import json
import pytest
import shutil
import zipfile
from pathlib import Path
import tempfile
from click.testing import CliRunner
import logging
from types import SimpleNamespace

from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.graph_builder import GraphBuilder
from uta.language.java.context_builder import ContextBuilder
from uta.cli import (
    _configure_run_logging,
    main,
    _pick_openai_oauth_method,
    _probe_openai_auth_ready,
    _probe_openai_auth_ready_with_retry,
    _task_quality_options,
)


class StubClient:
    def __init__(self, methods=None, auth=None, probe_events=None):
        self.methods = methods if methods is not None else [{"type": "oauth", "label": "OpenAI"}]
        self.auth = auth or {
            "url": "https://auth.example.test",
            "method": "auto",
            "instructions": "Open the URL",
        }
        self.probe_events = list(probe_events or [])
        self.authorize_calls = []
        self.create_calls = []
        self.send_calls = []
        self.delete_calls = []
        self.providers = {"connected": []}

    def list_provider_auth(self, repo_path=None):
        return {"openai": self.methods}


def test_task_quality_options_use_stored_ci_incremental_gates():
    quality_mode, coverage_gate, mutation_gate = _task_quality_options(
        {"coverage_gate": 95.0, "mutation_gate": 100.0},
        {"quality_mode": "ci_incremental"},
        80,
        70,
    )

    assert quality_mode == "ci_incremental"
    assert coverage_gate == 95
    assert mutation_gate == 100


def test_task_quality_options_default_to_batch_cli_gates():
    quality_mode, coverage_gate, mutation_gate = _task_quality_options(
        {"coverage_gate": None, "mutation_gate": None},
        {},
        80,
        70,
    )

    assert quality_mode == "class_batch"
    assert coverage_gate == 80
    assert mutation_gate == 70

    def authorize_provider_oauth(self, provider_id, method_index, repo_path=None, inputs=None):
        self.authorize_calls.append((provider_id, method_index, repo_path, inputs))
        return self.auth

    def create_session(self, model_id=None, provider_id=None):
        self.create_calls.append((model_id, provider_id))
        return f"session-{len(self.create_calls)}"

    def send_message(self, session_id, content, model_id=None):
        self.send_calls.append((session_id, content, model_id))
        return {}

    def poll_completion(self, session_id, timeout=0):
        if not self.probe_events:
            raise AssertionError("No probe events configured")
        return self.probe_events.pop(0)

    def delete_session(self, session_id):
        self.delete_calls.append(session_id)

    def list_providers(self, repo_path=None):
        return self.providers


def test_pick_openai_oauth_method_prefers_headless():
    methods = [
        {"type": "oauth", "label": "ChatGPT Pro/Plus (browser)"},
        {"type": "oauth", "label": "ChatGPT Pro/Plus (headless)"},
        {"type": "api", "label": "Manually enter API Key"},
    ]
    assert _pick_openai_oauth_method(methods) == 1


def test_probe_openai_auth_ready_success(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    calls = []
    monkeypatch.setattr(
        "uta.opencode.process.OpenCodeProcess.run_turn",
        lambda self, message, model_id=None, repo_path=None, timeout=None: calls.append((message, model_id, repo_path, timeout)) or SimpleNamespace(type="completed", result="OK", error=None),
    )

    assert _probe_openai_auth_ready(StubClient(), "/tmp/repo") is True
    assert calls == [("Reply with only: OK", "openai/gpt-5.4", "/tmp/repo", 120)]


def test_probe_openai_auth_ready_prefers_provider_from_model(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openrouter")
    calls = []
    monkeypatch.setattr(
        "uta.opencode.process.OpenCodeProcess.run_turn",
        lambda self, message, model_id=None, repo_path=None, timeout=None: calls.append((message, model_id, repo_path, timeout)) or SimpleNamespace(type="completed", result="OK", error=None),
    )

    assert _probe_openai_auth_ready(StubClient(), "/tmp/repo") is True
    assert calls == [("Reply with only: OK", "openai/gpt-5.4", "/tmp/repo", 120)]


def test_probe_openai_auth_ready_returns_false_for_provider_auth(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setattr(
        "uta.opencode.process.OpenCodeProcess.run_turn",
        lambda self, message, model_id=None, repo_path=None, timeout=None: SimpleNamespace(
            type="error",
            result="",
            error={"name": "ProviderAuthError", "data": {"message": "missing"}},
        ),
    )

    assert _probe_openai_auth_ready(StubClient(), "/tmp/repo") is False


def test_probe_openai_auth_ready_raises_for_rate_limit(monkeypatch):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setattr(
        "uta.opencode.process.OpenCodeProcess.run_turn",
        lambda self, message, model_id=None, repo_path=None, timeout=None: SimpleNamespace(
            type="rate_limited",
            result="",
            error={"retry_after_seconds": 120, "provider_id": "openai", "model_id": "gpt-5.4"},
        ),
    )

    with pytest.raises(RuntimeError, match="rate limited"):
        _probe_openai_auth_ready(StubClient(), "/tmp/repo")


def test_probe_openai_auth_ready_with_retry_exhausts_all_attempts_then_raises(monkeypatch):
    client = StubClient()
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    attempts_seen = []
    monkeypatch.setattr(
        "uta.cli._probe_openai_auth_ready",
        lambda client, repo: attempts_seen.append(repo) or (_ for _ in ()).throw(RuntimeError("OpenAI readiness probe timed out before the model replied.")),
    )

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        with pytest.raises(RuntimeError, match="timed out"):
            _probe_openai_auth_ready_with_retry(client, "/tmp/repo", attempts=3)
    assert attempts_seen == ["/tmp/repo", "/tmp/repo", "/tmp/repo"]


def test_probe_openai_auth_ready_with_retry_raises_when_disconnected(monkeypatch):
    client = StubClient()
    client.providers = {"connected": []}
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setattr(
        "uta.cli._probe_openai_auth_ready",
        lambda client, repo: (_ for _ in ()).throw(RuntimeError("OpenAI readiness probe timed out before the model replied. The ChatGPT auth may be fine, but the confirmation turn was too slow.")),
    )

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        with pytest.raises(RuntimeError, match="OpenAI readiness probe timed out"):
            _probe_openai_auth_ready_with_retry(client, "/tmp/repo", attempts=3)


def test_probe_openai_auth_ready_with_retry_raises_rate_limit_even_when_connected(monkeypatch):
    client = StubClient()
    client.providers = {"connected": ["openai"]}
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    attempts_seen = []
    monkeypatch.setattr(
        "uta.cli._probe_openai_auth_ready",
        lambda client, repo: attempts_seen.append(repo) or (_ for _ in ()).throw(RuntimeError("OpenAI readiness probe was rate limited by the provider/model. retry after 300s.")),
    )

    with pytest.MonkeyPatch.context() as mp:
        import time
        mp.setattr(time, "sleep", lambda x: None)
        with pytest.raises(RuntimeError, match="rate limited"):
            _probe_openai_auth_ready_with_retry(client, "/tmp/repo", attempts=3)
    assert attempts_seen == ["/tmp/repo", "/tmp/repo", "/tmp/repo"]


def test_configure_run_logging_creates_run_log(tmp_path):
    log_path = _configure_run_logging(str(tmp_path), verbose=False)
    path = Path(log_path)

    assert path.parent.name == "uta-run-logs"
    assert path.parent == Path(tempfile.gettempdir()) / "uta-run-logs"
    assert path.name.startswith(f"{tmp_path.name}_run_")
    assert path.suffix == ".log"
    assert path.exists()


def _copy_fixture_repo(fixtures_dir, repo_root: Path):
    src_root = repo_root / "src" / "main" / "java" / "com" / "example"
    service_dir = src_root / "service"
    mapper_dir = src_root / "mapper"
    service_dir.mkdir(parents=True, exist_ok=True)
    mapper_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(fixtures_dir) / "SampleService.java", service_dir / "SampleService.java")
    shutil.copy(Path(fixtures_dir) / "SampleMapper.java", mapper_dir / "SampleMapper.java")


def _write_java(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_query_index_json_output(fixtures_dir, tmp_path):
    _copy_fixture_repo(fixtures_dir, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(tmp_path),
            "--class-fqn",
            "com.example.service.SampleService",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert payload["class"]["fqn"] == "com.example.service.SampleService"
    assert any(field["name"] == "sampleMapper" for field in payload["fields"])
    assert any(method["name"] == "process" for method in payload["methods"])
    assert any(dep["fqn"] == "com.example.mapper.SampleMapper" for dep in payload["dependencies"])
    assert payload["symbols"]["SampleMapper"] == "com.example.mapper.SampleMapper"


def test_query_index_filters_method_and_symbol(fixtures_dir, tmp_path):
    _copy_fixture_repo(fixtures_dir, tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(tmp_path),
            "--class-fqn",
            "com.example.service.SampleService",
            "--section",
            "methods",
            "--section",
            "symbols",
            "--method",
            "process",
            "--symbol",
            "SampleMapper",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [method["name"] for method in payload["methods"]] == ["process"]
    assert payload["symbols"] == {"SampleMapper": "com.example.mapper.SampleMapper"}


def test_query_index_excludes_private_methods_and_returns_plan_summary(tmp_path):
    repo = tmp_path / "repo"
    _write_java(
        repo / "src" / "main" / "java" / "com" / "example" / "adapter" / "RemoteGateway.java",
        """package com.example.adapter;

public interface RemoteGateway {
    String fetchDefault();
}
""",
    )
    _write_java(
        repo / "src" / "main" / "java" / "com" / "example" / "service" / "PlannerService.java",
        """package com.example.service;

import com.example.adapter.RemoteGateway;

import org.springframework.stereotype.Service;

@Service
public class PlannerService {
    private RemoteGateway remoteGateway;

    public String process(String input) {
        if (input == null) {
            return remoteGateway.fetchDefault();
        }
        return input;
    }

    private String helper() {
        return "x";
    }
}
""",
    )
    runner = CliRunner()

    methods_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--class-fqn",
            "com.example.service.PlannerService",
            "--section",
            "methods",
            "--json-output",
        ],
    )
    assert methods_result.exit_code == 0, methods_result.output
    methods_payload = json.loads(methods_result.output)
    assert [method["name"] for method in methods_payload["methods"]] == ["process"]

    plan_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--class-fqn",
            "com.example.service.PlannerService",
            "--section",
            "plan_summary",
            "--json-output",
        ],
    )
    assert plan_result.exit_code == 0, plan_result.output
    plan_payload = json.loads(plan_result.output)
    assert plan_payload["found"] is True
    assert [method["name"] for method in plan_payload["plan_summary"]["public_entry_methods"]] == ["process"]
    assert "null vs non-null inputs" in plan_payload["plan_summary"]["branch_axes"]

    generation_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--class-fqn",
            "com.example.service.PlannerService",
            "--section",
            "generation_summary",
            "--json-output",
        ],
    )
    assert generation_result.exit_code == 0, generation_result.output
    generation_payload = json.loads(generation_result.output)
    assert [method["name"] for method in generation_payload["generation_summary"]["high_yield_methods"]] == ["process"]
    assert "process" not in generation_payload["generation_summary"]["construction_hints"]["manual_types"]

    generation_lookup_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--class-fqn",
            "com.example.service.PlannerService",
            "--section",
            "generation_lookup",
            "--symbol",
            "RemoteGateway",
            "--json-output",
        ],
    )
    assert generation_lookup_result.exit_code == 0, generation_lookup_result.output
    generation_lookup_payload = json.loads(generation_lookup_result.output)
    assert generation_lookup_payload["found"] is True
    assert generation_lookup_payload["generation_lookup"]["symbol"] == "RemoteGateway"
    assert generation_lookup_payload["generation_lookup"]["hits"][0]["symbol"] == "RemoteGateway"
    assert generation_lookup_payload["generation_lookup"]["hits"][0]["source_path"].endswith("RemoteGateway.java")

    fix_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--class-fqn",
            "com.example.service.PlannerService",
            "--section",
            "fix_summary",
            "--method",
            "helper",
            "--json-output",
        ],
    )
    assert fix_result.exit_code == 0, fix_result.output
    fix_payload = json.loads(fix_result.output)
    assert fix_payload["found"] is True
    assert fix_payload["fix_summary"]["matching_methods"][0]["name"] == "helper"
    assert fix_payload["fix_summary"]["matching_methods"][0]["visibility"] == "private"


def test_export_generation_pack_contains_selected_method_window(tmp_path):
    repo = tmp_path / "repo"
    _write_java(
        repo / "src" / "main" / "java" / "com" / "example" / "service" / "PlannerService.java",
        """package com.example.service;

public class PlannerService {
    private RemoteGateway remoteGateway;

    public String process(String input) {
        if (input == null) {
            return remoteGateway.fetchDefault();
        }
        return input.trim();
    }

    public String skip(String input) {
        return input;
    }
}
""",
    )

    parser = JavaParser()
    results = [parser.parse_file(str(repo / "src" / "main" / "java" / "com" / "example" / "service" / "PlannerService.java"))]
    graph = GraphBuilder().build(results)
    ctx = ContextBuilder(repo_path=str(repo), graph=graph, flows=[])
    pack_path = Path(
        ctx.export_generation_pack(
            "com.example.service.PlannerService",
            method_names=["process"],
            plan_path=str(repo / ".uta_cache" / "context" / "latest_generation_plan.md"),
        )
    )

    assert pack_path.exists()
    pack = pack_path.read_text(encoding="utf-8")
    assert "## Selected First-Pass Methods" in pack
    assert "`process`" in pack
    assert "### `String process(String input)`" in pack
    assert "null guards" in pack
    assert "`remoteGateway`" in pack
    assert "fetchDefault" in pack


def test_query_index_falls_back_to_discovered_module(fixtures_dir, tmp_path):
    module_root = tmp_path / "alpha"
    _copy_fixture_repo(fixtures_dir, module_root)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(tmp_path),
            "--module",
            "beta",
            "--class-fqn",
            "com.example.service.SampleService",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert payload["class"]["module"] == "alpha"
    assert payload["class"]["source_path"].endswith("alpha/src/main/java/com/example/service/SampleService.java")


def test_query_index_refreshes_when_source_changes(fixtures_dir, tmp_path):
    _copy_fixture_repo(fixtures_dir, tmp_path)
    runner = CliRunner()
    service_path = tmp_path / "src" / "main" / "java" / "com" / "example" / "service" / "SampleService.java"

    first = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(tmp_path),
            "--class-fqn",
            "com.example.service.SampleService",
            "--section",
            "methods",
            "--json-output",
        ],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert [method["name"] for method in first_payload["methods"]] == ["process"]

    updated = service_path.read_text()
    updated = updated.replace(
        "\n}\n",
        "\n\n    public boolean isReady() {\n        return true;\n    }\n}\n",
    )
    service_path.write_text(updated)

    second = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(tmp_path),
            "--class-fqn",
            "com.example.service.SampleService",
            "--section",
            "methods",
            "--json-output",
        ],
    )
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert [method["name"] for method in second_payload["methods"]] == ["isReady", "process"]


def test_query_index_finds_sibling_api_repo_and_util_module(tmp_path):
    main_repo = tmp_path / "main-repo"
    _write_java(
        main_repo / "biz" / "src" / "main" / "java" / "com" / "example" / "service" / "MainService.java",
        """package com.example.service;

import com.example.api.RemoteDto;

public class MainService {
    public RemoteDto fetch() {
        return new RemoteDto();
    }
}
""",
    )
    sibling_api = tmp_path / "shared-api"
    _write_java(
        sibling_api / "src" / "main" / "java" / "com" / "example" / "api" / "RemoteDto.java",
        """package com.example.api;

public class RemoteDto {
    public String code() {
        return "ok";
    }
}
""",
    )
    _write_java(
        sibling_api / "util" / "src" / "main" / "java" / "com" / "example" / "util" / "ApiHelper.java",
        """package com.example.util;

public class ApiHelper {
    public boolean ready() {
        return true;
    }
}
""",
    )

    runner = CliRunner()

    sibling_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(main_repo),
            "--class-fqn",
            "com.example.api.RemoteDto",
            "--section",
            "methods",
            "--json-output",
        ],
    )
    assert sibling_result.exit_code == 0, sibling_result.output
    sibling_payload = json.loads(sibling_result.output)
    assert sibling_payload["found"] is True
    assert [method["name"] for method in sibling_payload["methods"]] == ["code"]

    util_result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(main_repo),
            "--class-fqn",
            "com.example.util.ApiHelper",
            "--section",
            "methods",
            "--json-output",
        ],
    )
    assert util_result.exit_code == 0, util_result.output
    util_payload = json.loads(util_result.output)
    assert util_payload["found"] is True
    assert [method["name"] for method in util_payload["methods"]] == ["ready"]


def test_query_index_finds_class_under_configured_source_dirs(tmp_path, monkeypatch):
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir(parents=True, exist_ok=True)
    configured_base = tmp_path / "services" / "api"
    _write_java(
        configured_base / "outbound-api" / "model" / "src" / "main" / "java" / "com" / "example" / "api" / "ConfiguredDto.java",
        """package com.example.api;

public class ConfiguredDto {
    public String value() {
        return "configured";
    }
}
""",
    )
    runner = CliRunner()
    monkeypatch.setattr("uta.cli.settings.index_source_dirs", str(configured_base))
    monkeypatch.setattr("uta.cli.settings.index_fetch_sources", False)

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(main_repo),
            "--class-fqn",
            "com.example.api.ConfiguredDto",
            "--section",
            "methods",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert [method["name"] for method in payload["methods"]] == ["value"]


def test_query_index_falls_back_to_local_source_jar(tmp_path, monkeypatch):
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir(parents=True, exist_ok=True)
    local_repo = tmp_path / "m2repo"
    jar_path = local_repo / "com" / "example" / "demo" / "1.0" / "demo-1.0-sources.jar"
    jar_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr(
            "com/example/ext/ExternalDto.java",
            """package com.example.ext;

public class ExternalDto {
    public String name() {
        return "external";
    }
}
""",
        )
    settings_xml = tmp_path / "settings.xml"
    settings_xml.write_text(
        f"""<?xml version="1.0"?>
<settings>
  <localRepository>{local_repo}</localRepository>
</settings>
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    monkeypatch.setattr("uta.cli.settings.index_source_dirs", "")
    monkeypatch.setattr("uta.cli.settings.index_fetch_sources", False)
    monkeypatch.setattr("uta.cli.settings.maven_settings_path", str(settings_xml))

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(main_repo),
            "--class-fqn",
            "com.example.ext.ExternalDto",
            "--section",
            "methods",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert payload["class"]["source_kind"] == "source_jar"
    assert [method["name"] for method in payload["methods"]] == ["name"]


def test_query_index_fetches_sources_when_local_jar_missing(tmp_path, monkeypatch):
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir(parents=True, exist_ok=True)
    (main_repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    local_repo = tmp_path / "m2repo"
    settings_xml = tmp_path / "settings.xml"
    settings_xml.write_text(
        f"""<?xml version="1.0"?>
<settings>
  <localRepository>{local_repo}</localRepository>
</settings>
""",
        encoding="utf-8",
    )
    jar_path = local_repo / "com" / "example" / "demo" / "1.0" / "demo-1.0-sources.jar"

    def fake_run(cmd, cwd=None, check=None, stdout=None, stderr=None, text=None, timeout=None):
        jar_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar_path, "w") as archive:
            archive.writestr(
                "com/example/ext/FetchedDto.java",
                """package com.example.ext;

public class FetchedDto {
    public boolean ready() {
        return true;
    }
}
""",
            )
        return None

    runner = CliRunner()
    monkeypatch.setattr("uta.cli.settings.index_source_dirs", "")
    monkeypatch.setattr("uta.cli.settings.index_fetch_sources", True)
    monkeypatch.setattr("uta.cli.settings.maven_settings_path", str(settings_xml))
    monkeypatch.setattr("uta.cli.subprocess.run", fake_run)

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(main_repo),
            "--class-fqn",
            "com.example.ext.FetchedDto",
            "--section",
            "methods",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert payload["class"]["source_kind"] == "source_jar"
    assert [method["name"] for method in payload["methods"]] == ["ready"]


def test_run_logs_pipeline_exception(monkeypatch, tmp_path, caplog):
    class DummyClient:
        def __init__(self, repo_path=None, port=None):
            self.repo_path = repo_path
            self.port = port

        def create_session(self, model_id=None, provider_id=None):
            return "session-1"

    class DummyWorkflow:
        def invoke(self, initial_state):
            raise RuntimeError("boom")

    class DummyReporter:
        def __init__(self, repo):
            self.repo = repo

        def display_summary(self, results, metadata=None):
            return None

    monkeypatch.setattr("uta.graph.workflow.build_workflow", lambda: DummyWorkflow())
    monkeypatch.setattr("uta.opencode.config.generate_opencode_config", lambda repo: tmp_path / "opencode.json")
    monkeypatch.setattr("uta.opencode.client.OpenCodeClient", DummyClient)
    monkeypatch.setattr("uta.output.reporter.Reporter", DummyReporter)
    monkeypatch.setattr("uta.cli._ensure_model_auth", lambda repo: None)
    monkeypatch.setattr("uta.cli._configure_run_logging", lambda repo, verbose: str(tmp_path / "run.log"))

    caplog.set_level(logging.ERROR, logger="uta")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "run",
            "--repo",
            str(tmp_path),
            "--class-fqn",
            "com.example.service.SampleService",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Pipeline failed: boom" in result.output
    record = next((r for r in caplog.records if r.message == "Pipeline failed during run execution"), None)
    assert record is not None
    assert record.exc_info is not None
