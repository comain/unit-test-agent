from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable, Optional

from e2e_repo_paths import infer_java_module, primary_repo_path, python_repo_matrix
from uta.ci_plugin.enforcement import EnforcementResult, EnforcementResultStatus
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTriggerRequest
from uta.ci_plugin.service import CiPluginService
from uta.engine.languages import RawTargetSelection, default_registry, resolve_language
from uta.language.python.batch import run_python_batch_generation
from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.verification.runner import (
    CoverageSummary,
    MutationSummary,
    PythonVerificationResult,
    resolve_python_runtime_config,
    verify_python_target,
)
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads


@dataclass(frozen=True)
class E2ELaneConfig:
    language: str
    repo_path: Path
    target: str
    test_paths: tuple[str, ...]
    python_bin: str = "python3"
    mutmut_bin: str = "mutmut"
    repo_env: str = ""
    target_env: str = ""

    def replace(self, **kwargs: Any) -> "E2ELaneConfig":
        return replace(self, **kwargs)

    @property
    def syntax_version(self) -> str:
        return "python2" if self.language == "python2" else "python3"


@dataclass(frozen=True)
class JavaE2ELaneConfig:
    language: str
    repo_path: Path
    module: str = ""

    def replace(self, **kwargs: Any) -> "JavaE2ELaneConfig":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class E2EStageRecord:
    lane: str
    stage: int
    name: str
    status: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "stage": self.stage,
            "name": self.name,
            "status": self.status,
            "details": self.details,
        }


def python_lane_configs() -> list[E2ELaneConfig]:
    configs: list[E2ELaneConfig] = []
    for item in python_repo_matrix():
        language = str(item["language"])
        test_paths = _test_paths_for(language)
        configs.append(
            E2ELaneConfig(
                language=language,
                repo_path=Path(str(item["repo_path"])).expanduser().resolve(),
                target=str(item["target"]),
                test_paths=test_paths,
                python_bin=str(item["python_bin"]),
                mutmut_bin=str(item["mutmut_bin"]),
                repo_env=str(item["repo_env"]),
                target_env=str(item["target_env"]),
            )
        )
    return configs


def java_lane_config() -> JavaE2ELaneConfig:
    repo = Path(primary_repo_path()).expanduser().resolve()
    module = infer_java_module(str(repo)) if repo.exists() else ""
    return JavaE2ELaneConfig(language="java", repo_path=repo, module=module)


