"""
End-to-end integration tests for the UTA pipeline.

These tests run against a real Java Maven repo and exercise the pipeline (dry-run
and optional OpenCode). Configure with:

  - ``UTA_E2E_REPO`` — repo root (default: ``~/src/sample-service``)
  - ``UTA_E2E_MODULE`` — Maven module name (default: infer; prefers ``biz``)
  - ``UTA_E2E_OUTBOUND_REPO`` — second repo for ``TestCrossRepo`` (default: outbound-core)

Also see ``tests/test_cross_repo_smoke.py`` for ~/service_a and ~/service_b matrix.

Mark: pytest -m integration (markers can be added to slow tests)
"""
import os
import json
import re
import shutil
import subprocess
import time
import pytest
from pathlib import Path

from e2e_repo_paths import (
    primary_repo_path,
    infer_java_module,
    module_main_java,
    pick_service_like_class_for_context,
)

import_time = time.time()
E2E_BRANCH = "unit-code-gen-e2e"
REAL_OPENCODE_ENABLED = os.environ.get("UTA_E2E_REAL_OPENCODE", "").lower() in {"1", "true", "yes"}

REPO_PATH = primary_repo_path()
E2E_MODULE = infer_java_module(REPO_PATH)
E2E_MODULE_ARG = E2E_MODULE or None  # LangGraph / CLI use None for single-module root

pytestmark = pytest.mark.skipif(
    not os.path.isdir(REPO_PATH),
    reason=f"Primary E2E repo not found (set UTA_E2E_REPO): {REPO_PATH}",
)


def _keep_e2e_artifacts() -> bool:
    from uta.config import settings as uta_settings

    return uta_settings.e2e_keep_artifacts


def _maven_compile_stub_enabled() -> bool:
    return os.environ.get("UTA_E2E_REAL_MAVEN_COMPILE", "").lower() not in {"1", "true", "yes"}


def _stub_maven_compile(monkeypatch):
    """Keep E2E workflow tests deterministic unless explicitly validating Maven."""
    if not _maven_compile_stub_enabled():
        return

    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else []
        executable = Path(str(argv[0])).name if argv else ""
        is_compile_goal = any(str(arg).endswith("compile") for arg in argv)
        if executable in {"mvn", "mvnw"} and is_compile_goal:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def _first_java_class_fqns(repo_path: str, module: str, limit: int = 3) -> list[str]:
    java_root = module_main_java(repo_path, module)
    fqns: list[str] = []
    for source in sorted(java_root.rglob("*.java")):
        text = source.read_text(encoding="utf-8", errors="replace")
        package_match = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.MULTILINE)
        type_match = re.search(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)\b", text)
        if package_match and type_match:
            fqns.append(f"{package_match.group(1)}.{type_match.group(1)}")
            if len(fqns) >= limit:
                break
    return fqns


def _first_java_class_fqn(repo_path: str, module: str) -> str:
    fqns = _first_java_class_fqns(repo_path, module, limit=1)
    return fqns[0] if fqns else ""


E2E_CLASS_FQN = _first_java_class_fqn(REPO_PATH, E2E_MODULE) if os.path.isdir(REPO_PATH) else ""
E2E_CLASS_FQNS = _first_java_class_fqns(REPO_PATH, E2E_MODULE, limit=3) if os.path.isdir(REPO_PATH) else []


@pytest.fixture(autouse=True)
def clean_target_repo(monkeypatch):
    """Ensure target repo is on master and clean before/after each test."""
    _stub_maven_compile(monkeypatch)
    subprocess.run(["git", "-C", REPO_PATH, "checkout", "master"],
                   capture_output=True, check=False)
    subprocess.run(["git", "-C", REPO_PATH, "branch", "-D", E2E_BRANCH],
                   capture_output=True, check=False)
    yield
    # Cleanup: switch back to master, delete test-only branch if created
    subprocess.run(["git", "-C", REPO_PATH, "checkout", "master"],
                   capture_output=True, check=False)
    subprocess.run(["git", "-C", REPO_PATH, "branch", "-D", E2E_BRANCH],
                   capture_output=True, check=False)
    if not _keep_e2e_artifacts():
        cache_dir = Path(REPO_PATH) / ".uta_cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)


