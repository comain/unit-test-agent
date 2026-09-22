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
from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTriggerRequest
from uta.app.service import ApiTriggerService
from uta.shared.languages import RawTargetSelection, default_registry, resolve_language
from uta.language.java.verification.runner import JavaRuntimeConfig, JavaVerificationResult, verify_java_target
from uta.language.python.batch import run_python_batch_generation
from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.verification.runner import (
    CoverageSummary,
    MutationSummary,
    PythonVerificationResult,
    UTA_OWNED_MUTMUT_VERSION,
    resolve_python_runtime_config,
    verify_python_target,
)
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads
from uta.shared.targets import TargetIdentity


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
class JavaEnforcementCandidate:
    target: TargetIdentity
    test_class_fqn: str
    test_selector: str
    source_path: Path
    test_path: Path
    module: str


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
        from uta.shared.parse import ParseProjectRequest, make_parse_provider
        from uta.testgen.source_selection import get_all_source_files

        files = get_all_source_files("java", str(config.repo_path), config.module)
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

    def run_java_stage2_enforcement(
        self,
        config: JavaE2ELaneConfig,
        *,
        verify_func: Callable[..., JavaVerificationResult] = verify_java_target,
        require_tools: bool = True,
    ) -> E2EStageRecord:
        if not config.repo_path.exists():
            return E2EStageRecord(
                "java",
                2,
                "enforcement_only",
                "skipped",
                {"reason": "missing_java_repo", "repo_path": str(config.repo_path)},
            )
        if require_tools:
            missing_tool = _missing_java_enforcement_tool()
            if missing_tool:
                return E2EStageRecord(
                    "java",
                    2,
                    "enforcement_only",
                    "skipped",
                    {"reason": "missing_java_enforcement_tool", "detail": missing_tool},
                )
        candidate = _java_enforcement_candidate(config)
        if candidate is None:
            return E2EStageRecord(
                "java",
                2,
                "enforcement_only",
                "skipped",
                {
                    "reason": "missing_java_test_candidate",
                    "repo_path": str(config.repo_path),
                    "module": config.module,
                },
            )
        runtime = JavaRuntimeConfig(
            module=candidate.module or None,
            timeout_seconds=int(os.environ.get("UTA_E2E_JAVA_TIMEOUT_SECONDS", "300")),
            pitest_timeout_seconds=int(os.environ.get("UTA_E2E_JAVA_PIT_TIMEOUT_SECONDS", "300")),
        )
        try:
            result = verify_func(
                config.repo_path,
                candidate.target,
                test_class_fqn=candidate.test_class_fqn,
                test_selector=candidate.test_selector,
                coverage_gate=float(os.environ.get("UTA_E2E_JAVA_COVERAGE_GATE", "80")),
                mutation_gate=float(os.environ.get("UTA_E2E_JAVA_MUTATION_GATE", "70")),
                config=runtime,
            )
        except FileNotFoundError as exc:
            return E2EStageRecord(
                "java",
                2,
                "enforcement_only",
                "skipped",
                {"reason": "missing_java_enforcement_tool", "detail": str(exc)},
            )
        details = {
            "backend": "java_verifier",
            "target": candidate.target.target_id,
            "source_path": str(candidate.source_path),
            "test_class_fqn": candidate.test_class_fqn,
            "test_selector": candidate.test_selector,
            "test_path": str(candidate.test_path),
            "module": candidate.module,
            "status": result.status,
            "reason_code": result.reason_code,
            "message": result.message,
            "tests_pass": result.tests_pass,
            "coverage_passed": bool(result.coverage and result.coverage.passed),
            "mutation_passed": bool(result.mutation and result.mutation.passed),
            "coverage": result.coverage.as_dict() if result.coverage else None,
            "mutation": result.mutation.as_dict() if result.mutation else None,
            "commands": [command.as_dict() for command in result.commands],
            "artifacts": {
                "coverageXml": result.coverage.xml_path if result.coverage else "",
                "mutationReport": result.mutation.report_path if result.mutation else "",
            },
        }
        return E2EStageRecord(
            "java",
            2,
            "enforcement_only",
            "passed" if result.status == "passed" else "failed",
            details,
        )

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
            changed_lines=_fake_changed_lines_for_target(config) if fake_tools else _changed_lines_for_target(config),
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
            "coverage": result.coverage.as_dict() if result.coverage else None,
            "mutation": result.mutation.as_dict() if result.mutation else None,
            "commands": [command.as_dict() for command in result.commands],
            "artifacts": {
                "coverageXml": result.coverage.xml_path if result.coverage else "",
                "mutationArtifacts": dict(result.mutation.artifacts) if result.mutation else {},
            },
        }
        return E2EStageRecord(config.language, 2, "enforcement_only", "passed" if result.status == "passed" else "failed", details)

    def run_stage3_batch_plumbing(self, config: E2ELaneConfig) -> E2EStageRecord:
        from agent_core.harness import OpenCodeHarness

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
            harness_factory=lambda _repo_path: OpenCodeHarness(
                session_client_factory=lambda repo_path: _FakePythonOpenCodeClient(
                    repo_path
                )
            ),
            enforcer=_passing_python_enforcement,
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
            from uta.app.cli import _ensure_model_auth
            from agent_core.harness.config import generate_opencode_config

            generate_opencode_config(str(config.repo_path))
            _ensure_model_auth(str(config.repo_path))
            harness_factory = None
            if os.environ.get("UTA_E2E_REAL_OPENCODE_FULL_PROMPT", "").lower() not in {"1", "true", "yes"}:
                from agent_core.harness import OpenCodeHarness

                harness_factory = lambda _repo_path: OpenCodeHarness(
                    session_client_factory=lambda repo_path: _RealPythonOpenCodeSmokeClient(
                        repo_path
                    )
                )
            result = run_python_batch_generation(
                repo_path=config.repo_path,
                targets=[target],
                task_id=task_id,
                task_db_path=db_path,
                harness_factory=harness_factory,
                enforcer=_pytest_smoke_python_enforcement,
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
    def run_stage5_api_trigger_repair(self, config: E2ELaneConfig) -> E2EStageRecord:
        task_manager = TaskManager(self.results_dir / f"{config.language}-ci-tasks.db")
        runner = _SequencedPythonRunner(config)
        service = ApiTriggerService(
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
        target_rows = task_manager.list_class_tasks(repo_task_id)
        assert target_rows
        task_manager.db.update_class_task(
            int(target_rows[0]["id"]),
            status="PASS",
            current_stage="finished",
            stage="finished",
            current_detail="PASS",
            coverage_line=100.0,
            coverage=100.0,
            mutation_score=100.0,
            surviving_mutants=0,
            total_mutants=1,
            test_count=1,
            test_file_path="tests/uta_generated/test_jobs_forecast.py",
        )
        task_manager.mark_completed(repo_task_id)
        refreshed = service.get(record.task_id)
        repo_task = task_manager.get_task(repo_task_id)
        selection = json_loads(repo_task.get("selection_json") or "{}")
        rdc_context = json_loads(repo_task.get("rdc_context_json") or "{}")
        enforcement = rdc_context.get("enforcement") if isinstance(rdc_context.get("enforcement"), dict) else {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        details = {
            "initial_status": initial_status,
            "repair_task_id": repo_task_id,
            "repair_task_language": repo_task["language"],
            "repair_quality_gate_backend": selection.get("quality_gate_backend"),
            "repair_changed_lines": evidence.get("changedLines") or evidence.get("changed_lines") or {},
            "rerun_status": refreshed.status.value if refreshed else None,
            "runner_calls": len(runner.calls),
            "session_status": (refreshed.fix_sessions[0] if refreshed else {}).get("status"),
        }
        status = "passed" if details["rerun_status"] == "success" and details["session_status"] == "green" else "failed"
        return E2EStageRecord(config.language, 5, "api_trigger_repair_rerun", status, details)

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
                    self.run_stage5_api_trigger_repair(config),
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


def _missing_java_enforcement_tool() -> str:
    for command in ("java", "mvn"):
        missing = _missing_executable(command)
        if missing:
            return missing
    return ""


def _java_enforcement_candidate(config: JavaE2ELaneConfig) -> Optional[JavaEnforcementCandidate]:
    explicit_class = os.environ.get("UTA_E2E_JAVA_CLASS_FQN", "").strip()
    if explicit_class:
        module_root = _java_module_root(config)
        test_class_fqn = os.environ.get("UTA_E2E_JAVA_TEST_CLASS_FQN", "").strip() or f"{explicit_class}Test"
        return JavaEnforcementCandidate(
            target=TargetIdentity.java_class(explicit_class),
            test_class_fqn=test_class_fqn,
            test_selector=os.environ.get("UTA_E2E_JAVA_TEST_SELECTOR", "").strip() or test_class_fqn.rsplit(".", 1)[-1],
            source_path=_java_source_path_for_fqn(module_root, explicit_class),
            test_path=_java_test_path_for_fqn(module_root, test_class_fqn),
            module=config.module,
        )

    candidate = _java_enforcement_candidate_in_module(config.repo_path, config.module)
    if candidate is not None:
        return candidate
    for module_root in sorted(path for path in config.repo_path.iterdir() if path.is_dir()):
        module = module_root.name
        if module == config.module:
            continue
        candidate = _java_enforcement_candidate_in_module(config.repo_path, module)
        if candidate is not None:
            return candidate
    if os.environ.get("UTA_E2E_JAVA_DISABLE_PROBE", "").lower() in {"1", "true", "yes"}:
        return None
    return _write_java_probe_candidate(config)


def _java_enforcement_candidate_in_module(repo_path: Path, module: str) -> Optional[JavaEnforcementCandidate]:
    module_root = repo_path / module if module else repo_path

    main_root = module_root / "src" / "main" / "java"
    test_root = module_root / "src" / "test" / "java"
    if not main_root.exists() or not test_root.exists():
        return None
    for source_path in sorted(path for path in main_root.rglob("*.java") if path.is_file()):
        if source_path.name.endswith(("Test.java", "Tests.java")):
            continue
        class_fqn = _java_fqn_for_file(source_path, main_root)
        relative = source_path.relative_to(main_root)
        for suffix in ("Test.java", "Tests.java"):
            test_path = test_root / relative.with_name(f"{source_path.stem}{suffix}")
            if not test_path.exists():
                continue
            if "@Category(" in test_path.read_text(encoding="utf-8", errors="ignore"):
                continue
            test_class_fqn = _java_fqn_for_file(test_path, test_root)
            return JavaEnforcementCandidate(
                target=TargetIdentity.java_class(class_fqn),
                test_class_fqn=test_class_fqn,
                test_selector=test_class_fqn.rsplit(".", 1)[-1],
                source_path=source_path,
                test_path=test_path,
                module=module,
            )
    return None


def _write_java_probe_candidate(config: JavaE2ELaneConfig) -> Optional[JavaEnforcementCandidate]:
    module = _java_probe_module(config.repo_path, config.module)
    module_root = config.repo_path / module if module else config.repo_path
    main_root = module_root / "src" / "main" / "java"
    test_root = module_root / "src" / "test" / "java"
    if not main_root.exists() or not test_root.exists():
        return None
    class_fqn = "com.example.uta.e2e.UtaE2eProbe"
    test_class_fqn = "com.example.uta.e2e.UtaE2eProbeTest"
    source_path = _java_source_path_for_fqn(module_root, class_fqn)
    test_path = _java_test_path_for_fqn(module_root, test_class_fqn)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        "\n".join(
            [
                "package com.example.uta.e2e;",
                "",
                "public class UtaE2eProbe {",
                "    public int add(int left, int right) {",
                "        return left + right;",
                "    }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    test_path.write_text(
        "\n".join(
            [
                "package com.example.uta.e2e;",
                "",
                "import org.junit.Assert;",
                "import org.junit.Test;",
                "",
                "public class UtaE2eProbeTest {",
                "    @Test",
                "    public void addsTwoValues() {",
                "        UtaE2eProbe probe = new UtaE2eProbe();",
                "        Assert.assertEquals(5, probe.add(2, 3));",
                "    }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return JavaEnforcementCandidate(
        target=TargetIdentity.java_class(class_fqn),
        test_class_fqn=test_class_fqn,
        test_selector=test_class_fqn.rsplit(".", 1)[-1],
        source_path=source_path,
        test_path=test_path,
        module=module,
    )


def _java_probe_module(repo_path: Path, preferred_module: str) -> str:
    modules = [preferred_module] if preferred_module else []
    modules.extend(
        path.name
        for path in sorted(repo_path.iterdir())
        if path.is_dir() and path.name not in set(modules)
    )
    for module in modules:
        module_root = repo_path / module if module else repo_path
        if (module_root / "src" / "main" / "java").exists() and (module_root / "src" / "test" / "java").exists():
            return module
    return preferred_module


def _java_module_root(config: JavaE2ELaneConfig) -> Path:
    return config.repo_path / config.module if config.module else config.repo_path


def _java_source_path_for_fqn(module_root: Path, class_fqn: str) -> Path:
    return module_root / "src" / "main" / "java" / Path(*class_fqn.split(".")).with_suffix(".java")


def _java_test_path_for_fqn(module_root: Path, class_fqn: str) -> Path:
    return module_root / "src" / "test" / "java" / Path(*class_fqn.split(".")).with_suffix(".java")


def _java_fqn_for_file(path: Path, source_root: Path) -> str:
    package = _java_package(path)
    if package:
        return f"{package}.{path.stem}"
    return ".".join(path.relative_to(source_root).with_suffix("").parts)


def _java_package(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("package ") and stripped.endswith(";"):
                return stripped.removeprefix("package ").removesuffix(";").strip()
    except OSError:
        return ""
    return ""


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


def _first_changed_line(config: E2ELaneConfig, source_path: str) -> int:
    lines = _fake_changed_lines_for_target(config).get(source_path) or []
    return int(lines[0]) if lines else 1


def _fake_changed_lines_for_target(config: E2ELaneConfig) -> dict[str, list[int]]:
    changed = _changed_lines_for_target(config)
    return {path: [int(lines[-1])] for path, lines in changed.items() if lines}


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
            version = "mutmut 1.5.0" if config.language == "python2" else f"mutmut {UTA_OWNED_MUTMUT_VERSION}"
            return subprocess.CompletedProcess(cmd, 0, stdout=version, stderr="")
        if cmd[1:] == ["--version"]:
            version = "Python 2.7.18" if config.language == "python2" else "Python 3.11.8"
            return subprocess.CompletedProcess(cmd, 0, stdout=version, stderr="")
        if cmd[:3] == [executable, "-m", "pytest"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="pytest 8.0.0", stderr="")
        if cmd[:3] == [executable, "-m", "py_compile"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == [executable, "-m", "coverage"] and cmd[3] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="Coverage.py 7.0", stderr="")
        if cmd[0] == executable and Path(cmd[1]).name == "pytest_process.py" and "--coverage-include" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:4] == [executable, "-m", "coverage", "xml"]:
            coverage_xml = config.repo_path / ".uta_cache" / "python" / "coverage" / "coverage.xml"
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            source_path = config.target.split("::", 1)[0]
            covered_line = _first_changed_line(config, source_path)
            coverage_xml.write_text(
                "<coverage><packages><package><classes>"
                f"<class filename='{source_path}'><lines><line number='{covered_line}' hits='1'/></lines></class>"
                "</classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == [config.python_bin, "-c"] and cmd[-2] == "metadata":
            source_path = config.target.split("::", 1)[0]
            symbol = config.target.split("::", 1)[1] if "::" in config.target else "<module>"
            changed_line = _first_changed_line(config, source_path)
            module_name = source_path[:-3].replace("/", ".") if source_path.endswith(".py") else source_path.replace("/", ".")
            if module_name.startswith("src."):
                module_name = module_name[len("src.") :]
            mutant_key = f"{module_name}.x_{symbol}__mutmut_1" if symbol != "<module>" else f"{module_name}.x_module__mutmut_1"
            meta_path = config.repo_path / "mutants" / f"{source_path}.meta"
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({"exit_code_by_key": {mutant_key: None}}), encoding="utf-8")
            meta_path.with_suffix(meta_path.suffix + ".uta.json").write_text(
                json.dumps(
                    {
                        "line_by_key": {mutant_key: changed_line},
                        "operator_by_key": {mutant_key: "call_argument"},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="UTA_MUTMUT_GENERATION_STATS mutated=1 unmodified=0 ignored=0",
                stderr="",
            )
        if cmd[:2] == [config.python_bin, "-c"] and cmd[-2] == "run":
            return subprocess.CompletedProcess(cmd, 0, stdout="1 generated, 1 killed, 0 survived, 0 no coverage", stderr="")
        if config.language == "python2" and cmd[:2] == [config.mutmut_bin, "run"]:
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

    def open_session(self, model_id=None, permissions=None):
        return self.create_session(model_id=model_id)

    def send_message_split(self, session_id, stable_prefix, volatile_tail, model_id=None):
        return {}

    def send_message(self, session_id, content, model_id=None):
        return {}

    def poll_completion(self, session_id, timeout=600, on_update=None):
        return {"type": "completed", "result": "```python\ndef test_phase9_generated():\n    assert True\n```"}

    def run_node(self, *, session_id, phase, prompt, timeout, **kwargs):
        return self.poll_completion(session_id, timeout=timeout)

    def analyze_session_tokens(self, session_id):
        return {
            "main_model_tokens": {"input": 10, "output": 4, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 14},
            "small_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "other_model_tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "total_tokens": {"input": 10, "output": 4, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 14},
        }

    def analyze_session_retrospect(self, session_id):
        return {"hints": ["Python target generated through Phase 9 staged harness"]}

    def delete_session(self, session_id):
        return None


class _RealPythonOpenCodeSmokeClient:
    """Real OpenCode client with a compact deterministic response request for E2E stability."""

    def __init__(self, repo_path: str) -> None:
        from agent_core.harness.client import OpenCodeClient

        self._inner = OpenCodeClient(repo_path=repo_path)

    def create_session(self, model_id=None, provider_id=None):
        return self._inner.create_session(model_id=model_id, provider_id=provider_id)

    def open_session(self, model_id=None, permissions=None):
        return self.create_session(model_id=model_id)

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

    def run_node(self, *, session_id, phase, prompt, timeout, **kwargs):
        compact_prompt = (
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
        self._inner.send_message(session_id, compact_prompt, model_id=kwargs.get("model_id"))
        return self._inner.poll_completion(session_id, timeout=timeout)

    def analyze_session_tokens(self, session_id):
        return self._inner.analyze_session_tokens(session_id)

    def analyze_session_retrospect(self, session_id):
        return self._inner.analyze_session_retrospect(session_id)


def _passing_python_enforcement(**kwargs):
    result = PythonVerificationResult(
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
    return _python_enforcement_envelope(result, run_mutation=kwargs.get("run_mutation", True))


def _pytest_smoke_python_enforcement(**kwargs):
    repo_path = kwargs["repo_path"]
    test_paths = [str(path) for path in kwargs.get("test_paths") or [] if str(path)]
    if not test_paths:
        result = PythonVerificationResult(
            status="failed",
            reason_code="missing_generated_tests",
            tests_pass=False,
            message="No generated test paths were supplied for Phase 9 real generation smoke",
        )
        return _python_enforcement_envelope(result, run_mutation=kwargs.get("run_mutation", True))
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *test_paths],
        cwd=repo_path,
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("UTA_E2E_REAL_OPENCODE_TIMEOUT_SECONDS", "300")),
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        result = PythonVerificationResult(
            status="failed",
            reason_code="pytest_failed",
            tests_pass=False,
            message=output,
        )
        return _python_enforcement_envelope(result, run_mutation=kwargs.get("run_mutation", True))
    result = PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        message="Phase 9 real OpenCode pytest smoke passed",
    )
    return _python_enforcement_envelope(result, run_mutation=kwargs.get("run_mutation", True))


def _python_enforcement_envelope(result, *, run_mutation):
    return {
        "status": result.status,
        "passed": result.status == "passed",
        "reasonCode": result.reason_code,
        "summary": result.message,
        "targetResults": [{
            "status": result.status,
            "reasonCode": result.reason_code,
            "testsPass": result.tests_pass,
            "message": result.message,
            "coverage": result.coverage.as_dict() if result.coverage else None,
            "mutation": (
                result.mutation.as_dict() if run_mutation and result.mutation else None
            ),
        }],
    }


class _StaticWorkspaceManager:
    #: No git workspace of its own: the checkout already exists, so anything
    #: reading it falls back to the shared runner and the real repository.
    workspace = None

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path)

    def prepare(self, git_url: str, branch: str, task_id: str) -> Path:
        return self.repo_path

    def refresh_branch(self, repo_path: Path, *, branch: str) -> str:
        return "static-head"


class _SequencedPythonRunner:
    def __init__(self, config: E2ELaneConfig) -> None:
        self.config = config
        self.calls: list[Path] = []

    def run(self, repo_path: Path) -> QualityGateResult:
        self.calls.append(Path(repo_path))
        # Stay red through the initial check (call 1) and the branch-refresh rerun
        # (call 2) so a repair task is scheduled; go green on the post-repair rerun.
        passed = len(self.calls) > 2
        changed_lines = _changed_lines_for_target(self.config)
        evidence = {
            "schemaVersion": 1,
            "language": "python",
            "backend": "python_enforcer",
            "status": "passed" if passed else "failed",
            "passed": passed,
            "reasonCode": "passed" if passed else "coverage_gate_failed",
            "changedProductionFiles": [self.config.target.split("::", 1)[0]],
            "changedLines": changed_lines,
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
        return QualityGateResult(
            status=QualityGateStatus.passed if passed else QualityGateStatus.failed,
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