class E2EStageRunner:
    def __init__(self, *, results_dir: Path) -> None:
        self.results_dir = Path(results_dir)

    def run_java_stage1_scan_context(self, config: JavaE2ELaneConfig) -> E2EStageRecord:
        if not config.repo_path.exists():
            return E2EStageRecord(
                "java",
                1,
                "scan_parse_context",
                "skipped",
                {"reason": "missing_java_repo", "repo_path": str(config.repo_path)},
            )
        from uta.engine.parse import ParseProjectRequest, make_parse_provider
        from uta.engine.source_selection import get_all_java_files

        files = get_all_java_files(str(config.repo_path), config.module)
        if not files:
            return E2EStageRecord(
                "java",
                1,
                "scan_parse_context",
                "failed",
                {"reason": "no_java_files", "repo_path": str(config.repo_path), "module": config.module},
            )
        parsed = make_parse_provider("java").parse_project(
            ParseProjectRequest(repo_path=config.repo_path, module=config.module)
        )
        details = {
            "language": "java",
            "repo_path": str(config.repo_path),
            "module": config.module,
            "language_decision": resolve_language(
                default_registry(),
                config.repo_path,
                explicit_language="java",
            ).as_dict(),
            "selected_count": len(files),
            "first_file": files[0][0],
            "parsed_symbol_count": len(parsed.callables),
            "diagnostic_count": len(parsed.diagnostics),
        }
        return E2EStageRecord("java", 1, "scan_parse_context", "passed", details)

    def run_stage1_scan_context(self, config: E2ELaneConfig) -> E2EStageRecord:
        target = _python_target(config)
        builder = PythonContextBuilder(config.repo_path)
        index = builder.export_project_index(max_files=500)
        context = builder.build_target_context(target)
        selected_targets = [item.get("path") for item in index.get("files") or []]
        details = {
            "repo_path": str(config.repo_path),
            "target": config.target,
            "language_decision": resolve_language(
                default_registry(),
                config.repo_path,
                targets=[config.target],
            ).as_dict(),
            "selected_targets": selected_targets,
            "context_found": bool(context.get("found")),
            "syntax_version": (context.get("syntax") or {}).get("version"),
            "parser_backend": (context.get("syntax") or {}).get("parser_backend"),
            "symbol_count": len(context.get("symbols") or []),
            "index_path": str(config.repo_path / ".uta_cache" / "python_context" / "index.json"),
        }
        status = "passed" if details["context_found"] else "failed"
        return E2EStageRecord(config.language, 1, "scan_parse_context", status, details)

    def run_stage2_enforcement(
        self,
        config: E2ELaneConfig,
        *,
        fake_tools: bool = False,
        run_command: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> E2EStageRecord:
        if config.language == "python2" and not fake_tools:
            missing = _missing_executable(config.python_bin)
            if missing:
                return E2EStageRecord(
                    config.language,
                    2,
                    "enforcement_only",
                    "skipped",
                    {"reason": "missing_python2_runtime", "detail": missing},
                )
            missing = _missing_executable(config.mutmut_bin)
            if missing:
                return E2EStageRecord(
                    config.language,
                    2,
                    "enforcement_only",
                    "skipped",
                    {"reason": "missing_python2_mutmut", "detail": missing},
                )

        target = _python_target(config)
        command = run_command
        if fake_tools:
            command = _fake_python_verify_command(config)
        runtime = resolve_python_runtime_config(
            config.repo_path,
            overrides={
                "python_bin": config.python_bin,
                "python2_bin": config.python_bin if config.language == "python2" else None,
                "mutmut_bin": config.mutmut_bin,
                "python2_mutmut_bin": config.mutmut_bin if config.language == "python2" else None,
            },
        )
        result = verify_python_target(
            config.repo_path,
            target,
            test_paths=list(config.test_paths),
            syntax_version=config.syntax_version,
            coverage_gate=100.0 if fake_tools else float(os.environ.get("UTA_E2E_COVERAGE_GATE", "80")),
            mutation_gate=100.0 if fake_tools else float(os.environ.get("UTA_E2E_MUTATION_GATE", "70")),
            config=runtime,
            run_command=command,
            changed_lines=None if fake_tools else _changed_lines_for_target(config),
        )
        details = {
            "backend": "python_enforcer",
            "status": result.status,
            "reason_code": result.reason_code,
            "coverage_passed": bool(result.coverage and result.coverage.passed),
            "mutation_passed": bool(result.mutation and result.mutation.passed),
            "runtime_lane": result.mutation.runtime_lane if result.mutation else None,
            "coverage_scope": result.coverage.scope if result.coverage else None,
            "mutation_scope": result.mutation.scope if result.mutation else None,
        }
        return E2EStageRecord(config.language, 2, "enforcement_only", "passed" if result.status == "passed" else "failed", details)

    def run_stage3_batch_plumbing(self, config: E2ELaneConfig) -> E2EStageRecord:
        target = _python_target(config)
        db_path = self.results_dir / f"{config.language}-tasks.db"
        manager = TaskManager(db_path)
        task_id = manager.create_task_targets(repo_path=str(config.repo_path), targets=[target], language="python")
        manager.mark_running(task_id, stage="startup")
        result = run_python_batch_generation(
            repo_path=config.repo_path,
            targets=[target],
            task_id=task_id,
            task_db_path=db_path,
            client_factory=lambda repo_path: _FakePythonOpenCodeClient(repo_path),
            verification_runner=_passing_python_verification,
        )
        task = manager.get_task(task_id)
        target_row = manager.list_class_tasks(task_id)[0]
        target_result = result.results[target.target_id]
        details = {
            "task_id": task_id,
            "task_status": task["status"],
            "target_status": target_row["status"],
            "target_id": target.target_id,
            "generated_test_path": target_result["test_file_path"],
            "session_ids": result.session_ids,
        }
        return E2EStageRecord(config.language, 3, "batch_task_plumbing", "passed" if task["status"] == "COMPLETED" else "failed", details)

    def run_stage4_real_generation(self, config: E2ELaneConfig) -> E2EStageRecord:
        if os.environ.get("UTA_E2E_REAL_OPENCODE", "").lower() not in {"1", "true", "yes"}:
            return E2EStageRecord(
                config.language,
                4,
                "real_generation_smoke",
                "skipped",
                {"reason": "set UTA_E2E_REAL_OPENCODE=1 to run real OpenCode generation"},
            )
        if config.language != "python3":
            return E2EStageRecord(
                config.language,
                4,
                "real_generation_smoke",
                "skipped",
                {"reason": "real OpenCode generation smoke is only defined for Python 3"},
            )
        target = _python_target(config)
        db_path = self.results_dir / f"{config.language}-real-opencode-tasks.db"
        manager = TaskManager(db_path)
        task_id = manager.create_task_targets(repo_path=str(config.repo_path), targets=[target], language="python")
        manager.mark_running(task_id, stage="startup")
        try:
            from uta.cli import _ensure_model_auth
            from uta.opencode.config import generate_opencode_config

            generate_opencode_config(str(config.repo_path))
            _ensure_model_auth(str(config.repo_path))
            client_factory = None
            if os.environ.get("UTA_E2E_REAL_OPENCODE_FULL_PROMPT", "").lower() not in {"1", "true", "yes"}:
                client_factory = lambda repo_path: _RealPythonOpenCodeSmokeClient(repo_path)
            result = run_python_batch_generation(
                repo_path=config.repo_path,
                targets=[target],
                task_id=task_id,
                task_db_path=db_path,
                client_factory=client_factory,
                verification_runner=_pytest_smoke_python_verification,
                timeout_seconds=int(os.environ.get("UTA_E2E_REAL_OPENCODE_TIMEOUT_SECONDS", "300")),
            )
            task = manager.get_task(task_id)
            target_result = result.results.get(target.target_id) or {}
            details = {
                "task_id": task_id,
                "task_status": task["status"],
                "target_status": target_result.get("status"),
                "target_id": target.target_id,
                "session_ids": result.session_ids,
            }
            return E2EStageRecord(
                config.language,
                4,
                "real_generation_smoke",
                "passed" if task["status"] == "COMPLETED" else "failed",
                details,
            )
        except Exception as exc:  # noqa: BLE001
            return E2EStageRecord(
                config.language,
                4,
                "real_generation_smoke",
                "failed",
                {"task_id": task_id, "target_id": target.target_id, "error": str(exc)},
            )
    def run_stage5_ci_plugin_repair(self, config: E2ELaneConfig) -> E2EStageRecord:
        task_manager = TaskManager(self.results_dir / f"{config.language}-ci-tasks.db")
        runner = _SequencedPythonRunner(config)
        service = CiPluginService(
            workspace_manager=_StaticWorkspaceManager(config.repo_path),
            python_enforcement_runner=runner,
            task_manager=task_manager,
        )
        record = service.submit(
            CiTriggerRequest.model_validate(
                {
                    "appName": "python-fixture",
                    "gitUrl": "git@example.invalid/python-fixture.git",
                    "branch": "feature/python-e2e",
                    "language": "python",
                }
            )
        )
        initial_status = record.status.value
        fix = service.create_fix_session(
            record,
            CreateFixSessionRequest(target_ids=[_target_id(config.target)], user_context="repair Python test coverage"),
        )
        session = fix["session"]
        repo_task_id = int(session["repoTaskId"])
        task_manager.mark_completed(repo_task_id)
        refreshed = service.get(record.task_id)
        repo_task = task_manager.get_task(repo_task_id)
        selection = json_loads(repo_task.get("selection_json") or "{}")
        details = {
            "initial_status": initial_status,
            "repair_task_id": repo_task_id,
            "repair_task_language": repo_task["language"],
            "repair_quality_gate_backend": selection.get("quality_gate_backend"),
            "rerun_status": refreshed.status.value if refreshed else None,
            "runner_calls": len(runner.calls),
            "session_status": (refreshed.fix_sessions[0] if refreshed else {}).get("status"),
        }
        status = "passed" if details["rerun_status"] == "success" and details["runner_calls"] == 2 else "failed"
        return E2EStageRecord(config.language, 5, "ci_plugin_repair_rerun", status, details)

    def run_python_stages(self, config: E2ELaneConfig, *, fake_enforcement: bool = False) -> list[E2EStageRecord]:
        records = [
            self.run_stage1_scan_context(config),
            self.run_stage2_enforcement(config, fake_tools=fake_enforcement),
        ]
        if config.language == "python3":
            records.extend(
                [
                    self.run_stage3_batch_plumbing(config),
                    self.run_stage4_real_generation(config),
                    self.run_stage5_ci_plugin_repair(config),
                ]
            )
        return records

    def write_report(self, records: Iterable[E2EStageRecord]) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        rows = [record.as_dict() for record in records]
        summary = {
            "passed": sum(1 for row in rows if row["status"] == "passed"),
            "failed": sum(1 for row in rows if row["status"] == "failed"),
            "skipped": sum(1 for row in rows if row["status"] == "skipped"),
        }
        path = self.results_dir / "phase9-staged-e2e-results.json"
        path.write_text(json.dumps({"summary": summary, "records": rows}, indent=2, sort_keys=True), encoding="utf-8")
        return path


def _test_paths_for(language: str) -> tuple[str, ...]:
    env_name = "UTA_E2E_PY2_TEST_PATHS" if language == "python2" else "UTA_E2E_PY3_TEST_PATHS"
    raw = os.environ.get(env_name)
    if raw:
        return tuple(path for path in raw.split(os.pathsep) if path)
    return ("tests/test_legacy_job.py",) if language == "python2" else ("tests/test_forecast.py",)


def _missing_executable(command: str) -> str:
    path = Path(command)
    if path.parent != Path("."):
        return "" if path.exists() and os.access(path, os.X_OK) else f"configured executable is not available: {command}"
    return "" if shutil.which(command) else f"configured executable is not available on PATH: {command}"


def _python_target(config: E2ELaneConfig):
    adapter = default_registry().adapter_for("python")
    return adapter.normalize_target(RawTargetSelection(target=config.target))


def _changed_lines_for_target(config: E2ELaneConfig) -> dict[str, list[int]]:
    target = _python_target(config)
    if not target.source_path or not target.symbol:
        return {}
    context = PythonContextBuilder(config.repo_path).build_target_context(target)
    for symbol in context.get("symbols") or []:
        if symbol.get("qualified_name") == target.symbol or symbol.get("name") == target.symbol:
            start = int(symbol.get("line") or 0)
            end = int(symbol.get("end_line") or start)
            if end <= start:
                end = _expand_single_line_python_symbol(config.repo_path / target.source_path, start)
            if start > 0 and end >= start:
                return {target.source_path: list(range(start, end + 1))}
    return {}


def _expand_single_line_python_symbol(source_path: Path, start: int) -> int:
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return start
    upper = min(len(lines), start + 3)
    for line_no in range(max(1, start), upper + 1):
        text = lines[line_no - 1].strip()
        if text and not text.startswith("def "):
            return line_no
    return min(len(lines), start + 1)


def _fake_python_verify_command(config: E2ELaneConfig):
    def fake_run(cmd, cwd=None, timeout=None, env=None):
        executable = cmd[0]
        if cmd[:2] == [config.mutmut_bin, "--version"]:
            version = "mutmut 1.5.0" if config.language == "python2" else "mutmut 3.0.0"
            return subprocess.CompletedProcess(cmd, 0, stdout=version, stderr="")
        if cmd[1:] == ["--version"]:
            version = "Python 2.7.18" if config.language == "python2" else "Python 3.11.8"
            return subprocess.CompletedProcess(cmd, 0, stdout=version, stderr="")
        if cmd[:3] == [executable, "-m", "pytest"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="pytest 8.0.0", stderr="")
        if cmd[:3] == [executable, "-m", "coverage"] and cmd[3] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="Coverage.py 7.0", stderr="")
        if cmd[:4] == [executable, "-m", "coverage", "run"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:4] == [executable, "-m", "coverage", "xml"]:
            coverage_xml = config.repo_path / ".uta_cache" / "python" / "coverage" / "coverage.xml"
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            source_path = config.target.split("::", 1)[0]
            coverage_xml.write_text(
                "<coverage><packages><package><classes>"
                f"<class filename='{source_path}'><lines><line number='1' hits='1'/></lines></class>"
                "</classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == [config.mutmut_bin, "run"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1 generated, 1 killed, 0 survived, 0 no coverage", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class _FakePythonOpenCodeClient:
    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path
        self.sessions: list[str] = []

    def create_session(self, model_id=None, provider_id=None):
        session_id = f"phase9-{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        return session_id

    def send_message_split(self, session_id, stable_prefix, volatile_tail, model_id=None):
        return {}

    def poll_completion(self, session_id, timeout=600, on_update=None):
        return {"type": "completed", "result": "```python\ndef test_phase9_generated():\n    assert True\n```"}

    def analyze_session_tokens(self, session_id):
        return {
            "main_model_tokens": {"input": 10, "output": 4, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 14},
            "small_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "total_tokens": {"input": 10, "output": 4, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 14},
        }

    def analyze_session_retrospect(self, session_id):
        return {"hints": ["Python target generated through Phase 9 staged harness"]}


class _RealPythonOpenCodeSmokeClient:
    """Real OpenCode client with a compact deterministic response request for E2E stability."""

    def __init__(self, repo_path: str) -> None:
        from uta.opencode.client import OpenCodeClient

        self._inner = OpenCodeClient(repo_path=repo_path)

    def create_session(self, model_id=None, provider_id=None):
        return self._inner.create_session(model_id=model_id, provider_id=provider_id)

    def send_message_split(self, session_id, stable_prefix, volatile_tail, model_id=None):
        prompt = (
            "Reply only with this exact fenced Python code:\n"
            "```python\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n\n"
            "from jobs.forecast import forecast_for_store\n\n\n"
            "def test_forecast_for_store_empty_history_returns_zero():\n"
            "    assert forecast_for_store([]) == 0\n\n\n"
            "def test_forecast_for_store_uses_recent_three_values():\n"
            "    assert forecast_for_store([2, 4, 8, 10], uplift=1.0) == 7\n"
            "```\n"
        )
        return self._inner.send_message(session_id, prompt, model_id=model_id)

    def poll_completion(self, session_id, timeout=600, on_update=None):
        return self._inner.poll_completion(session_id, timeout=timeout, on_update=on_update)

    def analyze_session_tokens(self, session_id):
        return self._inner.analyze_session_tokens(session_id)

    def analyze_session_retrospect(self, session_id):
        return self._inner.analyze_session_retrospect(session_id)


def _passing_python_verification(*args, **kwargs) -> PythonVerificationResult:
    return PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=1,
            total=1,
            rate=100.0,
            gate=float(kwargs.get("coverage_gate") or 100.0),
            passed=True,
            xml_path=".uta_cache/python/coverage/coverage.xml",
        ),
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=1,
            killed=1,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=float(kwargs.get("mutation_gate") or 100.0),
            passed=True,
        ),
        message="Phase 9 fake verification passed",
    )