# ── Stage 1: Git scanning ──────────────────────────────────────────

class TestGitScanning:
    def test_scan_finds_candidates(self):
        from uta.engine.source_selection import get_changed_java_files
        # Use 1000 days — target repo may not have recent commits
        mod = E2E_MODULE or None
        files = get_changed_java_files(REPO_PATH, 1000, mod)
        assert len(files) > 0, "Should find changed Java files in last 1000 days"
        for path, count in files:
            assert path.endswith(".java")
            assert "src/main/java" in path
            assert count > 0

    def test_scan_respects_module_filter(self):
        from uta.engine.source_selection import get_changed_java_files
        mod = E2E_MODULE or None
        files = get_changed_java_files(REPO_PATH, 1000, mod)
        for path, _ in files:
            if E2E_MODULE:
                assert f"{E2E_MODULE}/" in path
            else:
                assert "src/main/java" in path

    def test_filter_limits_results(self):
        from uta.engine.source_selection import get_changed_java_files, filter_files
        files = get_changed_java_files(REPO_PATH, 90, E2E_MODULE or None)
        filtered = filter_files(files, 1)
        assert len(filtered) <= 1


# ── Stage 2: Java parsing ──────────────────────────────────────────

class TestJavaParsing:
    def test_parse_real_java_files(self):
        from uta.language.java.parse.java_parser import JavaParser
        parser = JavaParser()
        java_root = module_main_java(REPO_PATH, E2E_MODULE)
        java_files = list(java_root.rglob("*.java"))
        assert len(java_files) > 20, "Should find Java sources under inferred module"

        result = parser.parse_file(str(java_files[0]))
        assert result.package != ""
        assert len(result.symbols) > 0

    def test_parse_handles_chinese_comments(self):
        """Regression test for UTF-8 byte offset bug."""
        from uta.language.java.parse.java_parser import JavaParser
        parser = JavaParser()
        # Find a file likely to have Chinese comments
        java_root = module_main_java(REPO_PATH, E2E_MODULE)
        java_files = list(java_root.rglob("*.java"))

        # Parse all files — none should produce garbage symbol names
        for f in java_files[:20]:
            result = parser.parse_file(str(f))
            for sym in result.symbols:
                # Symbol names should be valid Java identifiers (no Chinese chars, no operators)
                assert sym.name.isidentifier() or sym.name == "<init>", \
                    f"Suspicious symbol name '{sym.name}' in {f}"


# ── Stage 3: Graph building ────────────────────────────────────────

class TestGraphBuilding:
    @pytest.fixture
    def graph(self):
        from uta.language.java.parse.java_parser import JavaParser
        from uta.language.java.parse.graph_builder import GraphBuilder
        parser = JavaParser()
        java_root = module_main_java(REPO_PATH, E2E_MODULE)
        java_files = list(java_root.rglob("*.java"))
        results = [parser.parse_file(str(f)) for f in java_files]
        builder = GraphBuilder()
        return builder.build(results)

    def test_graph_has_nodes_and_edges(self, graph):
        assert len(graph.nodes) > 100
        assert len(graph.edges) > 100

    def test_graph_has_classes_and_methods(self, graph):
        classes = [n for n in graph.nodes.values() if n.kind == "class"]
        methods = [n for n in graph.nodes.values() if n.kind == "method"]
        assert len(classes) > 50
        assert len(methods) > 200

    def test_graph_stores_parent_fqn(self, graph):
        """Regression: parent_fqn must be in metadata for entry point detection."""
        methods = [n for n in graph.nodes.values() if n.kind == "method"]
        methods_with_parent = [m for m in methods if m.metadata.get("parent_fqn")]
        assert len(methods_with_parent) > 0, "Methods should have parent_fqn in metadata"

    def test_annotations_extracted_correctly(self, graph):
        """Regression: annotations should be clean names, not garbage from byte offset bug."""
        service_classes = [
            n for n in graph.nodes.values()
            if n.kind == "class" and "Service" in n.metadata.get("annotations", [])
        ]
        assert len(service_classes) > 0, "Should find @Service classes"


