"""Matrix dry-run for optional extra Java repos.

Run when repos exist, or set ``UTA_E2E_SERVICE_A_REPO`` / ``UTA_E2E_SERVICE_B_REPO``.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from e2e_repo_paths import cross_repo_matrix, infer_java_module, module_main_java, find_largest_main_java


def _maven_compile_stub_enabled() -> bool:
    return os.environ.get("UTA_E2E_REAL_MAVEN_COMPILE", "").lower() not in {"1", "true", "yes"}


def _stub_maven_compile(monkeypatch):
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


@pytest.mark.e2e_git_home
@pytest.mark.parametrize("label,repo_path,env_name,module_env", cross_repo_matrix())
def test_pipeline_dry_run_cross_org(label, repo_path, env_name, module_env, monkeypatch):
    if not os.path.isdir(repo_path):
        pytest.skip(f"{label} repo not found ({env_name}={repo_path})")
    _stub_maven_compile(monkeypatch)

    mod = infer_java_module(
        repo_path,
        explicit_env=module_env,
        use_global_module_env=False,
    )
    java_root = module_main_java(repo_path, mod)
    if not java_root.is_dir():
        pytest.skip(f"No src/main/java under module {mod!r} in {repo_path}")

    from uta.graph.workflow import build_workflow

    workflow_app = build_workflow()
    initial_state = {
        "repo_path": repo_path,
        "module": mod or None,
        "days": 1000,
        "max_files": 2,
        "coverage_gate": 1,
        "mutation_gate": 0,
        "classes_per_agent_run": 1,
        "branch_name": "unit-code-gen-e2e",
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
    err = final_state.get("error")
    if err:
        assert False, f"{label}: {err}"
    assert final_state.get("finished") is True
    results = final_state.get("results", {})
    assert len(results) > 0, f"{label}: no candidates processed"
    for fqn, res in results.items():
        assert res["status"] == "SKIP", f"{label}: {fqn}"


@pytest.mark.e2e_git_home
@pytest.mark.parametrize("label,repo_path,env_name,module_env", cross_repo_matrix())
def test_largest_java_source_parses(label, repo_path, env_name, module_env):
    """Stress: largest ``src/main/java`` file must parse (proxy for huge classes in fix-loop)."""
    if not os.path.isdir(repo_path):
        pytest.skip(f"{label} repo not found")

    mod = infer_java_module(
        repo_path,
        explicit_env=module_env,
        use_global_module_env=False,
    )
    largest = find_largest_main_java(repo_path, mod)
    if not largest:
        pytest.skip("No Java sources")
    path, nlines = largest
    if nlines < 500:
        pytest.skip(f"Largest file only {nlines} lines (need ~500+ for stress intent)")

    from uta.language.java.parse.java_parser import JavaParser

    parser = JavaParser()
    result = parser.parse_file(str(path))
    assert result.package or result.symbols, f"{label}: parse produced empty result for {path}"