def _pytest_smoke_python_verification(repo_path, target, **kwargs) -> PythonVerificationResult:
    test_paths = [str(path) for path in kwargs.get("test_paths") or [] if str(path)]
    if not test_paths:
        return PythonVerificationResult(
            status="failed",
            reason_code="missing_generated_tests",
            tests_pass=False,
            message="No generated test paths were supplied for Phase 9 real generation smoke",
        )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *test_paths],
        cwd=repo_path,
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("UTA_E2E_REAL_OPENCODE_TIMEOUT_SECONDS", "300")),
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        return PythonVerificationResult(
            status="failed",
            reason_code="pytest_failed",
            tests_pass=False,
            message=output,
        )
    return PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        message="Phase 9 real OpenCode pytest smoke passed",
    )


class _StaticWorkspaceManager:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path)
        self.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    def prepare(self, git_url: str, branch: str, task_id: str) -> Path:
        return self.repo_path


class _SequencedPythonRunner:
    def __init__(self, config: E2ELaneConfig) -> None:
        self.config = config
        self.calls: list[Path] = []

    def run(self, repo_path: Path) -> EnforcementResult:
        self.calls.append(Path(repo_path))
        passed = len(self.calls) > 1
        evidence = {
            "schemaVersion": 1,
            "language": "python",
            "backend": "python_enforcer",
            "status": "passed" if passed else "failed",
            "passed": passed,
            "reasonCode": "passed" if passed else "coverage_gate_failed",
            "changedProductionFiles": [self.config.target.split("::", 1)[0]],
            "targets": [
                {
                    "language": "python",
                    "target_id": _target_id(self.config.target),
                    "target": self.config.target,
                    "source_path": self.config.target.split("::", 1)[0],
                }
            ],
            "coverage": {"covered": 1 if passed else 0, "total": 1, "rate": 100.0 if passed else 0.0, "passed": passed},
            "mutation": {"generated": 1, "killed": 1 if passed else 0, "survived": 0 if passed else 1, "rate": 100.0 if passed else 0.0, "passed": passed},
        }
        return EnforcementResult(
            status=EnforcementResultStatus.passed if passed else EnforcementResultStatus.failed,
            passed=passed,
            command=["uta", "python-enforce", "--repo", str(repo_path)],
            summary="Python enforcement passed" if passed else "Python enforcement failed",
            language="python",
            backend="python_enforcer",
            evidence=evidence,
        )


def _target_id(target: str) -> str:
    if "::" in target:
        return f"pysymbol:{target}"
    return f"pyfile:{target}"