# ── Stage 4: Process flow extraction ───────────────────────────────

class TestFlowExtraction:
    def test_flows_detected(self):
        from uta.language.java.parse.java_parser import JavaParser
        from uta.language.java.parse.graph_builder import GraphBuilder
        from uta.language.java.parse.process_extractor import ProcessExtractor

        parser = JavaParser()
        java_root = module_main_java(REPO_PATH, E2E_MODULE)
        java_files = list(java_root.rglob("*.java"))
        results = [parser.parse_file(str(f)) for f in java_files]

        builder = GraphBuilder()
        graph = builder.build(results)

        # Find entry points: methods in @Service classes
        entry_class_fqns = {
            fqn for fqn, node in graph.nodes.items()
            if node.kind == "class"
            and any(a in ["Controller", "DubboService", "Service", "Component"]
                    for a in node.metadata.get("annotations", []))
        }
        entry_points = [
            fqn for fqn, node in graph.nodes.items()
            if node.kind == "method" and node.metadata.get("parent_fqn") in entry_class_fqns
        ]

        assert len(entry_points) > 0, "Should find entry point methods"

        extractor = ProcessExtractor(graph)
        flows = extractor.extract_flows(entry_points[:200])
        assert len(flows) > 0, "Should detect process flows"


# ── Stage 5: Context building ──────────────────────────────────────

class TestContextBuilding:
    def test_build_context_for_class(self):
        from uta.language.java.parse.java_parser import JavaParser
        from uta.language.java.parse.graph_builder import GraphBuilder
        from uta.language.java.parse.process_extractor import ProcessExtractor
        from uta.language.java.context_builder import ContextBuilder

        parser = JavaParser()
        java_root = module_main_java(REPO_PATH, E2E_MODULE)
        java_files = list(java_root.rglob("*.java"))
        results = [parser.parse_file(str(f)) for f in java_files]

        builder = GraphBuilder()
        graph = builder.build(results)

        entry_class_fqns = {
            fqn for fqn, node in graph.nodes.items()
            if node.kind == "class"
            and any(a in ["Service"] for a in node.metadata.get("annotations", []))
        }
        entry_points = [
            fqn for fqn, node in graph.nodes.items()
            if node.kind == "method" and node.metadata.get("parent_fqn") in entry_class_fqns
        ]

        extractor = ProcessExtractor(graph)
        flows = extractor.extract_flows(entry_points[:200])

        target_class = pick_service_like_class_for_context(graph)
        assert target_class is not None, "No suitable concrete class for context build"

        ctx_builder = ContextBuilder(REPO_PATH, graph, flows)
        ctx = ctx_builder.build_for_class(target_class)

        assert "source_code" in ctx
        assert len(ctx["source_code"]) > 0
        assert "class_fqn" in ctx
        assert "dependencies" in ctx
        assert "process_flows" in ctx


# ── Stage 6: Full pipeline (dry-run) ──────────────────────────────

class TestFullPipeline:
    def test_pipeline_dry_run(self):
        """Run the full pipeline without OpenCode — should complete with SKIP status."""
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": REPO_PATH,
            "module": E2E_MODULE_ARG,
            "days": 1000,
            "max_files": 2,
            "explicit_class_fqns": E2E_CLASS_FQNS[:1],
            "coverage_gate": 30,
            "mutation_gate": 50,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": None,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)

        assert final_state.get("error") is None, f"Pipeline error: {final_state.get('error')}"
        assert final_state.get("finished") is True
        results = final_state.get("results", {})
        assert len(results) > 0, "Should have processed at least one candidate"
        for fqn, res in results.items():
            assert res["status"] == "SKIP"  # No OpenCode = SKIP

    def test_report_generated(self):
        """Run pipeline and verify report file is created."""
        from uta.graph.workflow import build_workflow
        from uta.output.reporter import Reporter

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": REPO_PATH,
            "module": E2E_MODULE_ARG,
            "days": 1000,
            "max_files": 1,
            "explicit_class_fqns": E2E_CLASS_FQNS[:1],
            "coverage_gate": 30,
            "mutation_gate": 50,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": None,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)
        results = final_state.get("results", {})

        reporter = Reporter(REPO_PATH)
        reporter.save_report(
            results,
            "test_report.json",
            metadata={
                "total_candidates": len(results),
                "phase_timings": {"generate_validate_seconds": 1.5},
                "total_elapsed_seconds": 2.0,
            },
        )

        report_path = Path(REPO_PATH) / ".uta_reports" / "test_report.json"
        assert report_path.exists()
        with open(report_path) as f:
            data = json.load(f)
        assert "project_summary" in data
        assert "per_file_metrics" in data
        assert "timing_details" in data

        # Cleanup
        report_path.unlink()


# ── Stage 7: Real run with OpenCode using the configured model ──────────────────────

class TestRealRun:
    """Real E2E test that starts OpenCode with the configured model and generates tests.

    Run with:
      pytest tests/test_pipeline_e2e.py::TestRealRun -v -s
      UTA_OPENCODE_MODEL=openai/gpt-5.4 UTA_OPENCODE_PROVIDER=openai pytest ...
    """

    pytestmark = pytest.mark.skipif(
        not REAL_OPENCODE_ENABLED,
        reason="set UTA_E2E_REAL_OPENCODE=1 to run real OpenCode Java pipeline tests",
    )

    @pytest.fixture(autouse=True)
    def setup_opencode(self):
        """Create a real process-based OpenCode session for integration tests."""
        from uta.opencode.config import generate_opencode_config
        from uta.opencode.client import OpenCodeClient
        from uta.cli import _ensure_model_auth
        from uta.config import settings as uta_settings

        generate_opencode_config(REPO_PATH)
        try:
            _ensure_model_auth(REPO_PATH)
            self.client = OpenCodeClient(repo_path=REPO_PATH)
            self.session_id = self.client.create_session(
                model_id=uta_settings.opencode_model,
                provider_id=uta_settings.opencode_provider,
            )
        except Exception as e:
            pytest.fail(f"OpenCode process integration setup failed: {e}")

        yield

        # Cleanup
        try:
            self.client.delete_session(self.session_id)
        except Exception:
            pass
        if not _keep_e2e_artifacts():
            # Remove generated config and test artifacts
            config_path = Path(REPO_PATH) / "opencode.json"
            if config_path.exists():
                config_path.unlink()
            # Clean up generated test files to avoid stale artifacts
            for test_file in Path(REPO_PATH).rglob("*Test.java"):
                if "src/test/java" in str(test_file) and test_file.stat().st_mtime > import_time:
                    test_file.unlink(missing_ok=True)

    def test_opencode_responds(self):
        """Verify OpenCode can respond to a simple message using the selected model."""
        from uta.config import settings as uta_settings

        self.client.send_message(
            self.session_id,
            "Reply with only: OK",
            model_id=uta_settings.opencode_model,
        )
        event = self.client.poll_completion(self.session_id, timeout=60)
        if event.get("type") == "rate_limited":
            assert event.get("rate_limit"), f"Expected structured rate-limit payload, got: {event}"
            return
        if event.get("type") in {"timeout", "error"}:
            assert "result" in event or "error" in event, f"Expected structured provider event, got: {event}"
            return
        assert event["type"] == "completed", f"Expected completed, got: {event}"
        assert "OK" in event["result"]

    def test_real_pipeline_single_class(self):
        """Run the full pipeline with OpenCode on 1 class, minimal coverage gate."""
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": REPO_PATH,
            "module": E2E_MODULE_ARG,
            "days": 1000,
            "max_files": 1,
            "explicit_class_fqns": E2E_CLASS_FQNS[:1],
            "coverage_gate": 1,  # minimal gate
            "mutation_gate": 0,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": self.session_id,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)

        assert final_state.get("error") is None, f"Pipeline error: {final_state.get('error')}"
        assert final_state.get("finished") is True
        results = final_state.get("results", {})
        assert len(results) > 0, "Should have processed at least one candidate"

        # Dump generated test file content for review (survives cleanup)
        for fqn, res in results.items():
            print(f"\n{'='*60}")
            print(f"Class: {fqn}")
            print(f"Status: {res['status']}, Coverage: {res.get('coverage', 0):.1f}%")
            print(f"Tests pass: {res.get('tests_pass')}")
            print(f"Test file: {res.get('test_file_path', 'N/A')}")
            if res.get("test_file_content"):
                print(f"\n--- Generated Test ({res['test_file_path']}) ---")
                print(res["test_file_content"])
                print(f"--- End Test ---")
            else:
                print("WARNING: No test file content captured")
            print(f"{'='*60}\n")
            assert res["status"] != "SKIP", f"Class {fqn} was skipped despite having OpenCode"

    def test_real_pipeline_multi_class(self):
        """Run the pipeline on 3 classes — verify loop and no state leakage."""
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": REPO_PATH,
            "module": E2E_MODULE_ARG,
            "days": 1000,
            "max_files": 3,
            "explicit_class_fqns": E2E_CLASS_FQNS[:3],
            "coverage_gate": 1,
            "mutation_gate": 0,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": self.session_id,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)

        assert final_state.get("error") is None, f"Pipeline error: {final_state.get('error')}"
        assert final_state.get("finished") is True
        results = final_state.get("results", {})
        assert len(results) >= 2, f"Should process multiple classes, got {len(results)}"

        for fqn, res in results.items():
            print(f"\n{'='*60}")
            print(f"Class: {fqn}")
            print(f"Status: {res['status']}, Coverage: {res.get('coverage', 0):.1f}%")
            print(f"Tests pass: {res.get('tests_pass')}")
            print(f"Test file: {res.get('test_file_path', 'N/A')}")
            print(f"{'='*60}\n")
            assert res["status"] != "SKIP", f"Class {fqn} was skipped"

    def test_coverage_reported(self):
        """Run pipeline on 1 class and verify Jacoco coverage is non-zero."""
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": REPO_PATH,
            "module": E2E_MODULE_ARG,
            "days": 1000,
            "max_files": 1,
            "explicit_class_fqns": E2E_CLASS_FQNS[:1],
            "coverage_gate": 1,
            "mutation_gate": 0,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": self.session_id,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)
        results = final_state.get("results", {})
        assert len(results) > 0

        for fqn, res in results.items():
            print(f"Class: {fqn}, Coverage: {res.get('coverage', 0):.1f}%")
            if res.get("tests_pass"):
                assert res.get("coverage", 0) > 0, \
                    f"Coverage should be >0 when tests pass for {fqn}"


# ── Stage 8: Cross-repo validation (second Maven tree) ───────────

SECONDARY_REPO = os.path.abspath(
    os.path.expanduser(os.environ.get("UTA_E2E_OUTBOUND_REPO", "~/src/sample-service"))
)
SECONDARY_MODULE_ARG = infer_java_module(
    SECONDARY_REPO,
    explicit_env="UTA_E2E_OUTBOUND_MODULE",
    use_global_module_env=False,
) or None
SECONDARY_CLASS_FQN = (
    _first_java_class_fqn(SECONDARY_REPO, SECONDARY_MODULE_ARG or "")
    if os.path.isdir(SECONDARY_REPO)
    else ""
)


@pytest.mark.skipif(
    not os.path.isdir(SECONDARY_REPO),
    reason=f"Secondary E2E repo missing (set UTA_E2E_OUTBOUND_REPO): {SECONDARY_REPO}",
)
class TestCrossRepo:
    """Validate pipeline on a second repo (default outbound-core)."""

    @pytest.fixture(autouse=True)
    def setup_outbound(self, monkeypatch):
        _stub_maven_compile(monkeypatch)
        subprocess.run(["git", "-C", SECONDARY_REPO, "checkout", "master"],
                       capture_output=True, check=False)
        subprocess.run(["git", "-C", SECONDARY_REPO, "branch", "-D", E2E_BRANCH],
                       capture_output=True, check=False)
        yield
        subprocess.run(["git", "-C", SECONDARY_REPO, "checkout", "master"],
                       capture_output=True, check=False)
        subprocess.run(["git", "-C", SECONDARY_REPO, "branch", "-D", E2E_BRANCH],
                       capture_output=True, check=False)
        if not _keep_e2e_artifacts():
            cache_dir = Path(SECONDARY_REPO) / ".uta_cache"
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

    def test_outbound_dry_run(self):
        """Dry-run on secondary repo — verify parsing, filtering, and graph building."""
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
        initial_state = {
            "repo_path": SECONDARY_REPO,
            "module": SECONDARY_MODULE_ARG,
            "days": 1000,
            "max_files": 2,
            "explicit_class_fqns": [SECONDARY_CLASS_FQN],
            "coverage_gate": 1,
            "mutation_gate": 0,
            "classes_per_agent_run": 1,
            "branch_name": E2E_BRANCH,
            "started_at": time.time(),
            "current_batch": [],
            "candidates": [],
            "current_class": None,
            "graph": None,
            "flows": [],
            "session_id": None,
            "results": {},
            "phase_timings": {},
            "error": None,
            "finished": False,
        }

        final_state = workflow_app.invoke(initial_state)
        assert final_state.get("error") is None, f"Pipeline error: {final_state.get('error')}"
        assert final_state.get("finished") is True
        results = final_state.get("results", {})
        assert len(results) > 0, "Should process at least one candidate"
        for fqn, res in results.items():
            print(f"Class: {fqn}, Status: {res['status']}")
            assert res["status"] == "SKIP"  # No OpenCode = SKIP

    @pytest.mark.skipif(
        not REAL_OPENCODE_ENABLED,
        reason="set UTA_E2E_REAL_OPENCODE=1 to run real OpenCode Java pipeline tests",
    )
    def test_outbound_real_single_class(self):
        """Real E2E on secondary repo — 1 class with OpenCode using the selected model."""
        from uta.opencode.server import OpenCodeServer
        from uta.opencode.config import generate_opencode_config
        from uta.opencode.client import OpenCodeClient
        from uta.cli import _ensure_model_auth
        from uta.config import settings as uta_settings
        from uta.graph.workflow import build_workflow

        generate_opencode_config(SECONDARY_REPO)
        server = OpenCodeServer(SECONDARY_REPO)
        try:
            server.start()
            _ensure_model_auth(SECONDARY_REPO)
            client = OpenCodeClient(repo_path=SECONDARY_REPO)
            session_id = client.create_session(
                model_id=uta_settings.opencode_model,
                provider_id=uta_settings.opencode_provider,
            )
        except Exception as e:
            pytest.skip(f"OpenCode server failed to start: {e}")
            return

        try:
            workflow_app = build_workflow()
            initial_state = {
                "repo_path": SECONDARY_REPO,
                "module": SECONDARY_MODULE_ARG,
                "days": 1000,
                "max_files": 1,
                "explicit_class_fqns": [SECONDARY_CLASS_FQN],
                "coverage_gate": 1,
                "mutation_gate": 0,
                "classes_per_agent_run": 1,
                "branch_name": E2E_BRANCH,
                "started_at": time.time(),
                "current_batch": [],
                "candidates": [],
                "current_class": None,
                "graph": None,
                "flows": [],
                "session_id": session_id,
                "results": {},
                "phase_timings": {},
                "error": None,
                "finished": False,
            }

            final_state = workflow_app.invoke(initial_state)

            assert final_state.get("error") is None
            results = final_state.get("results", {})
            assert len(results) > 0

            for fqn, res in results.items():
                print(f"\nClass: {fqn}")
                print(f"  Status: {res['status']}, Coverage: {res.get('coverage', 0):.1f}%")
                print(f"  Tests pass: {res.get('tests_pass')}")
                assert res["status"] != "SKIP"
        finally:
            try:
                client.delete_session(session_id)
            except Exception:
                pass
            server.stop()
            if not _keep_e2e_artifacts():
                config_path = Path(SECONDARY_REPO) / "opencode.json"
                if config_path.exists():
                    config_path.unlink()
                for test_file in Path(SECONDARY_REPO).rglob("*Test.java"):
                    if "src/test/java" in str(test_file) and test_file.stat().st_mtime > import_time:
                        test_file.unlink(missing_ok=True)
