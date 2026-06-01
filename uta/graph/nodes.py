import logging
import hashlib
import os
import re
import shlex
import shutil
import time
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from jinja2 import Template
from uta.compile import classify_compile_errors, error_delta
from uta.opencode.fallback import ProviderRateLimitError, raise_for_provider_fallback_event

logger = logging.getLogger("uta")


class TaskStopRequested(RuntimeError):
    pass


class TaskUnsafeDiffError(RuntimeError):
    pass


class TaskBudgetExceeded(RuntimeError):
    pass


def _git_run(repo_path: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run git in repo_path without relying on newer git's -C flag."""
    return subprocess.run(["git", *args], cwd=repo_path, **kwargs)


def _merge_phase_timings(state: Dict[str, Any], **updates: float) -> Dict[str, float]:
    timings = dict(state.get("phase_timings", {}) or {})
    for key, value in updates.items():
        timings[key] = timings.get(key, 0.0) + float(value)
    return timings


def _status_with_mutation_gate(
    test_ok: bool,
    line_cov: float,
    coverage_gate: int,
    mutation_gate_score: int,
    mutation_score: float,
) -> str:
    if not test_ok or line_cov < coverage_gate:
        return "FAIL"
    if mutation_gate_score > 0 and mutation_score < mutation_gate_score:
        return "MUTATION_FAIL"
    return "PASS"


def _should_run_mutation(test_ok: bool, mutation_gate_score: int) -> bool:
    return bool(test_ok and mutation_gate_score > 0)


def _delegated_gate_context(state: Dict[str, Any]) -> Dict[str, Any]:
    context = state.get("ci_context")
    if isinstance(context, dict):
        return context
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager
            from uta.tasks.models import json_loads

            task = TaskManager(task_db_path).get_task(int(task_id))
            return json_loads(task.get("ci_context_json") or "{}") if task else {}
        except Exception:
            logger.debug("Failed to load CI context for delegated quality gate", exc_info=True)
    return {}


def _run_delegated_quality_gate_once(
    state: Dict[str, Any],
    repo_path: str,
    *,
    run_command: Optional[Any] = None,
) -> Dict[str, Any]:
    from uta.ci_plugin.enforcement import MavenEnforcementRunner, EnforcementResult, EnforcementResultStatus

    context = _delegated_gate_context(state)
    enforcement = context.get("enforcement") if isinstance(context.get("enforcement"), dict) else {}
    raw_command = enforcement.get("command") if isinstance(enforcement, dict) else None
    configured_command = str(state.get("quality_gate_command") or "").strip()
    command = (
        configured_command
        or (shlex.join([str(item) for item in raw_command]) if isinstance(raw_command, list) else str(raw_command or uta_settings.ci_enforcement_command))
    )
    runner = MavenEnforcementRunner(
        command=command,
        timeout_seconds=int(uta_settings.ci_enforcement_timeout_seconds or 1800),
        run_command=run_command,
    )
    try:
        result = runner.run(Path(repo_path))
    except Exception as exc:
        result = EnforcementResult(
            status=EnforcementResultStatus.command_error,
            passed=False,
            command=shlex.split(command),
            stderr=str(exc),
            summary="test-enforcement command failed to start",
        )
    return result.model_dump(mode="json")


def _enforcement_evidence_detail(result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from uta.ci_plugin.reporting import CiReportRenderer

        return CiReportRenderer._evidence_detail(result)
    except Exception:
        logger.debug("Failed to normalize enforcement evidence", exc_info=True)
        return {"coverage": None, "mutation": None, "pitMutation": None}


def _delegated_gate_batch_results(
    *,
    state: Dict[str, Any],
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    gate_result: Dict[str, Any],
    gate_seconds: float,
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    precheck_existing_tests: bool = False,
) -> Dict[str, Dict[str, Any]]:
    evidence = _enforcement_evidence_detail(gate_result)
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
    mutation = evidence.get("mutation") if isinstance(evidence.get("mutation"), dict) else {}
    if not mutation and isinstance(evidence.get("pitMutation"), dict):
        mutation = evidence.get("pitMutation") or {}
    gate_output = _delegated_quality_gate_feedback(gate_result)
    batch_results = state.get("results", {}).copy()
    resolved_status = status or ("PASS" if gate_result.get("passed") else "FAIL")
    for class_fqn in batch:
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        test_file_abs = Path(repo_path) / test_file_rel
        try:
            test_file_content = test_file_abs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            test_file_content = ""
        batch_results[class_fqn] = {
            "status": resolved_status,
            "coverage": coverage.get("rate") if coverage else None,
            "tests_pass": bool(gate_result.get("passed")),
            "mutation_score": mutation.get("rate") if mutation else None,
            "surviving_mutants": int(mutation.get("survived") or 0) if mutation else 0,
            "total_mutants": int(mutation.get("generated") or 0) if mutation else 0,
            "killed_mutants": int(mutation.get("killed") or 0) if mutation else 0,
            "output": gate_output[:2000],
            "test_file_path": test_file_rel,
            "test_file_content": test_file_content,
            "elapsed_seconds": gate_seconds,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": 0.0,
            "mutation_seconds": gate_seconds,
            "session_id": session_id,
            "session_ids": list(session_ids or []),
            "delegated_quality_gate": gate_result,
            "precheck_existing_tests": precheck_existing_tests,
        }
    return batch_results


def _delegated_quality_gate_feedback(result: Dict[str, Any], max_chars: int = 6000) -> str:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    combined = (stdout + "\n" + stderr).strip()
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
    command = " ".join(str(item) for item in result.get("command") or [])
    return (
        f"Summary: {result.get('summary') or ''}\n"
        f"Status: {result.get('status') or ''}\n"
        f"Command: {command}\n\n"
        f"Output:\n{combined}"
    ).strip()


def _delegated_gate_failure_stage(result: Dict[str, Any]) -> str:
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    if "mutation" in output or "test-strength" in output or "check-mutation" in output:
        return "mutation_fix"
    return "coverage_fix"


def _run_delegated_quality_gate_fix_loop(
    *,
    state: Dict[str, Any],
    repo_path: str,
    batch: List[str],
    client: "OpenCodeClient",
    generation_session_id: str,
    max_fix_attempts: int = 3,
    initial_result: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Dict[str, Any], float, List[str]]:
    started = time.perf_counter()
    session_ids: List[str] = []
    last_result: Dict[str, Any] = dict(initial_result or {})
    for attempt in range(1, max(1, max_fix_attempts) + 1):
        if initial_result is None or attempt > 1:
            stage = _delegated_gate_failure_stage(last_result) if last_result else "coverage_fix"
            _set_stage(state, stage, f"delegated_gate attempt={attempt} batch={len(batch)}", class_fqns=batch)
            last_result = _run_delegated_quality_gate_once(state, repo_path)
        else:
            stage = _delegated_gate_failure_stage(last_result)
            _set_stage(state, stage, f"delegated_gate precheck_failed batch={len(batch)}", class_fqns=batch)
        if last_result.get("passed"):
            return True, last_result, time.perf_counter() - started, session_ids
        if attempt >= max(1, max_fix_attempts):
            break

        fix_session_id = _create_phase_session(
            state=state,
            client=client,
            session_ids=session_ids,
            model_id=uta_settings.opencode_model,
        )
        stage = _delegated_gate_failure_stage(last_result)
        progress = _session_progress_logger(batch, session_id=fix_session_id, stage=stage)
        feedback = _delegated_quality_gate_feedback(last_result)
        prompt = (
            "The delegated quality gate is still failing after the generated unit tests.\n\n"
            "For this task, the authoritative coverage/mutation checker is the configured "
            "Maven test-enforcement command.\n\n"
            f"Target batch: {', '.join(batch)}\n"
            f"Previous generation session: `{generation_session_id}`\n\n"
            f"### TEST-ENFORCEMENT FEEDBACK\n```\n{feedback}\n```\n\n"
            "Fix or improve the generated unit tests so the diff coverage and diff mutation gates pass. "
            "Prefer focused assertions for the changed behavior. Do not modify production code unless the "
            "failure proves the production branch itself is broken."
        )
        prompt += _stage_introspect_section(repo_path, stage)
        _set_stage(state, stage, f"delegated_gate_fix attempt={attempt} session={fix_session_id}", class_fqns=batch)
        client.send_message(fix_session_id, prompt, model_id=uta_settings.opencode_model)
        _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=client,
                session_id=fix_session_id,
                timeout=_llm_repair_timeout(uta_settings.opencode_model),
                phase=stage,
                batch=batch,
                on_update=progress if uta_settings.opencode_stream_progress else None,
                state=state,
            ),
            session_id=fix_session_id,
            client=client,
            phase=stage,
        )
    return False, last_result, time.perf_counter() - started, session_ids


def _mutation_enhancement_attempts() -> int:
    return max(1, int(uta_settings.mutation_enhancement_attempts or 1))


def _set_stage(
    state: Dict[str, Any],
    stage: str,
    detail: Optional[str] = None,
    *,
    class_fqns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    message = f"[stage] {stage}"
    if detail:
        message += f" — {detail}"
    logger.info(message)
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager

            manager = TaskManager(task_db_path)
            stop_reason = manager.check_stop_requested(int(task_id))
            if stop_reason:
                manager.mark_stopped(int(task_id), reason=stop_reason, stage=stage)
                raise TaskStopRequested(stop_reason)
            stage_class_fqns: List[str] = []
            if class_fqns is None:
                current_batch = state.get("current_batch") or []
                if isinstance(current_batch, list):
                    stage_class_fqns.extend(str(item) for item in current_batch if item)
                current_class = state.get("current_class")
                if current_class:
                    stage_class_fqns.append(str(current_class))
            else:
                stage_class_fqns.extend(str(item) for item in class_fqns if item)
            prev_stage = state.get("current_stage")
            if prev_stage and prev_stage != stage:
                manager.record_stage_completed(
                    int(task_id),
                    prev_stage,
                    class_fqns=list(dict.fromkeys(stage_class_fqns)),
                )
            manager.record_stage(
                int(task_id),
                stage,
                detail=detail,
                class_fqns=list(dict.fromkeys(stage_class_fqns)),
            )
        except TaskStopRequested:
            raise
        except Exception:
            logger.debug("Failed to record production task stage", exc_info=True)
    return {"current_stage": stage}


def _task_fqns_for_guard(state: Dict[str, Any], batch: Optional[List[str]] = None) -> List[str]:
    fqns: List[str] = []
    if batch:
        fqns.extend(batch)
    current_batch = state.get("current_batch") or []
    if isinstance(current_batch, list):
        fqns.extend(str(item) for item in current_batch if item)
    current_class = state.get("current_class")
    if current_class:
        fqns.append(str(current_class))
    return list(dict.fromkeys(fqns))


def _java_target_selection(class_fqn: str) -> Dict[str, Any]:
    return TargetIdentity.java_class(class_fqn).as_selection()


def _target_alias_update(batch: List[str]) -> Dict[str, Any]:
    target_batch = [_java_target_selection(fqn) for fqn in batch]
    return {
        "current_target_batch": target_batch,
        "current_target": target_batch[0] if target_batch else None,
    }


class _JavaWorkflowContextProvider:
    language = "java"

    def __init__(self, repo_path: str, graph: Any, flows: List[Any]):
        self.builder = ContextBuilder(repo_path, graph, flows)

    def export_project_context(self):
        return self.builder.export_context_files()

    def export_target_context(self, target, **kwargs):
        return self.builder.export_target_context_files(target.target_id, **kwargs)


def _git_status_paths(repo_path: str) -> set[str]:
    result = _git_run(
        repo_path,
        "status",
        "--porcelain",
        "--untracked-files=all",
        capture_output=True,
        check=False,
        text=True,
    )
    paths: set[str] = set()
    if result.returncode != 0:
        return paths
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.add(path)
    return paths


def _git_status_snapshot(repo_path: str) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for rel_path in _git_status_paths(repo_path):
        path = Path(repo_path) / rel_path
        if not path.exists():
            snapshot[rel_path] = "<missing>"
            continue
        if path.is_dir():
            snapshot[rel_path] = "<dir>"
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "<unreadable>"
        snapshot[rel_path] = digest
    return snapshot


def _allowed_llm_path(path: str, state: Dict[str, Any], batch: List[str]) -> bool:
    normalized = path.replace("\\", "/")
    if (
        normalized == "opencode.json"
        or normalized == ".coverage"
        or normalized.startswith(".coverage.")
        or normalized.startswith(".mutmut-cache/")
        or normalized.startswith(".pytest_cache/")
        or normalized == "mutants"
        or normalized.startswith("mutants/")
        or normalized == ".uta_summary.md"
        or normalized.startswith(".sisyphus/")
        or normalized.startswith(".uta_cache/")
        or normalized.startswith(".uta_reports/")
    ):
        return True
    if "/src/test/resources/" in normalized:
        return True
    if "/src/test/" in normalized and normalized.endswith(".java"):
        return True
    if normalized.startswith("tests/uta_generated/") and normalized.endswith(".py"):
        return True
    module = state.get("module")
    for class_fqn in _task_fqns_for_guard(state, batch):
        expected = _expected_test_file_rel(module, class_fqn).replace("\\", "/")
        if normalized == expected:
            return True
        if not module:
            unqualified = _expected_test_file_rel(None, class_fqn).replace("\\", "/")
            if normalized.endswith(f"/{unqualified}"):
                return True
    return False


def _allowed_preexisting_dirty_path(path: str, state: Dict[str, Any], batch: List[str]) -> bool:
    """Allow bounded UTA-owned setup residue while still blocking source edits."""
    normalized = path.replace("\\", "/")
    if _allowed_llm_path(normalized, state, batch):
        return True
    if normalized in set(state.get("deterministic_change_paths") or []):
        return True
    if normalized in {"AGENTS.md", "CLAUDE.md"} or normalized.startswith(".opencode/"):
        return True
    if normalized == "pom.xml" or normalized.endswith("/pom.xml"):
        return True
    if "/src/test/resources/" in normalized:
        return True
    if "/src/test/" in normalized and normalized.endswith(".java"):
        return True
    return False


def _llm_guard_before(state: Optional[Dict[str, Any]], batch: List[str], phase: str) -> Optional[Dict[str, Any]]:
    if not state or not state.get("task_id") or not state.get("task_db_path"):
        return None
    repo_path = state.get("repo_path")
    if not repo_path:
        return None
    task_id = int(state["task_id"])
    from uta.tasks.manager import TaskManager

    manager = TaskManager(state["task_db_path"])
    stop_reason = manager.check_stop_requested(task_id)
    if stop_reason:
        manager.mark_stopped(task_id, reason=stop_reason, stage=phase)
        raise TaskStopRequested(stop_reason)
    task = manager.db.get_repo_task(task_id)
    turn_limit = None
    try:
        from uta.tasks.models import json_loads

        budget_snapshot = json_loads(task["budget_config_snapshot_json"] if task else None)
        config_snapshot = json_loads(task["config_snapshot_json"] if task else None)
        turn_limit = (
            budget_snapshot.get("max_llm_turns_per_class")
            or config_snapshot.get("max_llm_turns_per_class")
            or os.environ.get("UTA_MAX_LLM_TURNS_PER_CLASS")
        )
        turn_limit = int(turn_limit) if turn_limit is not None else None
    except Exception:
        turn_limit = None
    for class_fqn in _task_fqns_for_guard(state, batch):
        row = manager.db.find_class_task(task_id, class_fqn)
        if not row:
            continue
        estimated_class_cost = row["estimated_cost_usd"] or row["estimated_cost"] or None
        current_class_cost = row["provider_cost_usd"] or row["actual_cost"] or 0.0
        if estimated_class_cost:
            class_hard_cap = max(float(estimated_class_cost) * 3.0, 2.0)
            if float(current_class_cost or 0.0) >= class_hard_cap:
                message = (
                    f"Class budget hard cap exceeded before {phase} for {class_fqn}: "
                    f"${float(current_class_cost):.4f} >= ${class_hard_cap:.4f}"
                )
                manager.db.update_class_task(row["id"], status="BUDGET_EXCEEDED", error=message, last_error=message)
                manager.db.add_event(task_id, row["id"], "budget_blocked", message, stage=phase, severity="ERROR")
                raise TaskBudgetExceeded(message)
        next_turn_count = int(row["llm_turn_count"] or 0) + 1
        if turn_limit and next_turn_count > turn_limit:
            message = f"LLM turn hard cap exceeded for {class_fqn}: {next_turn_count}>{turn_limit}"
            manager.db.update_class_task(row["id"], status="BUDGET_EXCEEDED", error=message, last_error=message)
            manager.mark_failed(task_id, message, stage="budget_blocked")
            manager.db.add_event(task_id, row["id"], "budget_blocked", message, stage=phase, severity="ERROR")
            raise TaskBudgetExceeded(message)
        manager.db.update_class_task(row["id"], llm_turn_count=next_turn_count)
    if task:
        current_cost = float(task["provider_cost_usd"] or task["actual_cost"] or 0.0)
        # Explicit hard cap set at enqueue time takes priority
        explicit_cap = task["hard_cap_usd"] if "hard_cap_usd" in task.keys() else None
        if explicit_cap and float(explicit_cap) > 0 and current_cost >= float(explicit_cap):
            message = f"Hard cap exceeded before {phase}: ${current_cost:.4f} >= ${float(explicit_cap):.4f}"
            for class_fqn in _task_fqns_for_guard(state, batch):
                row = manager.db.find_class_task(task_id, class_fqn)
                if row:
                    manager.db.update_class_task(row["id"], status="BUDGET_EXCEEDED", error=message, last_error=message)
            manager.mark_budget_exceeded(task_id, message)
            manager.db.add_event(task_id, None, "budget_blocked", message, stage=phase, severity="ERROR")
            raise TaskBudgetExceeded(message)
    if task and task["estimated_cost_usd"]:
        current_cost = float(task["provider_cost_usd"] or task["actual_cost"] or 0.0)
        hard_cap = float(task["estimated_cost_usd"]) * 2.0
        if current_cost >= hard_cap and hard_cap > 0:
            message = f"Budget hard cap exceeded before {phase}: ${current_cost:.4f} >= ${hard_cap:.4f}"
            for class_fqn in _task_fqns_for_guard(state, batch):
                row = manager.db.find_class_task(task_id, class_fqn)
                if row:
                    manager.db.update_class_task(row["id"], status="BUDGET_EXCEEDED", error=message, last_error=message)
            manager.mark_failed(task_id, message, stage="budget_blocked")
            manager.db.add_event(task_id, None, "budget_blocked", message, stage=phase, severity="ERROR")
            raise TaskBudgetExceeded(message)
        for threshold in (0.5, 0.75, 0.9):
            if current_cost >= hard_cap * threshold:
                manager.db.add_event(
                    task_id,
                    None,
                    "budget_warning",
                    f"Budget {threshold:.0%} warning before {phase}: ${current_cost:.4f} of ${hard_cap:.4f}",
                    stage=phase,
                    severity="WARNING",
                )
                break
    manager.db.add_event(task_id, None, "llm_progress", f"Starting LLM phase {phase}", stage=phase, payload={"batch": batch})
    return {
        "repo_path": repo_path,
        "before": _git_status_snapshot(repo_path),
        "phase": phase,
        "batch": list(batch or []),
    }


def _llm_guard_after(state: Optional[Dict[str, Any]], snapshot: Optional[Dict[str, Any]]) -> None:
    if not state or not snapshot or not state.get("task_id") or not state.get("task_db_path"):
        return
    repo_path = snapshot["repo_path"]
    before = dict(snapshot.get("before") or {})
    after = _git_status_snapshot(repo_path)
    changed_paths = {
        path
        for path in set(before.keys()) | set(after.keys())
        if before.get(path) != after.get(path)
    }
    new_paths = sorted(changed_paths)
    unsafe = [
        path for path in new_paths
        if not _allowed_llm_path(path, state, list(snapshot.get("batch") or []))
    ]
    if not unsafe:
        return
    from uta.tasks.manager import TaskManager

    task_id = int(state["task_id"])
    manager = TaskManager(state["task_db_path"])
    class_fqns = _task_fqns_for_guard(state, list(snapshot.get("batch") or []))
    for class_fqn in class_fqns:
        row = manager.db.find_class_task(task_id, class_fqn)
        if row:
            manager.db.update_class_task(
                row["id"],
                status="UNSAFE_DIFF",
                current_stage=snapshot.get("phase"),
                stage=snapshot.get("phase"),
                error="Unsafe LLM-authored paths: " + ", ".join(unsafe),
                last_error="Unsafe LLM-authored paths: " + ", ".join(unsafe),
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
    manager.db.add_event(
        task_id,
        None,
        "unsafe_diff",
        "Unsafe LLM-authored paths: " + ", ".join(unsafe),
        stage=snapshot.get("phase"),
        severity="ERROR",
        payload={"paths": unsafe},
    )
    raise TaskUnsafeDiffError("Unsafe LLM-authored paths: " + ", ".join(unsafe))


def _verify_task_branch_and_preexisting_diff(state: Dict[str, Any], batch: List[str]) -> None:
    if not state.get("task_id") or not state.get("task_db_path"):
        return
    repo_path = state.get("repo_path")
    expected_branch = state.get("branch_name")
    if not repo_path:
        return
    branch_result = _git_run(
        repo_path,
        "branch",
        "--show-current",
        capture_output=True,
        check=False,
        text=True,
    )
    if branch_result.returncode == 0 and expected_branch:
        current_branch = branch_result.stdout.strip()
        if current_branch and current_branch != expected_branch:
            message = f"Production task branch mismatch: expected={expected_branch} current={current_branch}"
            from uta.tasks.manager import TaskManager

            manager = TaskManager(state["task_db_path"])
            manager.mark_failed(int(state["task_id"]), message, stage="branch_safety")
            raise TaskUnsafeDiffError(message)

    dirty_paths = sorted(_git_status_paths(repo_path))
    unsafe = [
        path for path in dirty_paths
        if not _allowed_preexisting_dirty_path(path, state, batch)
    ]
    if unsafe:
        message = "Pre-existing unsafe dirty paths before class run: " + ", ".join(unsafe)
        from uta.tasks.manager import TaskManager

        task_id = int(state["task_id"])
        manager = TaskManager(state["task_db_path"])
        for class_fqn in _task_fqns_for_guard(state, batch):
            row = manager.db.find_class_task(task_id, class_fqn)
            if row:
                manager.db.update_class_task(row["id"], status="UNSAFE_DIFF", error=message, last_error=message)
        manager.mark_failed(task_id, message, stage="branch_safety")
        manager.db.add_event(task_id, None, "unsafe_diff", message, stage="branch_safety", severity="ERROR")
        raise TaskUnsafeDiffError(message)


def _should_stop_after(state: Dict[str, Any], stage: str) -> bool:
    target = (state.get("stop_after_stage") or "").strip()
    return bool(target and target == stage)


def _index_query_command(module: Optional[str], *, section: Optional[str] = None) -> str:
    cmd = [str((Path(__file__).resolve().parents[2] / "bin" / "uta-query-index").resolve())]
    if module:
        cmd.extend(["--module", module])
    if section:
        cmd.extend(["--section", section])
    return " ".join(shlex.quote(part) for part in cmd)


def _session_progress_logger(batch: List[str], session_id: Optional[str] = None, stage: Optional[str] = None):
    prefix = ",".join(fqn.split(".")[-1] for fqn in batch[:3])
    if len(batch) > 3:
        prefix += ",..."
    session_part = f" session={session_id}" if session_id else ""
    stage_part = f" stage={stage}" if stage else ""

    def _log(line: str) -> None:
        logger.info("[opencode%s%s %s] %s", stage_part, session_part, prefix, line)

    return _log


def _cleanup_focused_session(session_client, session_id: Optional[str]) -> None:
    if not session_id or uta_settings.opencode_preserve_focused_sessions:
        return
    try:
        session_client.delete_session(session_id)
    except Exception:
        pass


def _append_session_id(state: Dict[str, Any], session_ids: List[str], session_id: Optional[str]) -> List[str]:
    if not session_id:
        return session_ids
    if session_id not in session_ids:
        session_ids.append(session_id)
    if "session_ids" not in state:
        state["session_ids"] = []
    if session_id not in state["session_ids"]:
        state["session_ids"].append(session_id)
    return session_ids


def _create_phase_session(
    *,
    state: Dict[str, Any],
    client: Any,
    session_ids: List[str],
    model_id: str,
    permission: Optional[List[Dict[str, Any]]] = None,
) -> str:
    try:
        session_id = client.create_session(model_id=model_id, permission=permission)
    except TypeError:
        session_id = client.create_session(model_id=model_id)
    _append_session_id(state, session_ids, session_id)
    return session_id


def _sum_token_bucket(target: Dict[str, int], source: Dict[str, Any]) -> None:
    target["input"] += int(source.get("input", 0) or 0)
    target["output"] += int(source.get("output", 0) or 0)
    target["reasoning"] += int(source.get("reasoning", 0) or 0)
    target["cache_read"] += int(source.get("cache_read", 0) or 0)
    target["cache_write"] += int(source.get("cache_write", 0) or 0)
    target["total"] += int(source.get("total", 0) or 0)


def _empty_token_bucket() -> Dict[str, int]:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total": 0,
    }


def _append_phase_session_id(
    phase_session_ids: Dict[str, List[str]],
    phase: str,
    session_id: Optional[str],
) -> None:
    if not session_id:
        return
    target = phase_session_ids.setdefault(phase, [])
    if session_id not in target:
        target.append(session_id)


def _source_complexity_summary(source_path: str, coverage_gate: int) -> Dict[str, Any]:
    summary = {
        "source_path": source_path,
        "line_count": 0,
        "public_method_count": 0,
        "strict_coverage": False,
    }
    try:
        text = Path(source_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return summary

    summary["line_count"] = len(text.splitlines())
    summary["public_method_count"] = len(
        re.findall(r"^\s*public\s+(?!class\b|interface\b|enum\b|@interface\b)[^{;\n]*\(", text, flags=re.MULTILINE)
    )
    summary["strict_coverage"] = bool(
        coverage_gate >= 70
        and (summary["line_count"] >= 250 or summary["public_method_count"] >= 8)
    )
    return summary


def _candidate_source_path(state: Dict[str, Any], class_fqn: str) -> str:
    graph = state.get("graph")
    node = None
    if graph is not None and hasattr(graph, "nodes"):
        node = graph.nodes.get(class_fqn)
    if node is not None and getattr(node, "file_path", None):
        return str(node.file_path)
    repo_path = Path(str(state.get("repo_path") or ""))
    module = state.get("module")
    base = repo_path / module if module else repo_path
    return str(base / "src" / "main" / "java" / Path(*class_fqn.split(".")).with_suffix(".java"))


def _module_from_source_path(repo_path: str, source_path: str) -> Optional[str]:
    try:
        rel = Path(source_path).resolve().relative_to(Path(repo_path).resolve())
    except Exception:
        rel = Path(source_path)
    parts = rel.parts
    for index in range(0, max(0, len(parts) - 3)):
        if parts[index:index + 3] == ("src", "main", "java"):
            module_parts = parts[:index]
            return "/".join(module_parts) if module_parts else None
    return None


def _class_module(state: Dict[str, Any], class_fqn: str) -> Optional[str]:
    configured_module = state.get("module_filter") if "module_filter" in state else state.get("module")
    if configured_module:
        return configured_module
    source_path = _candidate_source_path(state, class_fqn)
    repo_path = str(state.get("repo_path") or "")
    return _module_from_source_path(repo_path, source_path)


def _batch_complexity_profile(state: Dict[str, Any], class_fqn: str) -> Dict[str, Any]:
    meta = _source_complexity_summary(
        _candidate_source_path(state, class_fqn),
        int(state.get("coverage_gate") or uta_settings.coverage_gate),
    )
    line_count = int(meta.get("line_count") or 0)
    public_methods = int(meta.get("public_method_count") or 0)
    is_complex = (
        line_count <= 0
        or line_count >= int(uta_settings.smart_complex_line_threshold or 100)
        or public_methods >= int(uta_settings.smart_complex_public_method_threshold or 4)
    )
    return {
        **meta,
        "batch_kind": "complex" if is_complex else "simple",
        "is_complex": is_complex,
    }


def _select_smart_batch(state: Dict[str, Any], remaining: List[str], requested_cap: int) -> List[str]:
    if not remaining:
        return []
    smart_batch_context = (
        bool(state.get("production"))
        or state.get("quality_gate_backend") == "maven_enforcer"
        or state.get("quality_mode") == "ci_incremental"
    )
    if not smart_batch_context or not bool(uta_settings.smart_batching_enabled):
        return remaining[: max(1, requested_cap)]

    first = remaining[0]
    first_module = _class_module(state, first)
    first_profile = _batch_complexity_profile(state, first)
    if first_profile["is_complex"]:
        logger.info(
            "Smart batch selected single complex class: %s lines=%s public_methods=%s",
            first,
            first_profile.get("line_count"),
            first_profile.get("public_method_count"),
        )
        return [first]

    cap = requested_cap if requested_cap > 1 else int(uta_settings.smart_simple_batch_size or 3)
    cap = max(1, min(3, cap))
    batch = [first]
    profiles = {first: first_profile}
    for class_fqn in remaining[1:]:
        if len(batch) >= cap:
            break
        if _class_module(state, class_fqn) != first_module:
            break
        profile = _batch_complexity_profile(state, class_fqn)
        profiles[class_fqn] = profile
        if profile["is_complex"]:
            break
        batch.append(class_fqn)
    logger.info(
        "Smart batch selected %d simple class(es): %s profiles=%s",
        len(batch),
        batch,
        {
            class_fqn: {
                "lines": profile.get("line_count"),
                "public_methods": profile.get("public_method_count"),
                "kind": profile.get("batch_kind"),
            }
            for class_fqn, profile in profiles.items()
        },
    )
    return batch


def _plan_needs_stricter_replan(plan_text: str, strict_classes: List[Dict[str, Any]]) -> bool:
    if not strict_classes:
        return False
    lowered = plan_text.lower()
    required_markers = [
        "methods required for gate",
        "estimated reach",
    ]
    if any(marker not in lowered for marker in required_markers):
        return True
    weak_markers = [
        "do not chase class-wide completeness",
        "defer heavier branches",
        "high-value public methods first",
        "fall back to",
    ]
    return any(marker in lowered for marker in weak_markers)


def _plan_breadth_replan_reason(class_fqn: str, breadth: Any) -> Optional[str]:
    """Return a replan reason only for fatal breadth issues.

    OVER means the plan is noisy, but it can still be a usable generation plan
    when feasibility passes. Treating OVER as fatal caused valid plans to be
    discarded and replaced by long replanning turns.
    """
    verdict = getattr(getattr(breadth, "verdict", None), "value", getattr(breadth, "verdict", None))
    if verdict == "UNDER":
        return f"[{class_fqn}] {breadth.message}"
    return None


def _continue_prompt_for_phase(phase: str) -> str:
    prompts = {
        "plan": (
            "Resume the interrupted planning work in this live session. "
            "Do NOT restart broad exploration from scratch. Continue the planning document only, "
            "editing the existing `latest_generation_plan.md` content instead of starting a new plan, "
            "reusing what you already learned in this session about public methods, branch axes, "
            "style references, safe mocking choices, and compile-critical facts."
        ),
        "generate": (
            "Resume the interrupted generation work in this live session. "
            "Do NOT restart broad exploration. Reuse the approved plan, current session context, "
            "and any file edits already made. Continue writing or validating the required test files "
            "from the current state."
        ),
        "compile_fix": (
            "Resume the interrupted compile-fix work in this live session. "
            "Do NOT restart exploration. Continue fixing only the current compile errors in the existing test files."
        ),
        "coverage_fix": (
            "Resume the interrupted coverage-hardening work in this live session. "
            "Do NOT restart exploration. Continue expanding path reach for the current class only, "
            "using the existing test file and latest coverage goal."
        ),
        "mutation_fix": (
            "Resume the interrupted mutation-hardening work in this live session. "
            "Do NOT restart exploration. Continue improving the current tests against the latest mutation findings only."
        ),
        "test_fix": (
            "Resume the interrupted test-fix work in this live session. "
            "Do NOT restart exploration. Continue fixing the current test execution failures in the existing file."
        ),
    }
    return prompts.get(
        phase,
        "Resume the interrupted work in this live session. Do NOT restart broad exploration. Continue from the current session state and existing file edits.",
    )


def _prepare_continue_artifact_for_phase(*, repo_path: Optional[str], phase: str) -> None:
    if phase != "plan" or not repo_path:
        return

    plan_path = _generation_plan_path(repo_path)
    candidate_path = _generation_plan_candidate_path(repo_path)
    if plan_path.exists() or not candidate_path.exists():
        return

    try:
        content = candidate_path.read_text(encoding="utf-8", errors="replace")
        plan_text = _extract_plan_body_from_artifact(content)
        session_id = _generation_plan_artifact_session_id(content) or "resume-plan"
        classes = _generation_plan_artifact_classes(content)
        _write_generation_plan(repo_path, session_id, classes, plan_text)
        logger.info(
            "Materialized candidate generation plan into latest_generation_plan.md for planning resume"
        )
    except Exception:
        logger.warning(
            "Failed to materialize candidate generation plan before planning resume",
            exc_info=True,
        )


def _detect_provider_limit_after_event(client: Any, session_id: str, event: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if event and event.get("type") == "rate_limited":
        return event.get("rate_limit") or client.detect_rate_limit_issue(session_id)
    detect_rate_limit = getattr(client, "detect_rate_limit_issue", None)
    if callable(detect_rate_limit):
        return detect_rate_limit(session_id)
    return None


def _rate_limited_result(
    *,
    state: Dict[str, Any],
    batch: List[str],
    session_id: str,
    generation_started_at: float,
    generation_finished_at: float,
    generation_seconds: float,
    generate_validate_seconds: float,
    repo_path: str,
    module: Optional[str],
    client: Any,
) -> Dict[str, Any]:
    session_ids = list(state.get("session_ids", []) or [])
    if session_id and session_id not in session_ids:
        session_ids.append(session_id)
    session_retrospect = _capture_session_retrospect(
        state=state,
        repo_path=repo_path,
        client=client,
        session_ids=session_ids,
    )
    new_results = state["results"].copy()
    rate_limit = client.detect_rate_limit_issue(session_id) or {}
    retry_after = rate_limit.get("retry_after_seconds")
    provider = rate_limit.get("provider_id") or "provider"
    model = rate_limit.get("model_id") or "model"
    message = rate_limit.get("message") or "Provider/model rate limit reached"
    output = f"{provider}/{model}: {message}"
    if retry_after:
        output += f" (retry after {retry_after}s)"
    for class_fqn in batch:
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        new_results[class_fqn] = {
            "status": "PROVIDER_RATE_LIMITED",
            "coverage": 0.0,
            "tests_pass": False,
            "mutation_score": 0.0,
            "surviving_mutants": 0,
            "output": output,
            "test_file_path": test_file_rel,
            "elapsed_seconds": generation_seconds,
            "generation_seconds": generation_seconds,
            "compile_seconds": 0.0,
            "test_seconds": 0.0,
            "mutation_seconds": 0.0,
            "session_id": session_id,
            "session_ids": list(session_ids),
            "generation_started_at": generation_started_at,
            "generation_finished_at": generation_finished_at,
            "rate_limit": rate_limit,
        }
    return {
        "results": new_results,
        "finished": True,
        "stopped_early": True,
        "current_batch": [],
        "current_class": None,
        "current_stage": "generate_prompt",
        "session_retrospect": session_retrospect,
        "session_token_usage": _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=session_ids,
        ),
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=generate_validate_seconds,
            generation_session_seconds=generation_seconds,
        ),
    }


def _event_error_message(event: Optional[Dict[str, Any]]) -> str:
    if not event:
        return "Provider/model returned an error"
    error = event.get("error") or {}
    data = error.get("data") or {}
    message = data.get("message") or error.get("message") or event.get("reason")
    return str(message or "Provider/model returned an error")


def _provider_error_result(
    *,
    state: Dict[str, Any],
    batch: List[str],
    session_id: str,
    event: Optional[Dict[str, Any]],
    stage: str,
    generate_validate_seconds: float,
    repo_path: str,
    module: Optional[str],
    client: Any,
) -> Dict[str, Any]:
    session_ids = list(state.get("session_ids", []) or [])
    if session_id and session_id not in session_ids:
        session_ids.append(session_id)
    session_retrospect = _capture_session_retrospect(
        state=state,
        repo_path=repo_path,
        client=client,
        session_ids=session_ids,
    )
    now = time.time()
    message = _event_error_message(event)
    new_results = state["results"].copy()
    for class_fqn in batch:
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        new_results[class_fqn] = {
            "status": "PROVIDER_ERROR",
            "coverage": 0.0,
            "tests_pass": False,
            "mutation_score": 0.0,
            "surviving_mutants": 0,
            "output": f"{stage} provider/model error: {message}",
            "test_file_path": test_file_rel,
            "elapsed_seconds": 0.0,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": 0.0,
            "mutation_seconds": 0.0,
            "session_id": session_id,
            "session_ids": list(session_ids),
            "generation_started_at": now,
            "generation_finished_at": now,
            "provider_error": event.get("error") if event else {},
        }
    return {
        "results": new_results,
        "current_class": None,
        "current_stage": stage,
        "finished": True,
        "stopped_early": True,
        "session_retrospect": session_retrospect,
        "session_token_usage": _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=session_ids,
        ),
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=generate_validate_seconds,
            generation_session_seconds=0.0,
        ),
    }


def _planning_timeout_result(
    *,
    state: Dict[str, Any],
    batch: List[str],
    session_id: str,
    planning_timeout: int,
    generate_validate_seconds: float,
    repo_path: str,
    module: Optional[str],
    client: Any,
) -> Dict[str, Any]:
    session_ids = list(state.get("session_ids", []) or [])
    if session_id and session_id not in session_ids:
        session_ids.append(session_id)
    session_retrospect = _capture_session_retrospect(
        state=state,
        repo_path=repo_path,
        client=client,
        session_ids=session_ids,
    )
    now = time.time()
    new_results = state["results"].copy()
    output = f"OpenCode planning timed out after {planning_timeout}s before emitting a final plan"
    for class_fqn in batch:
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        new_results[class_fqn] = {
            "status": "PLANNING_TIMEOUT",
            "coverage": 0.0,
            "tests_pass": False,
            "mutation_score": 0.0,
            "surviving_mutants": 0,
            "output": output,
            "test_file_path": test_file_rel,
            "elapsed_seconds": 0.0,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": 0.0,
            "mutation_seconds": 0.0,
            "session_id": session_id,
            "session_ids": list(session_ids),
            "generation_started_at": now,
            "generation_finished_at": now,
        }
    return {
        "results": new_results,
        "current_class": None,
        "current_stage": "plan_tests",
        "session_retrospect": session_retrospect,
        "session_token_usage": _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=session_ids,
        ),
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=generate_validate_seconds,
            generation_session_seconds=0.0,
        ),
    }


def _raise_for_rate_limit_event(
    *,
    event: Dict[str, Any],
    session_id: str,
    client: Any,
    phase: str,
) -> Dict[str, Any]:
    return raise_for_provider_fallback_event(
        event=event,
        session_id=session_id,
        client=client,
        phase=phase,
    )


def _event_needs_fresh_session(event: Dict[str, Any]) -> bool:
    return event.get("type") in {"timeout", "stalled_after_recovery", "stalled_no_progress"}


def _is_deepseek_model(model_id: Optional[str]) -> bool:
    model = (model_id or "").lower()
    provider = (uta_settings.opencode_provider or "").lower()
    return model.startswith("deepseek/") or provider == "deepseek"


def _llm_timeout(base_seconds: int, model_id: Optional[str] = None) -> int:
    base = max(1, int(base_seconds))
    multiplier = float(uta_settings.opencode_timeout_multiplier or 1.0)
    if _is_deepseek_model(model_id):
        multiplier *= float(uta_settings.opencode_deepseek_timeout_multiplier or 1.0)
    return max(1, int(base * multiplier))


def _llm_compile_fix_timeout(model_id: Optional[str] = None) -> int:
    base = max(120, int(uta_settings.opencode_compile_fix_timeout_seconds or 600))
    return _llm_timeout(base, model_id or uta_settings.opencode_model)


def _llm_repair_timeout(model_id: Optional[str] = None) -> int:
    base = max(120, int(uta_settings.opencode_repair_timeout_seconds or 900))
    return _llm_timeout(base, model_id or uta_settings.opencode_model)


def _batch_complexity_multiplier(complexity_by_class: Dict[str, Dict[str, Any]]) -> float:
    """Return 1.5 if any class in the batch is complex (>=100 source lines or >=4 public methods)."""
    for meta in complexity_by_class.values():
        if int(meta.get("line_count") or 0) >= 100 or int(meta.get("public_method_count") or 0) >= 4:
            return 1.5
    return 1.0


def _llm_stalled_no_progress_seconds() -> int:
    return max(
        60,
        int(
            uta_settings.opencode_stalled_no_progress_seconds
            or uta_settings.opencode_stream_idle_timeout_seconds
            or 900
        ),
    )


def _log_nonterminal_event(*, phase: str, session_id: str, event: Dict[str, Any], class_fqn: Optional[str] = None) -> None:
    if not _event_needs_fresh_session(event):
        return
    prefix = f"[{class_fqn}] " if class_fqn else ""
    reason = event.get("reason") or "no terminal completion"
    logger.warning(
        "%s%s session %s ended with %s (%s)",
        prefix,
        phase,
        session_id,
        event.get("type"),
        reason,
    )


def _poll_with_continue_recovery(
    *,
    client: Any,
    session_id: str,
    timeout: int,
    phase: str,
    batch: List[str],
    repo_path: Optional[str] = None,
    model_id: Optional[str] = None,
    on_update=None,
    stalled_no_progress_seconds: Optional[int] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = _llm_guard_before(state, batch, phase)
    try:
        event = client.poll_completion(
            session_id,
            timeout=timeout,
            on_update=on_update,
            **({"stalled_no_progress_seconds": stalled_no_progress_seconds} if stalled_no_progress_seconds is not None else {}),
        )
    finally:
        _llm_guard_after(state, snapshot)
    if event.get("type") not in {"stalled_after_recovery", "stalled_no_progress"}:
        return event

    reason = event.get("reason") or "no session progress"
    logger.warning("Session %s stalled during %s (%s); sending one guarded continue prompt", session_id, phase, reason)
    if on_update:
        on_update(f"recovery: session stalled during {phase} ({reason}); sending guarded continue prompt")
    _prepare_continue_artifact_for_phase(repo_path=repo_path, phase=phase)
    snapshot = _llm_guard_before(state, batch, phase)
    client.send_message(session_id, _continue_prompt_for_phase(phase), model_id=model_id)
    try:
        return client.poll_completion(
            session_id,
            timeout=max(120, min(timeout, 600)),
            on_update=on_update,
            **({"stalled_no_progress_seconds": stalled_no_progress_seconds} if stalled_no_progress_seconds is not None else {}),
        )
    finally:
        _llm_guard_after(state, snapshot)


def _capture_session_retrospect(
    *,
    state: Dict[str, Any],
    repo_path: str,
    client: Any,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    stages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    target_ids = list(session_ids or [])
    if session_id and session_id not in target_ids:
        target_ids.append(session_id)
    if not target_ids:
        return state.get("session_retrospect", {}) or {}

    session_reports: List[Dict[str, Any]] = []
    for target_id in target_ids:
        try:
            session_reports.append(client.analyze_session_retrospect(target_id))
        except Exception as exc:
            logger.warning("Failed to build session retrospect for %s: %s", target_id, exc)

    if not session_reports:
        return state.get("session_retrospect", {}) or {}

    hints: List[str] = []
    observations: List[str] = []
    compile_facts: List[str] = []
    repeated_tools: List[Dict[str, Any]] = []
    for report in session_reports:
        for hint in report.get("hints") or []:
            if hint not in hints:
                hints.append(hint)
        for observation in report.get("observations") or []:
            if observation not in observations:
                observations.append(observation)
        for fact in report.get("compile_facts") or []:
            if fact not in compile_facts:
                compile_facts.append(fact)
        for tool in report.get("repeated_tools") or []:
            if tool not in repeated_tools:
                repeated_tools.append(tool)

    merged = {
        "session_id": target_ids[-1],
        "session_ids": target_ids,
        "hint_count": len(hints),
        "hints": hints,
        "compile_facts": compile_facts,
        "observations": observations,
        "tool_count": sum(int(report.get("tool_count", 0) or 0) for report in session_reports),
        "patch_count": sum(int(report.get("patch_count", 0) or 0) for report in session_reports),
        "repeated_tools": repeated_tools,
    }
    merged["path"] = write_session_retrospect(repo_path, merged)
    stage_names = stages or ["plan", "generate", "compile_fix", "test_fix", "coverage_fix", "mutation_fix"]
    stage_paths: Dict[str, str] = {}
    for stage_name in stage_names:
        try:
            stage_paths[stage_name] = append_stage_introspect(repo_path, stage_name, hints)
        except Exception:
            logger.debug("Stage introspect append skipped for %s", stage_name, exc_info=True)
    if stage_paths:
        merged["stage_introspect_paths"] = stage_paths
    if compile_facts:
        merged["compile_facts_path"] = merge_compile_fix_facts(repo_path, compile_facts)
    return merged


def _stage_introspect_section(repo_path: str, stage: str) -> str:
    try:
        path = ensure_stage_introspect_file(repo_path, stage)
    except Exception:
        logger.debug("Stage introspect path unavailable for %s", stage, exc_info=True)
        return ""
    return (
        "\n\n### STAGE INTROSPECT\n"
        f"- Prior lessons for this stage: `{path}`\n"
        "- Read this file before broad exploration. Apply only lessons relevant to the current target and do not repeat already-avoided mistakes."
    )


def _capture_session_token_usage(
    *,
    state: Dict[str, Any],
    client: Any,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    target_ids = list(session_ids or [])
    if session_id and session_id not in target_ids:
        target_ids.append(session_id)
    if not target_ids:
        return state.get("session_token_usage", {}) or {}
    aggregated = {
        "session_id": target_ids[-1],
        "session_ids": target_ids,
        "assistant_messages": 0,
        "main_model_tokens": _empty_token_bucket(),
        "small_model_tokens": _empty_token_bucket(),
        "other_model_tokens": _empty_token_bucket(),
        "total_tokens": _empty_token_bucket(),
        "by_model": {},
    }
    any_success = False
    for target_id in target_ids:
        try:
            token_usage = client.analyze_session_tokens(target_id)
            any_success = True
        except Exception as exc:
            logger.warning("Failed to build session token usage for %s: %s", target_id, exc)
            continue
        aggregated["assistant_messages"] += int(token_usage.get("assistant_messages", 0) or 0)
        _sum_token_bucket(aggregated["main_model_tokens"], token_usage.get("main_model_tokens") or {})
        _sum_token_bucket(aggregated["small_model_tokens"], token_usage.get("small_model_tokens") or {})
        _sum_token_bucket(aggregated["other_model_tokens"], token_usage.get("other_model_tokens") or {})
        _sum_token_bucket(aggregated["total_tokens"], token_usage.get("total_tokens") or {})
        for model_key, bucket in (token_usage.get("by_model") or {}).items():
            target_bucket = aggregated["by_model"].setdefault(model_key, _empty_token_bucket())
            _sum_token_bucket(target_bucket, bucket or {})
    return aggregated if any_success else (state.get("session_token_usage", {}) or {})


def _capture_phase_token_usage(
    *,
    state: Dict[str, Any],
    client: Any,
    phase_session_ids: Dict[str, List[str]],
) -> Dict[str, Dict[str, int]]:
    observed: Dict[str, Dict[str, int]] = {}
    for phase, session_ids in (phase_session_ids or {}).items():
        if not session_ids:
            continue
        usage = _capture_session_token_usage(
            state={"session_token_usage": {}},
            client=client,
            session_ids=session_ids,
        )
        total_tokens = (usage or {}).get("total_tokens") or {}
        if any(int(total_tokens.get(metric, 0) or 0) for metric in _empty_token_bucket().keys()):
            observed[phase] = {
                metric: int(total_tokens.get(metric, 0) or 0)
                for metric in _empty_token_bucket().keys()
            }
    return observed if observed else (state.get("phase_token_usage", {}) or {})

from uta.engine.source_selection import get_all_java_files, get_changed_java_files, filter_files
from uta.language.java.context_builder import ContextBuilder
from uta.engine.project_summary_artifacts import (
    append_stage_introspect,
    ensure_stage_introspect_file,
    merge_compile_fix_facts,
    sync_project_summaries,
    maybe_run_project_init_command,
    maybe_run_opencode_init_slash,
    prompt_template_paths,
    write_session_retrospect,
)
from uta.language.java.symbol_resolver import resolve_symbols, format_candidates_markdown
from uta.config import settings as uta_settings
from uta.opencode.client import OpenCodeClient
from uta.maven.jacoco import (
    extract_uncovered_clusters,
    format_uncovered_clusters_markdown,
    run_test_with_jacoco,
    run_tests_with_jacoco_batch,
    find_jacoco_report,
    parse_jacoco_report,
    parse_surefire_results,
)
from uta.maven.pitest import (
    run_pitest,
    find_latest_pitest_report,
    parse_pitest_report,
    parse_pitest_green_suite_failure,
    compute_mutation_stats,
    summarize_surviving_mutants,
    format_mutation_families_markdown,
)
from uta.graph.state import AgentState
from uta.engine.parse import ParseProjectRequest, make_parse_provider
from uta.tasks.targets import TargetIdentity


def _clean_rerun_artifacts(repo_path: str) -> None:
    """Remove stale untracked artifacts while preserving UTA cache/summary outputs."""
    try:
        result = _git_run(
            repo_path,
            "ls-files",
            "--others",
            "--exclude-standard",
            capture_output=True,
            check=False,
        )
        untracked = result.stdout.decode(errors="replace").splitlines() if result.stdout else []
    except Exception as exc:
        logger.warning("Could not list untracked rerun artifacts in %s: %s", repo_path, exc)
        return

    preserved_prefixes = (".uta_cache/", ".uta_reports/")
    preserved_files = {
        ".uta_summary.md",
        "opencode.json",
    }
    removable = []
    for rel in untracked:
        normalized = rel.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized in preserved_files or normalized.startswith(preserved_prefixes):
            continue
        removable.append(Path(repo_path) / normalized)

    for path in removable:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove stale rerun artifact %s: %s", path, exc)

    if removable:
        logger.info(
            "Removed %d stale untracked rerun artifact(s) (preserved .uta_cache, .uta_reports, .uta_summary.md, and opencode.json)",
            len(removable),
        )

def setup_branch(state: AgentState) -> Dict[str, Any]:
    """Create/update the configured generation branch from the default branch."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    branch_name = state.get("branch_name", "unit-code-gen")
    if state.get("preserve_branch"):
        current = _git_run(
            repo_path,
            "branch",
            "--show-current",
            capture_output=True,
            check=False,
        )
        current_branch = current.stdout.decode(errors="replace").strip() if current.stdout else ""
        if branch_name and current_branch != branch_name:
            exists = _git_run(
                repo_path,
                "rev-parse",
                "--verify",
                branch_name,
                capture_output=True,
                check=False,
            )
            if exists.returncode != 0:
                # Branch doesn't exist yet (first production run for this task) — fall through to creation.
                logger.info("Production branch %s not found locally; will create from default branch.", branch_name)
            else:
                checkout = _git_run(
                    repo_path,
                    "checkout",
                    branch_name,
                    capture_output=True,
                    check=False,
                )
                if checkout.returncode != 0:
                    err = checkout.stderr.decode(errors="replace") if checkout.stderr else ""
                    raise RuntimeError(
                        f"Could not checkout existing generation branch {branch_name} without resetting local changes. "
                        f"stderr: {err[:300]}"
                    )
                logger.info("Using existing branch/worktree for %s; skipping reset/cleanup", branch_name)
                return {
                    "current_stage": "setup_branch",
                    "phase_timings": _merge_phase_timings(
                        state,
                        setup_branch_seconds=time.perf_counter() - started,
                    ),
                }
        else:
            logger.info("Using existing branch/worktree for %s; skipping reset/cleanup", branch_name)
            return {
                "current_stage": "setup_branch",
                "phase_timings": _merge_phase_timings(
                    state,
                    setup_branch_seconds=time.perf_counter() - started,
                ),
            }

    # Fetch latest from remote (non-fatal if no remote)
    logger.info("Fetching latest from remote...")
    _git_run(repo_path, "fetch", "origin", capture_output=True, check=False)

    # Detect default branch — prefer remote tracking, fall back to local
    default_branch = None
    for candidate in ["origin/master", "origin/main", "master", "main"]:
        r = _git_run(repo_path, "rev-parse", "--verify", candidate, capture_output=True)
        if r.returncode == 0:
            default_branch = candidate
            break

    if not default_branch:
        # Last resort: use current HEAD
        default_branch = "HEAD"

    logger.info("Default branch: %s", default_branch)

    r2 = _git_run(
        repo_path,
        "checkout",
        "-B",
        branch_name,
        default_branch,
        "-f",
        capture_output=True,
        check=False,
    )
    if r2.returncode != 0:
        err = r2.stderr.decode(errors="replace") if r2.stderr else ""
        logger.error("Could not recreate %s from %s in %s: %s", branch_name, default_branch, repo_path, err[:500])
        raise RuntimeError(
            f"git could not recreate branch {branch_name} from {default_branch} in {repo_path}. "
            f"Ensure the repo is a git checkout with a valid default branch. stderr: {err[:300]}"
        )

    _git_run(
        repo_path,
        "reset",
        "--hard",
        default_branch,
        capture_output=True,
        check=False,
    )
    _clean_rerun_artifacts(repo_path)
    logger.info("Recreated %s from %s and cleared stale generated test artifacts", branch_name, default_branch)
    return {
        "phase_timings": _merge_phase_timings(
            state,
            branch_setup_seconds=time.perf_counter() - started,
        )
    }

def _upgrade_mockito(repo_path: str) -> bool:
    """Upgrade/normalize Mockito test dependencies in pom.xml files.

    - mockito-all → mockito-core:2.28.2 (with or without version tag)
    - Removes mockito-inline if present (incompatible with Java 8)
    - Pins ByteBuddy + agent to 1.9.10 when Mockito is present, avoiding the
      common runtime mismatch where mockito-core 2.28.2 is paired with
      byte-buddy 1.9.6 and byte-buddy-agent 1.9.10.

    Returns True if any pom was modified.
    """
    import re
    modified = False

    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
            original = content
        except Exception:
            continue

        # Replace mockito-all with mockito-core — handle both with and without <version>
        if "mockito-all" in content:
            # Case 1: <artifactId>mockito-all</artifactId> followed by <version>
            content = re.sub(
                r"<artifactId>mockito-all</artifactId>(\s*<version>[^<]+</version>)",
                "<artifactId>mockito-core</artifactId>\n            <version>2.28.2</version>",
                content,
            )
            # Case 2: <artifactId>mockito-all</artifactId> without <version> (inherits from parent)
            content = content.replace(
                "<artifactId>mockito-all</artifactId>",
                "<artifactId>mockito-core</artifactId>",
            )
            logger.info("Upgraded mockito-all → mockito-core in %s", pom_path)

        # Also remove the stale property if present
        content = re.sub(
            r"<mockito-all\.version>[^<]+</mockito-all\.version>",
            "<mockito-core.version>2.28.2</mockito-core.version>",
            content,
        )

        if "<mockito-core.version>" in content and "<byte-buddy.version>" not in content:
            content = content.replace(
                "<mockito-core.version>2.28.2</mockito-core.version>",
                "<mockito-core.version>2.28.2</mockito-core.version>\n        <byte-buddy.version>1.9.10</byte-buddy.version>",
            )

        content = re.sub(
            r"<byte-buddy\.version>[^<]+</byte-buddy\.version>",
            "<byte-buddy.version>1.9.10</byte-buddy.version>",
            content,
        )

        # Upgrade mockito-core 1.x to 2.28.2
        def _bump_mockito(m):
            ver = m.group(1)
            if ver.startswith("1."):
                return "<artifactId>mockito-core</artifactId>\n            <version>2.28.2</version>"
            return m.group(0)

        content = re.sub(
            r"<artifactId>mockito-core</artifactId>\s*<version>([^<]+)</version>",
            _bump_mockito,
            content,
        )

        # NOTE: Do NOT add mockito-inline — it is incompatible with Java 8
        # (causes ClassNotFoundException: mock-maker-default).
        # Instead, the prompt instructs the LLM to never mock concrete classes.

        # Remove mockito-inline if it was previously injected
        if "mockito-inline" in content:
            content = re.sub(
                r"\s*<dependency>\s*<groupId>org\.mockito</groupId>\s*"
                r"<artifactId>mockito-inline</artifactId>\s*"
                r"<version>[^<]+</version>\s*"
                r"(?:<scope>[^<]+</scope>\s*)?"
                r"</dependency>",
                "",
                content,
            )
            logger.info("Removed mockito-inline (Java 8 incompatible) from %s", pom_path)

        if "byte-buddy" in content:
            content = re.sub(
                r"(<artifactId>byte-buddy(?:-agent)?</artifactId>\s*<version>)[^<]+(</version>)",
                r"\g<1>${byte-buddy.version}\2",
                content,
            )

        if content != original:
            pom_path.write_text(content)
            modified = True

    # If no pom.xml has mockito-core at all, add it to the root pom's <dependencies>
    root_pom = Path(repo_path) / "pom.xml"
    if root_pom.exists():
        root_content = root_pom.read_text(errors="replace")
        root_original = root_content
        if "<mockito-core.version>" in root_content and "<byte-buddy.version>" not in root_content:
            root_content = root_content.replace(
                "<mockito-core.version>2.28.2</mockito-core.version>",
                "<mockito-core.version>2.28.2</mockito-core.version>\n        <byte-buddy.version>1.9.10</byte-buddy.version>",
            )
        elif "mockito-core" in root_content and "<byte-buddy.version>" not in root_content and "</properties>" in root_content:
            root_content = root_content.replace(
                "</properties>",
                "        <byte-buddy.version>1.9.10</byte-buddy.version>\n    </properties>",
                1,
            )
        if "mockito-core" not in root_content and "mockito-all" not in root_content:
            mockito_dep = (
                "        <dependency>\n"
                "            <groupId>org.mockito</groupId>\n"
                "            <artifactId>mockito-core</artifactId>\n"
                "            <version>2.28.2</version>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
            )
            # Insert before last </dependencies>
            last_idx = root_content.rfind("</dependencies>")
            if last_idx >= 0:
                root_content = root_content[:last_idx] + mockito_dep + "    " + root_content[last_idx:]
                logger.info("Added mockito-core:2.28.2 to %s (was missing entirely)", root_pom)

        if "mockito-core" in root_content:
            byte_buddy_mgmt = (
                "            <dependency>\n"
                "                <groupId>net.bytebuddy</groupId>\n"
                "                <artifactId>byte-buddy</artifactId>\n"
                "                <version>${byte-buddy.version}</version>\n"
                "                <scope>test</scope>\n"
                "            </dependency>\n"
                "\n"
                "            <dependency>\n"
                "                <groupId>net.bytebuddy</groupId>\n"
                "                <artifactId>byte-buddy-agent</artifactId>\n"
                "                <version>${byte-buddy.version}</version>\n"
                "                <scope>test</scope>\n"
                "            </dependency>\n"
            )
            if "<dependencyManagement>" in root_content and "<artifactId>byte-buddy</artifactId>" not in root_content:
                insert_at = root_content.find("</dependencies>", root_content.find("<dependencyManagement>"))
                if insert_at >= 0:
                    root_content = root_content[:insert_at] + byte_buddy_mgmt + root_content[insert_at:]
                    logger.info("Added ByteBuddy dependencyManagement entries to %s", root_pom)

            byte_buddy_dep = (
                "        <dependency>\n"
                "            <groupId>net.bytebuddy</groupId>\n"
                "            <artifactId>byte-buddy</artifactId>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
                "\n"
                "        <dependency>\n"
                "            <groupId>net.bytebuddy</groupId>\n"
                "            <artifactId>byte-buddy-agent</artifactId>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
            )
            deps_start = root_content.rfind("<dependencies>")
            deps_end = root_content.rfind("</dependencies>")
            deps_block = root_content[deps_start:deps_end] if deps_start >= 0 and deps_end >= 0 else ""
            if deps_block and "<artifactId>byte-buddy</artifactId>" not in deps_block:
                root_content = root_content[:deps_end] + byte_buddy_dep + "    " + root_content[deps_end:]
                logger.info("Added direct ByteBuddy test deps to %s", root_pom)

        if root_content != root_original:
            root_pom.write_text(root_content)
            modified = True

    return modified


def _mockito_api_guidance(repo_path: str) -> str:
    has_mockito_all = False
    has_mockito_core = False
    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
        except Exception:
            continue
        has_mockito_all = has_mockito_all or "mockito-all" in content
        has_mockito_core = has_mockito_core or "mockito-core" in content
    if has_mockito_all and not has_mockito_core:
        return (
            "- Detected committed repo dependencies use Mockito 1.x (`mockito-all`).\n"
            "- Use `org.mockito.Matchers` and `org.mockito.runners.MockitoJUnitRunner`.\n"
            "- Do NOT import `org.mockito.ArgumentMatchers` or `org.mockito.junit.MockitoJUnitRunner` unless the committed pom already uses Mockito 2.x."
        )
    return (
        "- Use **Mockito 2.x** for interfaces and abstract collaborators when that fits the repo style.\n"
        "- Use `org.mockito.ArgumentMatchers.any()` — NOT the deprecated `org.mockito.Matchers.any()`."
    )


def _fix_mockito_imports(repo_path: str):
    """Fix existing test files for Mockito 1→2 migration.

    - org.mockito.Matchers → org.mockito.ArgumentMatchers
    - org.mockito.runners.MockitoJUnitRunner → org.mockito.junit.MockitoJUnitRunner
    """
    import re
    test_dirs = list(Path(repo_path).rglob("src/test/java"))
    for test_dir in test_dirs:
        for java_file in test_dir.rglob("*.java"):
            try:
                content = java_file.read_text(errors="replace")
                original = content
                content = content.replace(
                    "org.mockito.Matchers",
                    "org.mockito.ArgumentMatchers",
                )
                content = content.replace(
                    "org.mockito.runners.MockitoJUnitRunner",
                    "org.mockito.junit.MockitoJUnitRunner",
                )
                if content != original:
                    java_file.write_text(content)
                    logger.info("Fixed Mockito imports in %s", java_file)
            except Exception:
                continue


def _relax_surefire_skiptests(repo_path: str) -> bool:
    """Rewrite hardcoded Surefire ``skipTests`` flags so CLI properties can override them.

    Some legacy repos pin ``<skipTests>true</skipTests>`` in pom.xml, which makes
    ``mvn test -DskipTests=false`` still skip execution. On the temporary UTA test
    branch we rewrite those entries to ``${skipTests}``, preserving opt-out while
    allowing explicit CLI override.
    """
    import re

    modified = False
    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
            original = content
        except Exception:
            continue

        if "<artifactId>maven-surefire-plugin</artifactId>" not in content:
            continue

        content = re.sub(
            r"(<artifactId>maven-surefire-plugin</artifactId>.*?<skipTests>)\s*true\s*(</skipTests>)",
            r"\1${skipTests}\2",
            content,
            flags=re.DOTALL,
        )

        if content != original:
            pom_path.write_text(content)
            modified = True
            logger.info("Relaxed hardcoded surefire skipTests in %s", pom_path)

    return modified


_PAIR_SHIM = '''\
package javafx.util;

/**
 * Minimal shim for javafx.util.Pair — allows projects that import this class
 * to compile on OpenJDK/Zulu (which does not bundle JavaFX).
 * Auto-generated by UTA baseline_compile.
 */
public class Pair<K, V> {
    private final K key;
    private final V value;

    public Pair(K key, V value) {
        this.key = key;
        this.value = value;
    }

    public K getKey() { return key; }
    public V getValue() { return value; }

    @Override
    public String toString() { return key + "=" + value; }

    @Override
    public int hashCode() {
        int h = key != null ? key.hashCode() : 0;
        h = 31 * h + (value != null ? value.hashCode() : 0);
        return h;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Pair)) return false;
        Pair<?, ?> p = (Pair<?, ?>) o;
        return java.util.Objects.equals(key, p.key) && java.util.Objects.equals(value, p.value);
    }
}
'''


def _ensure_javafx_pair(repo_path: str) -> bool:
    """Create a javafx.util.Pair shim if the project uses it.

    Many legacy projects import javafx.util.Pair which is bundled with Oracle JDK 8
    but missing from OpenJDK/Zulu. We create a minimal Pair class in the first module
    that has src/main/java so the import resolves without changing any source files.
    """
    # Quick check: does any Java file import javafx.util.Pair?
    has_javafx = False
    for java_file in Path(repo_path).rglob("src/main/java/**/*.java"):
        try:
            if "javafx.util.Pair" in java_file.read_text(errors="replace"):
                has_javafx = True
                break
        except Exception:
            continue

    if not has_javafx:
        return False

    # Find a suitable module to place the shim — prefer common, then service, then root
    candidates = ["common", "service", "model"]
    target_dir = None
    for mod in candidates:
        d = Path(repo_path) / mod / "src" / "main" / "java"
        if d.exists():
            target_dir = d
            break
    if not target_dir:
        # Fall back to any module with src/main/java
        for d in Path(repo_path).glob("*/src/main/java"):
            target_dir = d
            break
    if not target_dir:
        target_dir = Path(repo_path) / "src" / "main" / "java"

    shim_dir = target_dir / "javafx" / "util"
    shim_file = shim_dir / "Pair.java"
    if shim_file.exists():
        return False

    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_file.write_text(_PAIR_SHIM)
    logger.info("Created javafx.util.Pair shim at %s", shim_file)
    return True


def baseline_compile(state: AgentState) -> Dict[str, Any]:
    """Upgrade Mockito to 2.x if needed, then verify target module compiles."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    module = state["module"]
    quality_mode = state.get("quality_mode") or "class_batch"
    _set_stage(state, "baseline_compile", "upgrade test deps and verify compile")
    before_deterministic = _git_status_snapshot(repo_path) if state.get("task_id") and state.get("task_db_path") else {}

    # Upgrade Mockito 1.x → 2.x in pom.xml and fix existing test imports
    if quality_mode == "ci_incremental":
        logger.info("ci_incremental mode: preserving committed test dependencies during baseline compile")
    elif _upgrade_mockito(repo_path):
        logger.info("Mockito upgraded to 2.x — fixing existing test imports")
        _fix_mockito_imports(repo_path)

    if _relax_surefire_skiptests(repo_path):
        logger.info("Rewrote hardcoded surefire skipTests so UTA can force test execution")

    # Add javafx.util.Pair shim if needed (missing on OpenJDK/Zulu)
    _ensure_javafx_pair(repo_path)

    deterministic_change_paths: List[str] = list(state.get("deterministic_change_paths") or [])
    def _merge_deterministic_changes(before_snapshot: Dict[str, str]) -> List[str]:
        if not before_snapshot:
            return deterministic_change_paths
        after_snapshot = _git_status_snapshot(repo_path)
        changed_paths = sorted(
            path
            for path in set(before_snapshot.keys()) | set(after_snapshot.keys())
            if before_snapshot.get(path) != after_snapshot.get(path)
        )
        if not changed_paths:
            return deterministic_change_paths
        return list(dict.fromkeys(deterministic_change_paths + changed_paths))

    if before_deterministic:
        after_deterministic = _git_status_snapshot(repo_path)
        changed = sorted(
            path
            for path in set(before_deterministic.keys()) | set(after_deterministic.keys())
            if before_deterministic.get(path) != after_deterministic.get(path)
        )
        if changed:
            deterministic_change_paths = list(dict.fromkeys(deterministic_change_paths + changed))
            try:
                from uta.tasks.manager import TaskManager

                TaskManager(state["task_db_path"]).db.add_event(
                    int(state["task_id"]),
                    None,
                    "deterministic_change",
                    "Deterministic baseline setup changed files",
                    stage="baseline_compile",
                    payload={"paths": changed},
                )
            except Exception:
                logger.debug("Failed to record deterministic change audit event", exc_info=True)

    # Try compiling with -am first (builds dependencies too).
    # If that fails due to pre-existing errors in sibling modules,
    # fall back to compiling just the target module (assumes deps are installed).
    from uta.language.java.maven_project import with_default_profile_args

    cmd = with_default_profile_args([uta_settings.maven_bin, "compile", "-DskipTests"], Path(repo_path))
    if module:
        cmd.extend(["-pl", module, "-am"])

    def _compile_ok() -> Dict[str, Any]:
        before_init = _git_status_snapshot(repo_path) if state.get("task_id") and state.get("task_db_path") else {}
        maybe_run_opencode_init_slash(repo_path, state.get("session_id"))
        maybe_run_project_init_command(repo_path, uta_settings.opencode_init_command)
        updated_change_paths = _merge_deterministic_changes(before_init)
        return {
            "error": None,
            "current_stage": "baseline_compile",
            "deterministic_change_paths": updated_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }

    try:
        subprocess.run(cmd, cwd=repo_path, capture_output=True, check=True, timeout=600)
        return _compile_ok()
    except subprocess.CalledProcessError as e:
        if module:
            # Fall back: compile only the target module (deps must be in local .m2)
            logger.warning("Full compile failed, trying target module only...")
            cmd_fallback = with_default_profile_args(
                [uta_settings.maven_bin, "compile", "-DskipTests", "-pl", module],
                Path(repo_path),
            )
            try:
                subprocess.run(cmd_fallback, cwd=repo_path, capture_output=True,
                               check=True, timeout=600)
                logger.info("Target module compiled successfully (without -am)")
                return _compile_ok()
            except subprocess.CalledProcessError as e2:
                stderr = e2.stderr.decode(errors='replace') if e2.stderr else ""
                stdout = e2.stdout.decode(errors='replace') if e2.stdout else ""
                error_msg = stderr or stdout[-2000:] if stdout else "unknown error"
                return {
                    "error": f"Baseline compilation failed: {error_msg[-1000:]}",
                    "current_stage": "baseline_compile",
                    "deterministic_change_paths": deterministic_change_paths,
                    "phase_timings": _merge_phase_timings(
                        state,
                        baseline_compile_seconds=time.perf_counter() - started,
                    ),
                }
        stderr = e.stderr.decode(errors='replace') if e.stderr else ""
        stdout = e.stdout.decode(errors='replace') if e.stdout else ""
        error_msg = stderr or stdout[-2000:] if stdout else "unknown error"
        return {
            "error": f"Baseline compilation failed: {error_msg[-1000:]}",
            "current_stage": "baseline_compile",
            "deterministic_change_paths": deterministic_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "Baseline compilation timed out (10min)",
            "current_stage": "baseline_compile",
            "deterministic_change_paths": deterministic_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }

def scan_and_select(state: AgentState) -> Dict[str, Any]:
    """Rank .java files by change frequency."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    days = state["days"]
    module = state["module"]
    max_files = state["max_files"]
    select_all_files = bool(state.get("select_all_files", False))
    explicit_class_fqns = state.get("explicit_class_fqns", [])
    scan_detail = "all production files" if select_all_files else f"days={days} max_files={max_files}"
    _set_stage(state, "scan_candidates", scan_detail)

    if explicit_class_fqns:
        logger.info(
            "Using explicit class override: %d class(es)=%s",
            len(explicit_class_fqns),
            explicit_class_fqns,
        )
        return {
            "candidates": explicit_class_fqns,
            "current_stage": "scan_candidates",
            "phase_timings": _merge_phase_timings(
                state,
                scan_select_seconds=time.perf_counter() - started,
            ),
        }

    if select_all_files:
        files_with_counts = get_all_java_files(repo_path, module)
        top_files = [path for path, _count in files_with_counts]
        logger.info("Using all production Java files: %d file(s)", len(top_files))
    else:
        files_with_counts = get_changed_java_files(repo_path, days, module)
        top_files = filter_files(files_with_counts, max_files)
    return {
        "candidates": top_files,
        "current_stage": "scan_candidates",
        "phase_timings": _merge_phase_timings(
            state,
            scan_select_seconds=time.perf_counter() - started,
        ),
    }

def parse_context(state: AgentState) -> Dict[str, Any]:
    """One-time deep parse of the entire module."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    module = state["module"]
    _set_stage(state, "parse_context", "build graph and export cached context")

    parse_result = make_parse_provider("java").parse_project(
        ParseProjectRequest(repo_path=Path(repo_path), module=module)
    )
    graph = parse_result.graph
    flows = parse_result.flows

    # Export context files for agent exploration through the backend provider contract.
    ctx_provider = _JavaWorkflowContextProvider(repo_path, graph, flows)
    ctx_provider.export_project_context()
    sync_project_summaries(repo_path, graph, module, language="java")

    explicit_class_fqns = state.get("explicit_class_fqns", [])
    final_candidates = []
    if explicit_class_fqns:
        for fqn in explicit_class_fqns:
            if not parse_result.contains_target(fqn):
                logger.info("Explicit candidate not found in parsed graph: %s", fqn)
                continue
            final_candidates.append(fqn)
    else:
        for path in state["candidates"]:
            fqn = parse_result.target_id_for_source_path(path)
            if fqn and _is_testable_class(fqn, graph):
                final_candidates.append(fqn)
            elif fqn:
                logger.info("Filtered out candidate: %s", fqn)

    logger.info("Testable candidates: %d out of %d scanned files",
                len(final_candidates), len(state["candidates"]))
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager

            manager = TaskManager(task_db_path)
            manager.ensure_class_tasks(int(task_id), final_candidates, module=module)
            manager.db.update_repo_task(int(task_id), total_classes=len(final_candidates))
        except Exception:
            logger.debug("Failed to create production child class tasks", exc_info=True)
    return {
        "graph": graph,
        "flows": flows,
        "candidates": final_candidates,
        "target_candidates": parse_result.target_selections(final_candidates),
        "current_stage": "parse_context",
        "phase_timings": _merge_phase_timings(
            state,
            parse_context_seconds=time.perf_counter() - started,
        ),
    }

_ACCESSOR_METHOD_NAMES = {"equals", "hashCode", "toString", "canEqual"}


def _method_simple_name(method_node) -> str:
    return method_node.fqn.rsplit(".", 1)[-1]


def _is_accessor_like_method_name(name: str) -> bool:
    if name in _ACCESSOR_METHOD_NAMES:
        return True
    return (
        (name.startswith("get") and len(name) > 3 and name[3].isupper())
        or (name.startswith("set") and len(name) > 3 and name[3].isupper())
        or (name.startswith("is") and len(name) > 2 and name[2].isupper())
    )


def _is_accessor_like_method(method_node) -> bool:
    name = _method_simple_name(method_node)
    if name in _ACCESSOR_METHOD_NAMES:
        return True
    if not _is_accessor_like_method_name(name):
        return False

    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    external_calls = int(complexity.get("external_calls", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic <= 1 and body_lines <= 6 and control_nodes == 0 and external_calls <= 2


def _is_data_like_class_name(name: str) -> bool:
    data_suffixes = (
        "DTO", "Dto", "VO", "Vo", "AO", "Ao", "BO", "Bo", "DO", "Do", "PO", "Po",
        "Param", "Params", "Request", "Response", "Result", "Message", "Context",
        "Item", "Info", "Detail", "Entity", "Model", "Data", "Key", "Query", "Form",
    )
    return name.endswith(data_suffixes)


def _is_data_like_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    data_paths = (
        "/model/", "/bean/", "/beans/", "/param/", "/params/", "/dto/", "/vo/",
        "/entity/", "/entities/", "/query/", "/form/",
    )
    return any(part in normalized for part in data_paths)


def _is_thin_event_class_name(name: str) -> bool:
    return name.endswith(("Actor", "Listener", "Schedule", "Task"))


def _is_thin_event_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(part in normalized for part in ("/actor/", "/listener/", "/workflow/"))


def _is_business_like_class_name(name: str) -> bool:
    if _is_thin_event_class_name(name):
        return False
    business_suffixes = (
        "Biz", "BizImpl", "Service", "ServiceImpl", "Handler", "Processor", "Manager",
        "Checker", "Validator", "Strategy", "Executor", "Factory", "Rule",
        "WriterBack", "WriteBack",
    )
    return name.endswith(business_suffixes)


def _is_business_like_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if _is_thin_event_path(normalized):
        return False
    business_paths = (
        "/biz/", "/handler/", "/processor/", "/manager/",
        "/service/impl/", "/checkservice/", "/validate/",
        "/validator/",
    )
    return any(part in normalized for part in business_paths)


def _is_complex_single_method(method_node) -> bool:
    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic >= 4 or body_lines >= 30 or control_nodes >= 3


def _is_trivial_delegate_method(method_node) -> bool:
    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic <= 1 and body_lines <= 6 and control_nodes == 0


def _has_listener_registration_hint(name: str, methods: List[Any]) -> bool:
    if name.endswith(("Register", "Registrar", "Registry")):
        return True
    registration_annotations = {
        "MessageConsumer",
        "RabbitListener",
        "KafkaListener",
        "JmsListener",
        "EventListener",
        "Scheduled",
    }
    return any(
        str(annotation).lstrip("@").split(".")[-1] in registration_annotations
        for method in methods
        for annotation in (method.metadata.get("annotations") or [])
    )


def _is_delegate_only_registration_wrapper(name: str, behavior_methods: List[Any]) -> bool:
    if len(behavior_methods) < 2 or not _has_listener_registration_hint(name, behavior_methods):
        return False
    trivial_methods = [method for method in behavior_methods if _is_trivial_delegate_method(method)]
    return len(trivial_methods) / len(behavior_methods) >= 0.8


def _is_testable_class(fqn: str, graph) -> bool:
    """Filter out classes that are NOT good candidates for unit testing.

    Excludes:
    - Entry-level wrappers (Controllers, REST endpoints, Dubbo facades) — thin delegation, no logic
    - Outbound wrappers / remote adapters — just proxy remote calls
    - Test-driven scripts / backdoor scripts — operational tooling, not business logic
    - Interfaces, abstract classes, enums, constants, configs
    - POJOs / DTOs / models with no methods beyond getters/setters
    """
    node = graph.nodes.get(fqn)
    if not node or node.kind != "class":
        return False

    annotations = node.metadata.get("annotations", [])
    name = fqn.split(".")[-1]
    path = node.file_path.lower().replace("\\", "/") if node.file_path else ""

    # --- Exclude by annotation: entry-level wrappers ---
    entry_annotations = {"Controller", "RestController", "DubboService", "RequestMapping"}
    if any(a in entry_annotations for a in annotations):
        logger.debug("Skipping %s — entry-level wrapper (%s)", fqn, annotations)
        return False

    # --- Exclude by name pattern ---
    skip_suffixes = (
        "Controller", "Facade", "Endpoint", "Resource",  # entry wrappers
        "RemoteWrapper", "Wrapper", "Adapter", "Proxy",  # outbound wrappers
        "Script", "Tool", "Migration", "Patch", "Fix",   # scripts / backdoors
        "Config", "Configuration", "Properties",          # config classes
        "Constant", "Constants", "Enum",                  # constants
    )
    if any(name.endswith(s) for s in skip_suffixes):
        logger.debug("Skipping %s — name pattern match", fqn)
        return False

    skip_contains = ("backdoor", "script", "migration", "patch", "tool", "demo", "test")
    if any(k in name.lower() for k in skip_contains):
        logger.debug("Skipping %s — name contains excluded keyword", fqn)
        return False

    # --- Exclude by path pattern ---
    skip_paths = ("adapter/", "wrapper/", "controller/", "facade/", "endpoint/",
                  "script/", "tool/", "backdoor/", "migration/")
    if any(p in path for p in skip_paths):
        logger.debug("Skipping %s — path contains excluded dir", fqn)
        return False

    # --- Exclude pure interfaces / abstract classes ---
    modifiers = node.metadata.get("modifiers", [])
    if "abstract" in modifiers or "interface" in [node.kind]:
        logger.debug("Skipping %s — abstract/interface", fqn)
        return False

    # --- Keep classes with real behavior; exclude accessor-heavy data holders ---
    methods = [n for n_fqn, n in graph.nodes.items()
               if n.kind == "method" and n.metadata.get("parent_fqn") == fqn
               and "private" not in n.metadata.get("modifiers", [])]
    if not methods:
        logger.debug("Skipping %s — no public methods", fqn)
        return False

    behavior_methods = [
        method for method in methods
        if not _is_accessor_like_method(method)
    ]
    if not behavior_methods:
        logger.debug("Skipping %s — only accessor/object methods", fqn)
        return False

    data_like = _is_data_like_class_name(name) or _is_data_like_path(path)
    business_like = _is_business_like_class_name(name) or _is_business_like_path(path)
    thin_event_like = _is_thin_event_class_name(name) or _is_thin_event_path(path)
    accessor_count = len(methods) - len(behavior_methods)
    accessor_ratio = accessor_count / len(methods)
    if data_like and (not business_like or accessor_ratio >= 0.5):
        logger.debug(
            "Skipping %s — data-like class/path with weak behavior (%d behavior, %.0f%% accessors)",
            fqn,
            len(behavior_methods),
            accessor_ratio * 100,
        )
        return False

    if len(behavior_methods) == 1 and accessor_ratio >= 0.5 and _is_trivial_delegate_method(behavior_methods[0]):
        logger.debug("Skipping %s — accessor-backed thin delegator", fqn)
        return False

    if _is_delegate_only_registration_wrapper(name, behavior_methods):
        logger.debug("Skipping %s — delegate-only listener registration wrapper", fqn)
        return False

    if len(methods) == 1 and not business_like:
        if thin_event_like and _is_complex_single_method(behavior_methods[0]):
            return True
        logger.debug("Skipping %s — single behavior method without business class/path hint", fqn)
        return False

    return True


def select_next_class(state: AgentState) -> Dict[str, Any]:
    _set_stage(state, "select_batch", "choose next batch", class_fqns=[])
    candidates = state["candidates"]
    results = state["results"]
    batch_cap = state.get("classes_per_agent_run", 1)

    remaining = [fqn for fqn in candidates if fqn not in results]
    if not remaining:
        return {
            "finished": True,
            "current_batch": [],
            "current_class": None,
            "current_stage": "select_batch",
            **_target_alias_update([]),
        }
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager
            from uta.tasks.models import TERMINAL_CLASS_STATUSES

            rows = TaskManager(task_db_path).db.class_tasks_by_fqn(int(task_id), remaining)
            # Skip classes already in a terminal state in the DB (e.g. PASS from a prior
            # successful run that wasn't reflected in the in-memory results dict on resume).
            already_done = {fqn for fqn, row in rows.items() if row["status"] in TERMINAL_CLASS_STATUSES}
            if already_done:
                logger.info("select_batch: skipping %d already-terminal DB class(es): %s", len(already_done), already_done)
                remaining = [fqn for fqn in remaining if fqn not in already_done]
            if not remaining:
                return {
                    "finished": True,
                    "current_batch": [],
                    "current_class": None,
                    "current_stage": "select_batch",
                    **_target_alias_update([]),
                }
            candidate_index = {fqn: index for index, fqn in enumerate(candidates)}
            remaining = sorted(
                remaining,
                key=lambda fqn: (
                    int(rows[fqn]["priority"]) if fqn in rows else 100,
                    candidate_index.get(fqn, 10**9),
                ),
            )
        except Exception:
            logger.debug("Failed to apply production class priority ordering", exc_info=True)

    batch = _select_smart_batch(state, remaining, max(1, int(batch_cap or 1)))
    active_module = _class_module(state, batch[0]) if batch else state.get("module")
    _verify_task_branch_and_preexisting_diff(state, batch)
    logger.info(
        "Selected batch: requested=%d actual=%d classes=%s",
        batch_cap,
        len(batch),
        batch,
    )
    update = {
        "current_batch": batch,
        "current_class": batch[0],
        "finished": False,
        "current_stage": "select_batch",
        **_target_alias_update(batch),
    }
    if "module_filter" not in state:
        update["module_filter"] = state.get("module")
    if active_module:
        update["module"] = active_module
    return update

def _writeback_resolved_symbols(
    compile_errors: str,
    repo_path: str,
    class_fqn: str,
    symbols_abs: Optional[str],
) -> Dict[str, List[str]]:
    """Resolve unresolved symbols from compile errors and write candidates into
    the class-level .symbols.md and project compile_facts.md so the next fix
    iteration doesn't need to grep for the same types (strategy C).
    """
    from uta.compile import classify_compile_errors, CATEGORY_UNRESOLVED_SYMBOL
    errors = classify_compile_errors(compile_errors)
    unresolved = [
        e.symbol for e in errors
        if e.category == CATEGORY_UNRESOLVED_SYMBOL and e.symbol
    ]
    if not unresolved:
        return {}

    package = class_fqn.rsplit(".", 1)[0] if "." in class_fqn else ""
    resolutions = resolve_symbols(
        unresolved,
        repo_path,
        target_package=package,
        limit_per_symbol=3,
    )
    if not any(resolutions.values()):
        return {}

    md_block = format_candidates_markdown(resolutions)

    # Append to class-level .symbols.md
    if symbols_abs:
        sym_path = Path(symbols_abs)
        if sym_path.exists():
            existing = sym_path.read_text(encoding="utf-8")
            if "## Python-Resolved Candidates" not in existing:
                sym_path.write_text(
                    existing.rstrip() + "\n\n## Python-Resolved Candidates\n\n" + md_block,
                    encoding="utf-8",
                )
            else:
                # Replace the section
                pre, _, _ = existing.partition("## Python-Resolved Candidates")
                sym_path.write_text(
                    pre.rstrip() + "\n\n## Python-Resolved Candidates\n\n" + md_block,
                    encoding="utf-8",
                )
            logger.debug("[%s] Wrote %d resolved symbols to %s", class_fqn, len(resolutions), sym_path)

    # Append terse import candidates to compile_facts.md
    facts = [
        f"Unresolved `{name}` candidates: " + ", ".join(f"`{c.fqn}`" for c in candidates)
        for name, candidates in resolutions.items()
        if candidates
    ]
    if facts:
        merge_compile_fix_facts(repo_path, facts)
    return {
        name: [candidate.fqn for candidate in candidates]
        for name, candidates in resolutions.items()
        if candidates
    }


def run_compile_fix_loop(
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    client: OpenCodeClient,
    maven_module_flag: str,
    generation_session_id: Optional[str] = None,
    target_context_paths: Optional[Dict[str, Dict[str, str]]] = None,
    max_fix_attempts: int = 3,
    session_id: Optional[str] = None,
    state: Optional[AgentState] = None,
) -> tuple[bool, Optional[str]]:
    """Re-run ``mvn test-compile`` until success or ``max_fix_attempts``, prompting the agent to fix."""
    generation_session_id = generation_session_id or session_id or ""
    compile_ok = False
    compile_fix_session_id: Optional[str] = None
    progress = None
    prev_classified: list = []  # classified errors from last attempt (for delta + hopeless guard)
    compile_fix_model: Optional[str] = None
    resolved_symbols_log: Dict[str, List[str]] = {}
    quality_mode = (state or {}).get("quality_mode") or "class_batch"
    expected_test_paths = _expected_test_paths_for_batch(module, batch)
    for attempt in range(max_fix_attempts):
        compile_ok, compile_errors = _compile_test(repo_path, module)
        if compile_ok:
            logger.info("Compilation passed (attempt %d)", attempt + 1)
            return True, compile_fix_session_id

        all_classified = classify_compile_errors(compile_errors)
        curr_classified = all_classified
        if quality_mode == "ci_incremental" and all_classified:
            curr_classified = _filter_compile_errors_to_paths(all_classified, expected_test_paths)
            if not curr_classified:
                logger.warning(
                    "ci_incremental compile gate saw %d compile error(s), but none are in current generated test file(s): %s. "
                    "Ignoring them for this per-class repair; final test-enforcer rerun remains authoritative.",
                    len(all_classified),
                    ", ".join(expected_test_paths),
                )
                return True, compile_fix_session_id
        compile_errors_for_prompt = compile_errors

        # Strategy C: resolve unresolved symbols and write candidates back to context files.
        first_fqn_c = batch[0]
        resolved_now = _writeback_resolved_symbols(
            compile_errors=compile_errors,
            repo_path=repo_path,
            class_fqn=first_fqn_c,
            symbols_abs=(target_context_paths or {}).get(first_fqn_c, {}).get("symbols_abs"),
        )
        for short_name, fqns in (resolved_now or {}).items():
            resolved_symbols_log[short_name] = list(fqns)

        # Hopeless-loop guard (strategy G): if all errors are identical to last attempt, stop.
        if attempt > 0 and prev_classified:
            new_errors, recurring = error_delta(prev_classified, curr_classified)
            if not new_errors and recurring:
                logger.warning(
                    "Compile-fix loop detected hopeless state at attempt %d/%d: "
                    "%d recurring error(s) with no new errors — stopping early.",
                    attempt + 1, max_fix_attempts, len(recurring),
                )
                return False, compile_fix_session_id
            compile_errors_for_prompt = _render_compile_fix_feedback(
                curr_classified,
                new_errors=new_errors,
                recurring_errors=recurring,
            )
            logger.info(
                "Error delta: %d new, %d recurring errors at attempt %d",
                len(new_errors), len(recurring), attempt + 1,
            )
        elif curr_classified:
            compile_errors_for_prompt = _render_compile_fix_feedback(curr_classified)
        prev_classified = curr_classified

        logger.warning(
            "Compilation failed (attempt %d/%d), sending fix prompt",
            attempt + 1,
            max_fix_attempts,
        )
        if not compile_fix_session_id:
            from uta.opencode.tiered_router import effective_model as _effective_model
            compile_fix_model = _effective_model("compile_fix")
            compile_fix_session_id = client.create_session(model_id=compile_fix_model)
            progress = _session_progress_logger(batch, session_id=compile_fix_session_id, stage="compile_fix")

        from uta.prompts.loader import render_prompt_split as _render_split
        first_fqn = batch[0]
        test_file_path = expected_test_paths[0]

        _fix_compile_kwargs = dict(
            class_fqn=first_fqn,
            compile_errors=compile_errors_for_prompt,
            test_file_path=test_file_path,
            maven_module_flag=maven_module_flag,
            mockito_api_guidance=_mockito_api_guidance(repo_path),
            target_context_abs=(target_context_paths or {}).get(first_fqn, {}).get("context_abs", ""),
            target_symbols_abs=(target_context_paths or {}).get(first_fqn, {}).get("symbols_abs", ""),
            stage_introspect_abs=ensure_stage_introspect_file(repo_path, "compile_fix"),
        )
        _fix_stable, _fix_volatile = _render_split("fix_compile", **_fix_compile_kwargs)
        _fix_volatile_tail = _fix_volatile
        if len(batch) > 1:
            extra_paths = []
            for fqn in batch:
                tn = f"{fqn.split('.')[-1]}Test"
                pkg = fqn.rsplit(".", 1)[0].replace(".", "/")
                extra_paths.append(f"{module_prefix}src/test/java/{pkg}/{tn}.java")
            _fix_volatile_tail += (
                "\n\n### ALL TEST FILES IN THIS BATCH\n"
                "Fix compilation errors in ANY of these files as needed:\n"
                + "\n".join(f"- `{p}`" for p in extra_paths)
            )
        _fix_volatile_tail += (
            "\n\n### GENERATION SESSION CONTEXT\n"
            f"- previous generation session: `{generation_session_id}`\n"
            "Continue from the existing generated files on disk. Do not restart broad exploration."
        )
        client.send_message_split(
            compile_fix_session_id,
            _fix_stable,
            _fix_volatile_tail,
            model_id=compile_fix_model or uta_settings.opencode_model,
        )
        event = _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=client,
                session_id=compile_fix_session_id,
                timeout=_llm_compile_fix_timeout(compile_fix_model or uta_settings.opencode_model),
                phase="compile_fix",
                batch=batch,
                model_id=compile_fix_model,
                on_update=progress,
                state=state,
            ),
            session_id=compile_fix_session_id,
            client=client,
            phase="compile_fix",
        )
        if _event_needs_fresh_session(event):
            logger.warning(
                "Compile-fix session %s ended with %s; starting a fresh repair session on the next attempt",
                compile_fix_session_id,
                event.get("type"),
            )
            compile_fix_session_id = None
            progress = None

    # L1: record compile-fix inefficiencies for cross-run learning.
    try:
        from uta.learning import record_run_inefficiencies
        from uta.compile import CATEGORY_UNRESOLVED_SYMBOL
        last_symbols: Dict[str, List[str]] = {}
        if prev_classified:
            sym_path = (target_context_paths or {}).get(batch[0], {}).get("symbols_abs")
            for e in prev_classified:
                if e.category == CATEGORY_UNRESOLVED_SYMBOL and e.symbol:
                    last_symbols.setdefault(e.symbol, [])
        record_run_inefficiencies(
            repo_path=repo_path,
            class_fqn=batch[0],
            compile_fix_iterations=attempt + 1,
            recurring_error_sigs=[e.signature for e in prev_classified] if prev_classified else [],
            resolved_symbols=resolved_symbols_log,
        )
    except Exception:
        logger.debug("L1 record_run_inefficiencies skipped", exc_info=True)

    return False, compile_fix_session_id


def _run_generation_compile_gate(
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    client: OpenCodeClient,
    maven_module_flag: str,
    generation_session_id: Optional[str] = None,
    target_context_paths: Optional[Dict[str, Dict[str, str]]] = None,
    max_fix_attempts: int = 3,
    session_id: Optional[str] = None,
) -> tuple[bool, float, Optional[str]]:
    """Enforce compilation before generation is considered complete."""
    generation_session_id = generation_session_id or session_id or ""
    logger.info("Step 2: Compile gate before accepting generation output")
    _set_stage(state, "compile_verification", f"batch={len(batch)}")
    compile_started = time.perf_counter()
    compile_ok, compile_fix_session_id = run_compile_fix_loop(
        repo_path=repo_path,
        module=module,
        batch=batch,
        generation_session_id=generation_session_id,
        client=client,
        maven_module_flag=maven_module_flag,
        target_context_paths=target_context_paths,
        max_fix_attempts=max_fix_attempts,
        state=state,
    )
    compile_seconds = time.perf_counter() - compile_started
    return compile_ok, compile_seconds, compile_fix_session_id


def _format_compile_error_block(errors: List[Any], *, heading: Optional[str] = None) -> str:
    if not errors:
        return ""
    lines: List[str] = []
    if heading:
        lines.append(heading)
    for err in errors:
        line = f"- [{err.category.upper()}] {err.file}:{err.line}: {err.message}"
        if getattr(err, "symbol", None):
            line += f" ({err.symbol})"
        lines.append(line)
        for detail in list(getattr(err, "detail", ()) or ())[:4]:
            lines.append(f"  {detail}")
    return "\n".join(lines)


def _render_compile_fix_feedback(
    current_errors: List[Any],
    *,
    new_errors: Optional[List[Any]] = None,
    recurring_errors: Optional[List[Any]] = None,
) -> str:
    if not current_errors:
        return ""
    if new_errors is None or recurring_errors is None:
        return _format_compile_error_block(current_errors, heading="ALL CURRENT COMPILATION ERRORS")

    sections: List[str] = [
        "Fix the full current compile-error set below. Do not focus only on the first error.",
    ]
    if new_errors:
        sections.append(_format_compile_error_block(new_errors, heading="NEW ERROR CLUSTERS"))
    if recurring_errors:
        sections.append(_format_compile_error_block(recurring_errors, heading="RECURRING ERROR CLUSTERS"))
    return "\n\n".join(section for section in sections if section.strip())


def _expected_test_paths_for_batch(module: Optional[str], batch: List[str]) -> List[str]:
    module_prefix = f"{module}/" if module else ""
    paths = []
    for fqn in batch:
        test_name = f"{fqn.split('.')[-1]}Test"
        package_path = fqn.rsplit(".", 1)[0].replace(".", "/")
        paths.append(f"{module_prefix}src/test/java/{package_path}/{test_name}.java")
    return paths


def _filter_compile_errors_to_paths(errors: List[Any], expected_paths: List[str]) -> List[Any]:
    normalized = [path.replace("\\", "/").lstrip("/") for path in expected_paths]
    filtered = []
    for error in errors:
        error_file = str(getattr(error, "file", "") or "").replace("\\", "/")
        if any(error_file.endswith(path) for path in normalized):
            filtered.append(error)
    return filtered


def _refresh_test_failure_summary(
    repo_path: str,
    test_selector: str,
    module: Optional[str],
    fallback_output: str,
) -> str:
    test_classes = [name.strip() for name in test_selector.split(",") if name.strip()]
    if not test_classes:
        return fallback_output

    try:
        results = parse_surefire_results(repo_path, test_classes, module)
    except Exception:
        logger.warning("Failed to refresh Surefire failure summary for %s", test_selector, exc_info=True)
        return fallback_output

    if not results:
        return fallback_output

    failed_sections: List[str] = []
    for test_class in test_classes:
        result = results.get(test_class)
        if not result or result.get("passed", False):
            continue
        output = (result.get("output") or "").strip()
        if output:
            failed_sections.append(f"## {test_class}\n{output}")
        else:
            failed_sections.append(f"## {test_class}\nSurefire reported failures but did not capture details.")

    return "\n\n".join(failed_sections).strip() or fallback_output


def _run_test_selector(
    repo_path: str,
    test_selector: str,
    module: Optional[str] = None,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Run one or more tests selected by ``-Dtest=...`` and return success plus concise failure text."""
    cmd = [
        uta_settings.maven_bin,
        "test",
        f"-Dtest={test_selector}",
        "-DfailIfNoTests=false",
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "-DskipTests=false",
        "-Dmaven.test.skip=false",
    ]
    if module:
        cmd.extend(["-pl", module, "-am"])
    try:
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            stdout = result.stdout.decode(errors="replace") if result.stdout else ""
            error_text = stderr or stdout
            lines = error_text.split("\n")
            error_lines = [
                l for l in lines
                if "[ERROR]" in l or "FAILURE" in l or "Tests run:" in l or "Failed tests:" in l
            ]
            return False, "\n".join(error_lines[-40:]) if error_lines else error_text[-2500:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Test execution timed out"


def run_coverage_fix_loop(
    repo_path: str,
    module: Optional[str],
    class_fqn: str,
    session_id: str,
    client: OpenCodeClient,
    test_class_name: str,
    test_file_abs: Path,
    source_file_abs: Optional[Path],
    coverage_gate: int,
    current_coverage: float,
    maven_module_flag: str,
    target_context_abs: str = "",
    target_symbols_abs: str = "",
    max_fix_attempts: int = 2,
    roi_abs: str = "",
    graph: Optional[Any] = None,
    state: Optional[AgentState] = None,
) -> tuple[bool, float, str, List[str]]:
    """Improve coverage until the gate is met, tests fail, or attempts are exhausted."""
    line_cov = current_coverage
    last_output = ""
    focused_session_ids: List[str] = []

    for attempt in range(1, max_fix_attempts + 1):
        if line_cov >= coverage_gate:
            return True, line_cov, last_output, focused_session_ids

        logger.info(
            "[%s] Coverage %.1f%% < gate %d%%, sending coverage-hardening prompt (attempt %d/%d)",
            class_fqn,
            line_cov,
            coverage_gate,
            attempt,
            max_fix_attempts,
        )

        uncovered_summary = {"methods": [], "line_clusters": []}
        jacoco_path = find_jacoco_report(repo_path, module)
        if jacoco_path:
            uncovered_summary = extract_uncovered_clusters(jacoco_path, class_fqn)
        roi_for_prompt = roi_abs
        if graph is not None and jacoco_path:
            try:
                from uta.language.java.scoring.coverage_roi import compute_class_roi, is_degenerate_roi_data

                refreshed_roi = compute_class_roi(class_fqn, graph, jacoco_xml_path=jacoco_path)
                if is_degenerate_roi_data(refreshed_roi):
                    logger.warning("[%s] Refreshed ROI data is degenerate; coverage-fix will ignore ROI guidance", class_fqn)
                    roi_for_prompt = ""
                else:
                    ctx_builder = ContextBuilder(repo_path, graph, [])
                    roi_for_prompt = ctx_builder.export_roi_scores(
                        class_fqn,
                        refreshed_roi,
                        source_path=str(source_file_abs or ""),
                        jacoco_xml_path=jacoco_path,
                        debug=uta_settings.roi_debug,
                    )
            except Exception:
                logger.warning("[%s] Failed to refresh ROI scores for coverage fix", class_fqn, exc_info=True)
                roi_for_prompt = ""
        elif roi_abs:
            try:
                from uta.language.java.scoring.coverage_roi import is_degenerate_roi_markdown

                if Path(roi_abs).exists():
                    existing_roi = Path(roi_abs).read_text(encoding="utf-8", errors="replace")
                    if is_degenerate_roi_markdown(existing_roi):
                        logger.warning("[%s] Existing ROI artifact is degenerate; coverage-fix will ignore ROI guidance", class_fqn)
                        roi_for_prompt = ""
            except Exception:
                logger.warning("[%s] Failed to validate existing ROI artifact", class_fqn, exc_info=True)
                roi_for_prompt = ""
        focused_session_id = _run_focused_coverage_fix_round(
            repo_path=repo_path,
            class_fqn=class_fqn,
            session_client=client,
            source_path=str(source_file_abs) if source_file_abs else "",
            test_file_path=str(test_file_abs),
            target_context_abs=target_context_abs,
            target_symbols_abs=target_symbols_abs,
            current_coverage=line_cov,
            coverage_gate=coverage_gate,
            test_class_name=test_class_name,
            maven_module_flag=maven_module_flag,
            attempt=attempt,
            uncovered_summary_md=format_uncovered_clusters_markdown(uncovered_summary),
            roi_abs=roi_for_prompt,
            state=state,
        )
        if focused_session_id and focused_session_id not in focused_session_ids:
            focused_session_ids.append(focused_session_id)

        test_ok, last_output = _run_test(repo_path, test_class_name, module)
        if not test_ok:
            logger.warning(
                "[%s] Targeted test run failed after coverage hardening; entering focused test-fix before coverage verification",
                class_fqn,
            )
            test_ok, last_output, coverage_test_fix_session_id = _run_coverage_test_fix_loop(
                repo_path=repo_path,
                module=module,
                class_fqn=class_fqn,
                test_class_name=test_class_name,
                current_output=last_output,
                client=client,
                maven_module_flag=maven_module_flag,
                state=state,
            )
            if coverage_test_fix_session_id and coverage_test_fix_session_id not in focused_session_ids:
                focused_session_ids.append(coverage_test_fix_session_id)
            if not test_ok:
                return False, line_cov, last_output, focused_session_ids

        test_ok, last_output = run_test_with_jacoco(repo_path, test_class_name, module)
        if not test_ok:
            logger.warning(
                "[%s] Coverage verification test run failed after hardening; entering focused test-fix before giving up",
                class_fqn,
            )
            test_ok, last_output, coverage_test_fix_session_id = _run_coverage_test_fix_loop(
                repo_path=repo_path,
                module=module,
                class_fqn=class_fqn,
                test_class_name=test_class_name,
                current_output=last_output,
                client=client,
                maven_module_flag=maven_module_flag,
                state=state,
            )
            if coverage_test_fix_session_id and coverage_test_fix_session_id not in focused_session_ids:
                focused_session_ids.append(coverage_test_fix_session_id)
            if not test_ok:
                return False, line_cov, last_output, focused_session_ids
            test_ok, last_output = run_test_with_jacoco(repo_path, test_class_name, module)
            if not test_ok:
                return False, line_cov, last_output, focused_session_ids

        jacoco_path = find_jacoco_report(repo_path, module)
        if jacoco_path:
            cov_stats = parse_jacoco_report(jacoco_path, class_fqn)
            line_cov = cov_stats.get("line", 0.0)
            logger.info("[%s] Coverage after hardening attempt %d: %.1f%%", class_fqn, attempt, line_cov)

    return line_cov >= coverage_gate, line_cov, last_output, focused_session_ids


def _run_coverage_test_fix_loop(
    *,
    repo_path: str,
    module: Optional[str],
    class_fqn: str,
    test_class_name: str,
    current_output: str,
    client: OpenCodeClient,
    maven_module_flag: str,
    max_fix_attempts: int = 2,
    state: Optional[AgentState] = None,
) -> tuple[bool, str, Optional[str]]:
    """Repair a test that broke during coverage verification before aborting coverage hardening."""
    test_selector = test_class_name
    test_fix_session_id: Optional[str] = None
    progress = None
    last_output = current_output

    for attempt in range(max_fix_attempts):
        last_output = _refresh_test_failure_summary(repo_path, test_selector, module, last_output)
        logger.warning(
            "[%s] Coverage test-fix attempt %d/%d",
            class_fqn,
            attempt + 1,
            max_fix_attempts,
        )
        if not test_fix_session_id:
            test_fix_session_id = client.create_session(model_id=uta_settings.opencode_model)
            progress = _session_progress_logger([class_fqn], session_id=test_fix_session_id, stage="coverage_test_fix")

        test_file_path = _expected_test_file_rel(module, class_fqn)
        prompt = (
            f"The coverage-hardening turn made `{test_file_path}` fail during the Jacoco verification run.\n\n"
            f"### TEST FAILURES\n```\n{last_output}\n```\n\n"
            "Fix the existing test file in this same session. Do NOT create a new file. "
            "Do NOT inspect JaCoCo or coverage artifacts. Fix the full current failing suite below, not just the first failing test. "
            "If several failures share setup or stubbing, fix the shared seam first. Restore passing targeted tests first.\n"
            f"After fixing, run: mvn test -Dtest={test_selector} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
        )
        prompt += _stage_introspect_section(repo_path, "test_fix")
        patch_count_before = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else 0
        client.send_message(test_fix_session_id, prompt, model_id=uta_settings.opencode_model)
        event = _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=client,
                session_id=test_fix_session_id,
                timeout=_llm_repair_timeout(uta_settings.opencode_model),
                phase="test_fix",
                batch=[class_fqn],
                on_update=progress if uta_settings.opencode_stream_progress else None,
                state=state,
            ),
            session_id=test_fix_session_id,
            client=client,
            phase="test_fix",
        )
        if _event_needs_fresh_session(event):
            logger.warning(
                "Coverage test-fix session %s ended with %s; starting a fresh repair session on the next attempt",
                test_fix_session_id,
                event.get("type"),
            )
            test_fix_session_id = None
            progress = None
            continue
        while True:
            patch_count_after = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else patch_count_before
            test_ok, last_output = _run_test_selector(repo_path, test_selector, module=module)
            if test_ok:
                return True, "", test_fix_session_id
            last_output = _refresh_test_failure_summary(repo_path, test_selector, module, last_output)
            if patch_count_after <= patch_count_before:
                break
            followup = _test_fix_continue_prompt(
                scope_label=f"`{test_file_path}` after coverage hardening",
                failures=last_output,
                test_selector=test_selector,
                maven_module_flag=maven_module_flag,
            )
            client.send_message(test_fix_session_id, followup, model_id=uta_settings.opencode_model)
            event = _raise_for_rate_limit_event(
                event=_poll_with_continue_recovery(
                    client=client,
                    session_id=test_fix_session_id,
                    timeout=_llm_repair_timeout(uta_settings.opencode_model),
                    phase="test_fix",
                    batch=[class_fqn],
                    on_update=progress if uta_settings.opencode_stream_progress else None,
                    state=state,
                ),
                session_id=test_fix_session_id,
                client=client,
                phase="test_fix",
            )
            if _event_needs_fresh_session(event):
                logger.warning(
                    "Coverage test-fix session %s ended with %s during in-session continuation; starting a fresh repair session on the next attempt",
                    test_fix_session_id,
                    event.get("type"),
                )
                test_fix_session_id = None
                progress = None
                continue
            patch_count_before = patch_count_after

    return False, last_output, test_fix_session_id


def _run_mutation_test_fix_loop(
    *,
    repo_path: str,
    module: Optional[str],
    class_fqn: str,
    test_class_name: str,
    current_output: str,
    client: OpenCodeClient,
    maven_module_flag: str,
    max_fix_attempts: int = 2,
    state: Optional[AgentState] = None,
) -> tuple[bool, str, Optional[str]]:
    """Repair a test that PIT reported as non-green before mutation analysis."""
    test_selector = test_class_name
    test_fix_session_id: Optional[str] = None
    progress = None
    last_output = current_output

    for attempt in range(max_fix_attempts):
        last_output = _refresh_test_failure_summary(repo_path, test_selector, module, last_output)
        logger.warning(
            "[%s] Mutation test-fix attempt %d/%d",
            class_fqn,
            attempt + 1,
            max_fix_attempts,
        )
        if not test_fix_session_id:
            test_fix_session_id = client.create_session(model_id=uta_settings.opencode_model)
            progress = _session_progress_logger([class_fqn], session_id=test_fix_session_id, stage="mutation_test_fix")

        test_file_path = _expected_test_file_rel(module, class_fqn)
        prompt = (
            f"PIT could not start mutation analysis because `{test_file_path}` is not green under PIT's precheck.\n\n"
            f"### PIT PRECHECK FAILURE\n```\n{last_output}\n```\n\n"
            "Fix the existing test file in this same session. Do NOT create a new file. "
            "Fix the full current failing suite below, not just the first failing test. If several failures share setup or stubbing, "
            "fix the shared seam first. Restore a robust green suite first. Prefer value-based numeric assertions over scale- or formatting-sensitive "
            "equality when the behavior is numeric (for example `Scale6Decimal` / `BigDecimal` values like `5` vs `5.000000`).\n"
            f"After fixing, run: mvn test -Dtest={test_selector} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
        )
        prompt += _stage_introspect_section(repo_path, "test_fix")
        patch_count_before = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else 0
        client.send_message(test_fix_session_id, prompt, model_id=uta_settings.opencode_model)
        event = _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=client,
                session_id=test_fix_session_id,
                timeout=_llm_repair_timeout(uta_settings.opencode_model),
                phase="test_fix",
                batch=[class_fqn],
                on_update=progress if uta_settings.opencode_stream_progress else None,
                state=state,
            ),
            session_id=test_fix_session_id,
            client=client,
            phase="test_fix",
        )
        if _event_needs_fresh_session(event):
            logger.warning(
                "Mutation test-fix session %s ended with %s; starting a fresh repair session on the next attempt",
                test_fix_session_id,
                event.get("type"),
            )
            test_fix_session_id = None
            progress = None
            continue
        while True:
            patch_count_after = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else patch_count_before
            test_ok, last_output = _run_test_selector(repo_path, test_selector, module=module)
            if test_ok:
                return True, "", test_fix_session_id
            last_output = _refresh_test_failure_summary(repo_path, test_selector, module, last_output)
            if patch_count_after <= patch_count_before:
                break
            followup = _test_fix_continue_prompt(
                scope_label=f"`{test_file_path}` during PIT precheck repair",
                failures=last_output,
                test_selector=test_selector,
                maven_module_flag=maven_module_flag,
            )
            client.send_message(test_fix_session_id, followup, model_id=uta_settings.opencode_model)
            event = _raise_for_rate_limit_event(
                event=_poll_with_continue_recovery(
                    client=client,
                    session_id=test_fix_session_id,
                    timeout=_llm_repair_timeout(uta_settings.opencode_model),
                    phase="test_fix",
                    batch=[class_fqn],
                    on_update=progress if uta_settings.opencode_stream_progress else None,
                    state=state,
                ),
                session_id=test_fix_session_id,
                client=client,
                phase="test_fix",
            )
            if _event_needs_fresh_session(event):
                logger.warning(
                    "Mutation test-fix session %s ended with %s during in-session continuation; starting a fresh repair session on the next attempt",
                    test_fix_session_id,
                    event.get("type"),
                )
                test_fix_session_id = None
                progress = None
                continue
            patch_count_before = patch_count_after

    return False, last_output, test_fix_session_id


def _compile_test(repo_path: str, module: str = None, timeout: int = 300) -> tuple:
    """Run mvn test-compile independently. Returns (success, error_output)."""
    cmd = [uta_settings.maven_bin, "test-compile", "-DskipTests"]
    if module:
        cmd.extend(["-pl", module, "-am"])
    try:
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            stdout = result.stdout.decode(errors="replace") if result.stdout else ""
            # Extract the most useful error lines
            error_text = stderr or stdout
            # Find compilation error section
            lines = error_text.split("\n")
            error_lines = [l for l in lines if "[ERROR]" in l]
            return False, "\n".join(error_lines[-30:]) if error_lines else error_text[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out"


def _expected_test_file_rel(module: Optional[str], class_fqn: str) -> str:
    test_class_name = f"{class_fqn.split('.')[-1]}Test"
    package_path = class_fqn.rsplit(".", 1)[0].replace(".", "/")
    module_prefix = f"{module}/" if module else ""
    return f"{module_prefix}src/test/java/{package_path}/{test_class_name}.java"


def _generation_plan_path(repo_path: str) -> Path:
    return Path(repo_path) / ".uta_cache" / "context" / "latest_generation_plan.md"


def _generation_plan_candidate_path(repo_path: str) -> Path:
    return Path(repo_path) / ".uta_cache" / "context" / "latest_generation_plan.candidate.md"


def _clear_generation_plan(repo_path: str) -> None:
    for plan_path in (_generation_plan_path(repo_path), _generation_plan_candidate_path(repo_path)):
        try:
            plan_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to clear stale generation plan artifact: %s", plan_path, exc_info=True)


def _write_generation_plan(repo_path: str, session_id: str, batch: List[str], plan_text: str) -> str:
    ctx_dir = _generation_plan_path(repo_path).parent
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = _generation_plan_path(repo_path)
    lines = [
        "# Latest Generation Plan",
        "",
        f"- session_id: `{session_id}`",
        f"- classes: `{', '.join(batch)}`",
        "",
        plan_text.strip() or "_No plan text captured._",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    try:
        _generation_plan_candidate_path(repo_path).unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to clear candidate generation plan after final write", exc_info=True)
    return str(out.resolve())


def _write_generation_plan_candidate(
    repo_path: str,
    session_id: str,
    batch: List[str],
    plan_text: str,
    replan_reasons: List[str],
) -> str:
    ctx_dir = _generation_plan_candidate_path(repo_path).parent
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = _generation_plan_candidate_path(repo_path)
    lines = [
        "# Candidate Generation Plan",
        "",
        f"- session_id: `{session_id}`",
        f"- classes: `{', '.join(batch)}`",
        "- status: `candidate-before-replan`",
        "",
        "## Replan Reasons",
        "",
        *(f"- {reason}" for reason in replan_reasons),
        "",
        "## Plan",
        "",
        plan_text.strip() or "_No plan text captured._",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out.resolve())


def _extract_plan_body_from_artifact(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "# Latest Generation Plan":
        body_started = False
        body: List[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if not body_started:
                if stripped.startswith("- session_id:") or stripped.startswith("- classes:") or stripped == "":
                    continue
                body_started = True
            if body_started:
                body.append(line)
        text = "\n".join(body).strip()
    elif lines and lines[0].strip() == "# Candidate Generation Plan":
        body_started = False
        body: List[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if not body_started:
                if (
                    stripped.startswith("- session_id:")
                    or stripped.startswith("- classes:")
                    or stripped.startswith("- status:")
                    or stripped == ""
                    or stripped == "## Replan Reasons"
                    or stripped == "## Plan"
                    or stripped.startswith("- ")
                ):
                    continue
                body_started = True
            if body_started:
                body.append(line)
        text = "\n".join(body).strip()
    if text == "_No plan text captured._":
        return ""
    return text


def _generation_plan_artifact_session_id(content: str) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- session_id:"):
            continue
        return stripped.removeprefix("- session_id:").strip().strip("`").strip()
    return ""


def _generation_plan_artifact_classes(content: str) -> List[str]:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- classes:"):
            continue
        raw = stripped.removeprefix("- classes:").strip().strip("`").strip()
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _load_generation_plan_for_resume(repo_path: str, batch: List[str]) -> str:
    artifacts = [
        (_generation_plan_path(repo_path), "generation plan"),
        (_generation_plan_candidate_path(repo_path), "candidate generation plan"),
    ]
    for plan_path, label in artifacts:
        if not plan_path.exists():
            continue
        try:
            content = plan_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.warning("Resume requested but %s artifact could not be read: %s", label, plan_path, exc_info=True)
            continue

        artifact_classes = _generation_plan_artifact_classes(content)
        if artifact_classes and artifact_classes != batch:
            logger.warning(
                "Resume requested but %s classes %s do not match current batch %s; ignoring artifact",
                label,
                artifact_classes,
                batch,
            )
            continue
        return _extract_plan_body_from_artifact(content)

    logger.warning(
        "Resume requested but neither final nor candidate generation plan artifact exists for batch %s",
        batch,
    )
    return ""


_MAX_PLAN_CHARS = 3000  # target cap for compressed plan in generation prompt


def _compress_plan_for_generation(plan_text: str) -> str:
    """Compress verbose plan prose into a compact generation-ready summary (strategy I).

    Tries three passes in order of decreasing fidelity:
    1. Extract structured PLANNED TESTS items + WAVE assignments into a table.
    2. Extract just the section headers + bullet points (strip long explanations).
    3. Hard-truncate to _MAX_PLAN_CHARS with a note.

    If the plan is already short enough, returns it unchanged.
    """
    if not plan_text or len(plan_text) <= _MAX_PLAN_CHARS:
        return plan_text

    # Pass 1: extract structured items.
    compressed = _extract_planned_tests_table(plan_text)
    if compressed and len(compressed) <= _MAX_PLAN_CHARS:
        return compressed

    # Pass 2: strip long prose, keep bullet/heading structure.
    compressed = _strip_plan_prose(plan_text)
    if len(compressed) <= _MAX_PLAN_CHARS:
        return compressed

    # Pass 3: truncate with note.
    return (
        plan_text[: _MAX_PLAN_CHARS - 80].rstrip()
        + "\n\n... [plan truncated for token efficiency; see plan file for full details]"
    )


def _extract_planned_tests_table(plan_text: str) -> str:
    """Extract test-method entries from plan text into a compact table.

    Looks for lines like:
      - `testMethodName` — covers branch X (wave 1)
      - testFooBar: covers guard clause (WAVE 1)
    Returns empty string if no such entries are found.
    """
    # Match lines starting with '- ' that look like test entries.
    test_entry_re = re.compile(
        r"^\s*[-*]\s+[`\"]?(?P<name>test\w+)[`\"]?\s*[:\-–—]?\s*(?P<desc>[^\n]{0,120})",
        re.IGNORECASE | re.MULTILINE,
    )
    wave_re = re.compile(r"\bwave\s*[12]\b", re.IGNORECASE)

    rows: List[str] = []
    for m in test_entry_re.finditer(plan_text):
        name = m.group("name").strip("`\"")
        desc = m.group("desc").strip()
        wave = "W1" if wave_re.search(m.group(0)) and "1" in (wave_re.search(m.group(0)) or re.match("", "")).group(0) else (
            "W2" if "wave 2" in m.group(0).lower() else "W1"
        )
        # Trim desc
        if len(desc) > 80:
            desc = desc[:77] + "..."
        rows.append(f"| `{name}` | {desc} | {wave} |")

    if not rows:
        return ""
    header = "| Test method | Description | Wave |\n|---|---|---|"
    return header + "\n" + "\n".join(rows)


def _strip_plan_prose(plan_text: str) -> str:
    """Keep section headers and bullet points; strip long explanatory paragraphs."""
    lines = plan_text.splitlines()
    out: List[str] = []
    consecutive_blank = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            consecutive_blank += 1
            if consecutive_blank <= 1:
                out.append("")
            continue
        consecutive_blank = 0
        # Keep headings and bullet points; skip long prose lines.
        if stripped.startswith("#") or stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("|"):
            out.append(line)
        elif len(stripped) <= 100:
            out.append(line)
        # Else skip long prose
    return "\n".join(out).strip()


def _recover_plan_text_from_session_artifact(
    *,
    repo_path: str,
    session_id: str,
    client: Any,
) -> str:
    plan_path = _generation_plan_path(repo_path)
    candidate_path = _generation_plan_candidate_path(repo_path)
    try:
        messages = client.get_messages(session_id)
    except Exception:
        messages = []

    plan_touched = False
    targets = {
        str(plan_path.resolve()): plan_path,
        str(candidate_path.resolve()): candidate_path,
    }
    for msg in messages:
        info = msg.get("info", {}) or {}
        if info.get("role") != "assistant":
            continue
        for part in msg.get("parts", []):
            if part.get("type") != "patch":
                continue
            files = part.get("files") or []
            for file_path in files:
                file_str = str(file_path)
                if (
                    file_str in targets
                    or file_str.endswith("/latest_generation_plan.md")
                    or file_str.endswith("/latest_generation_plan.candidate.md")
                ):
                    plan_touched = True
                    break
            if plan_touched:
                break
        if plan_touched:
            break

    if not plan_touched:
        return ""

    for artifact_path in (plan_path, candidate_path):
        if not artifact_path.exists():
            continue
        try:
            content = artifact_path.read_text(encoding="utf-8", errors="replace")
            artifact_session_id = _generation_plan_artifact_session_id(content)
            if artifact_session_id and artifact_session_id != session_id:
                logger.warning(
                    "Ignoring generation plan artifact for stale session %s while recovering session %s",
                    artifact_session_id,
                    session_id,
                )
                continue
            return _extract_plan_body_from_artifact(content)
        except Exception:
            continue
    return ""


def _write_context_artifact(repo_path: str, filename: str, content: str) -> str:
    ctx_dir = Path(repo_path) / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = ctx_dir / filename
    out.write_text(content, encoding="utf-8")
    return str(out.resolve())


def _filter_mutation_families_by_roi(
    families: List[Dict[str, Any]],
    *,
    max_families: int = 15,
    skip_expensive: bool = True,
) -> List[Dict[str, Any]]:
    """Retain only the top-ROI mutation families for the fix prompt (strategy F).

    Drops:
    - Families marked ``likely_equivalent`` (unobservable mutations).
    - ``effort_band=expensive`` families when ``skip_expensive=True``.
    Keeps at most ``max_families`` entries in ROI rank order.
    """
    filtered = [
        fam for fam in families
        if not fam.get("likely_equivalent")
        and not (skip_expensive and fam.get("effort_band") == "expensive")
    ]
    return filtered[:max_families]


def _flatten_mutation_family_examples(families: List[Dict[str, Any]], max_examples: int = 12) -> List[Dict[str, Any]]:
    examples: List[Dict[str, Any]] = []
    for family in families:
        for example in family.get("examples", []):
            examples.append(example)
            if len(examples) >= max_examples:
                return examples
    return examples


def _session_patch_count(session_client: Any, session_id: str) -> int:
    def _part_is_successful_patch(part: Dict[str, Any]) -> bool:
        ptype = part.get("type") or ""
        if ptype == "patch":
            return True
        if ptype != "tool" or part.get("tool") != "apply_patch":
            return False
        state = part.get("state") or {}
        output = str(state.get("output") or "")
        return state.get("status") == "completed" and "Success." in output

    stream_patch_count = 0
    stream_counter = getattr(session_client, "get_session_patch_count", None)
    if callable(stream_counter):
        try:
            stream_patch_count = int(stream_counter(session_id) or 0)
        except Exception:
            stream_patch_count = 0

    try:
        messages = session_client.get_messages(session_id)
    except Exception:
        return stream_patch_count

    patch_count = 0
    for msg in messages:
        for part in msg.get("parts", []):
            if _part_is_successful_patch(part):
                patch_count += 1
    return max(patch_count, stream_patch_count)


def _test_fix_continue_prompt(
    *,
    scope_label: str,
    failures: str,
    test_selector: str,
    maven_module_flag: str,
) -> str:
    return (
        f"The latest targeted rerun for {scope_label} still fails after your last repair in this same session.\n\n"
        f"### REMAINING TEST FAILURES\n```\n{failures}\n```\n\n"
        "Continue fixing the existing generated test file(s) in this same session. "
        "Do NOT create new files. Do NOT stop after fixing only the first visible seam. "
        "Patch the full current failure family before rerunning, including overload variants or shared route/mock seams.\n"
        f"After fixing, run: mvn test -Dtest={test_selector} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
    )


def _run_focused_mutation_fix_round(
    *,
    repo_path: str,
    module: Optional[str],
    class_fqn: str,
    session_client: OpenCodeClient,
    source_file_abs: Optional[Path],
    test_file_abs: Path,
    target_context_abs: str,
    target_symbols_abs: str,
    current_coverage: float,
    mutation_gate_score: int,
    attempt: int,
    mutation_score: float,
    mutation_stats: Dict[str, Any],
    report_path: str,
    method_efforts: Optional[List[Dict[str, Any]]] = None,
    state: Optional[AgentState] = None,
) -> Dict[str, Any]:
    """Run one mutation-fix round in a fresh focused OpenCode session."""
    mutation_roi_enabled = bool(uta_settings.mutation_roi_enabled) and method_efforts is not None
    families = summarize_surviving_mutants(
        report_path,
        class_fqn,
        method_efforts=method_efforts if mutation_roi_enabled else None,
    )
    # Strategy F: filter to top-ROI families only to avoid huge prompts on large classes.
    skip_expensive = bool(uta_settings.mutation_roi_skip_expensive)
    if mutation_roi_enabled:
        families = _filter_mutation_families_by_roi(families, skip_expensive=skip_expensive)
    family_summary = format_mutation_families_markdown(families)
    family_summary_abs = _write_context_artifact(
        repo_path,
        f"{class_fqn.split('.')[-1]}.mutation_families.md",
        "# Mutation Survivor Families\n\n" + family_summary,
    )

    from uta.prompts.loader import render_prompt_split as _render_split
    _mut_kwargs = dict(
        class_fqn=class_fqn,
        current_coverage=current_coverage,
        mutation_gate=mutation_gate_score,
        current_mutation_score=mutation_score,
        mutation_stats=mutation_stats,
        surviving_mutants=_flatten_mutation_family_examples(families),
        mutation_family_summary=family_summary,
        mutation_family_summary_abs=family_summary_abs,
        source_path=str(source_file_abs) if source_file_abs else "",
        test_file_path=str(test_file_abs),
        target_context_abs=target_context_abs,
        target_symbols_abs=target_symbols_abs,
        stage_introspect_abs=ensure_stage_introspect_file(repo_path, "mutation_fix"),
        mutation_roi_enabled=mutation_roi_enabled,
        mutation_roi_skip_expensive=bool(uta_settings.mutation_roi_skip_expensive),
    )
    _mut_stable, _mut_volatile = _render_split("fix_mutations", **_mut_kwargs)

    from uta.opencode.tiered_router import effective_model as _effective_model
    mutation_fix_model = _effective_model("mutation_fix")
    focused_session_id = session_client.create_session(model_id=mutation_fix_model)
    progress = _session_progress_logger([class_fqn], session_id=focused_session_id, stage=f"mutation_fix_round_{attempt}")
    try:
        session_client.send_message_split(
            focused_session_id,
            _mut_stable,
            _mut_volatile,
            model_id=mutation_fix_model,
        )
        event = _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=session_client,
                session_id=focused_session_id,
                timeout=_llm_repair_timeout(mutation_fix_model),
                phase="mutation_fix",
                batch=[class_fqn],
                model_id=mutation_fix_model,
                on_update=progress if uta_settings.opencode_stream_progress else None,
                state=state,
            ),
            session_id=focused_session_id,
            client=session_client,
            phase="mutation_fix",
        )
        _log_nonterminal_event(
            phase="Focused mutation-fix",
            session_id=focused_session_id,
            event=event,
            class_fqn=class_fqn,
        )
        patch_count = _session_patch_count(session_client, focused_session_id)
    finally:
        _cleanup_focused_session(session_client, focused_session_id)
    return {
        "session_id": focused_session_id,
        "patched": patch_count > 0,
        "patch_count": patch_count,
        "event_type": event.get("type"),
        "family_count": len(families),
        "ranked_methods": [family.get("method", "") for family in families if family.get("method")],
    }


def _run_focused_coverage_fix_round(
    *,
    repo_path: str = "",
    class_fqn: str,
    session_client: OpenCodeClient,
    source_path: str,
    test_file_path: str,
    target_context_abs: str,
    target_symbols_abs: str,
    current_coverage: float,
    coverage_gate: int,
    test_class_name: str,
    maven_module_flag: str,
    attempt: int,
    uncovered_summary_md: str,
    roi_abs: str = "",
    state: Optional[AgentState] = None,
) -> str:
    """Run one coverage-fix round in a fresh focused OpenCode session."""
    from uta.prompts.loader import render_prompt_split as _render_split
    _cov_kwargs = dict(
        class_fqn=class_fqn,
        current_coverage=current_coverage,
        coverage_gate=coverage_gate,
        source_path=source_path,
        test_file_path=test_file_path,
        test_class_name=test_class_name,
        maven_module_flag=maven_module_flag,
        target_context_abs=target_context_abs,
        target_symbols_abs=target_symbols_abs,
        uncovered_summary=uncovered_summary_md,
        roi_abs=roi_abs,
        stage_introspect_abs=ensure_stage_introspect_file(repo_path or str(Path(test_file_path).parent), "coverage_fix"),
    )
    _cov_stable, _cov_volatile = _render_split("fix_coverage", **_cov_kwargs)

    from uta.opencode.tiered_router import effective_model as _effective_model
    _coverage_model = _effective_model("coverage_fix")
    focused_session_id = session_client.create_session(model_id=_coverage_model)
    progress = _session_progress_logger([class_fqn], session_id=focused_session_id, stage=f"coverage_fix_round_{attempt}")
    try:
        session_client.send_message_split(focused_session_id, _cov_stable, _cov_volatile,
                                           model_id=_coverage_model)
        event = _raise_for_rate_limit_event(
            event=_poll_with_continue_recovery(
                client=session_client,
                session_id=focused_session_id,
                timeout=_llm_repair_timeout(_coverage_model),
                phase="coverage_fix",
                batch=[class_fqn],
                model_id=_coverage_model,
                on_update=progress if uta_settings.opencode_stream_progress else None,
                stalled_no_progress_seconds=_llm_stalled_no_progress_seconds(),
                state=state,
            ),
            session_id=focused_session_id,
            client=session_client,
            phase="coverage_fix",
        )
        _log_nonterminal_event(
            phase="Focused coverage-fix",
            session_id=focused_session_id,
            event=event,
            class_fqn=class_fqn,
        )
    finally:
        _cleanup_focused_session(session_client, focused_session_id)
    return focused_session_id


def _prompt_for_missing_batch_files(
    *,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    session_id: str,
    client: OpenCodeClient,
    progress,
    state: Optional[AgentState] = None,
) -> List[str]:
    missing = []
    for class_fqn in batch:
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        if not (Path(repo_path) / test_file_rel).exists():
            missing.append(test_file_rel)

    if not missing:
        return []

    logger.warning("Batch generation completed with missing test files: %s", missing)
    prompt = (
        "The previous batch generation turn finished without creating all required test files.\n\n"
        "You must continue from the existing work and write the missing files below. "
        "Do not rewrite unrelated files. Finish the missing files first, then stop.\n\n"
        "### MISSING TEST FILES\n"
        + "\n".join(f"- `{path}`" for path in missing)
    )
    client.send_message(session_id, prompt, model_id=uta_settings.opencode_model)
    event = _raise_for_rate_limit_event(
        event=_poll_with_continue_recovery(
            client=client,
            session_id=session_id,
            timeout=_llm_timeout(1200, uta_settings.opencode_model),
            phase="generate",
            batch=batch,
            on_update=progress if uta_settings.opencode_stream_progress else None,
            state=state,
        ),
        session_id=session_id,
        client=client,
        phase="generate",
    )
    _log_nonterminal_event(
        phase="Missing-file recovery",
        session_id=session_id,
        event=event,
    )

    still_missing = []
    for path in missing:
        if not (Path(repo_path) / path).exists():
            still_missing.append(path)
    return still_missing


def _run_test(repo_path: str, test_class: str, module: str = None, timeout: int = 300) -> tuple:
    """Run a specific test class. Returns (success, error_output)."""
    return _run_test_selector(repo_path, test_class, module=module, timeout=timeout)


class _ExistingTestsPrechecker:
    """Strategy for deciding whether existing tests can satisfy a batch before LLM generation."""

    def run(
        self,
        *,
        state: AgentState,
        repo_path: str,
        module: Optional[str],
        batch: List[str],
        coverage_gate: int,
        mutation_gate_score: int,
        node_started: float,
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class _ClassLevelExistingTestsPrechecker(_ExistingTestsPrechecker):
    """Batch-mode precheck using targeted tests, JaCoCo, and class-level PIT evidence."""

    def run(
        self,
        *,
        state: AgentState,
        repo_path: str,
        module: Optional[str],
        batch: List[str],
        coverage_gate: int,
        mutation_gate_score: int,
        node_started: float,
    ) -> Optional[Dict[str, Any]]:
        return _precheck_existing_tests_class_level(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            coverage_gate=coverage_gate,
            mutation_gate_score=mutation_gate_score,
            node_started=node_started,
        )


class _DiffEnforcerExistingTestsPrechecker(_ExistingTestsPrechecker):
    """CI incremental precheck using the diff-based Maven test-enforcer plugin."""

    def run(
        self,
        *,
        state: AgentState,
        repo_path: str,
        module: Optional[str],
        batch: List[str],
        coverage_gate: int,
        mutation_gate_score: int,
        node_started: float,
    ) -> Optional[Dict[str, Any]]:
        return _precheck_existing_tests_diff_enforcer(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            coverage_gate=coverage_gate,
            mutation_gate_score=mutation_gate_score,
            node_started=node_started,
        )


def _existing_tests_prechecker_for(state: AgentState) -> _ExistingTestsPrechecker:
    if state.get("quality_mode") == "ci_incremental" and state.get("quality_gate_backend") == "maven_enforcer":
        return _DiffEnforcerExistingTestsPrechecker()
    return _ClassLevelExistingTestsPrechecker()


def _precheck_existing_tests_class_level(
    *,
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    coverage_gate: int,
    mutation_gate_score: int,
    node_started: float,
) -> Optional[Dict[str, Any]]:
    """Skip LLM work when existing test files already satisfy all gates."""
    test_files = {class_fqn: Path(repo_path) / _expected_test_file_rel(module, class_fqn) for class_fqn in batch}
    missing = [str(path) for path in test_files.values() if not path.exists()]
    if missing:
        logger.info("Existing-test precheck skipped; missing test file(s): %s", missing)
        return None

    logger.info("Prechecking existing tests before LLM work for batch=%d", len(batch))
    _set_stage(state, "precheck_existing_tests", f"batch={len(batch)}")
    precheck_started = time.perf_counter()
    test_names = [f"{class_fqn.split('.')[-1]}Test" for class_fqn in batch]
    test_ok, test_output = run_tests_with_jacoco_batch(repo_path, test_names, module)
    test_seconds = time.perf_counter() - precheck_started
    if not test_ok:
        logger.info("Existing-test precheck did not pass targeted tests; continuing with LLM workflow")
        return None

    jacoco_path = find_jacoco_report(repo_path, module)
    if not jacoco_path:
        logger.info("Existing-test precheck did not find a JaCoCo XML report; continuing with LLM workflow")
        return None

    surefire_results = parse_surefire_results(repo_path, test_names, module)
    coverage_by_class: Dict[str, float] = {}
    output_by_class: Dict[str, str] = {}
    for class_fqn, test_name in zip(batch, test_names):
        class_test = surefire_results.get(test_name, {})
        if not bool(class_test.get("passed", True)):
            logger.info("[%s] Existing-test precheck found failing Surefire result; continuing with LLM workflow", class_fqn)
            return None
        output_by_class[class_fqn] = class_test.get("output") or test_output or ""
        line_cov = float(parse_jacoco_report(jacoco_path, class_fqn).get("line", 0.0) or 0.0)
        coverage_by_class[class_fqn] = line_cov
        if line_cov < coverage_gate:
            logger.info(
                "[%s] Existing-test precheck coverage %.1f%% < gate %d%%; continuing with LLM workflow",
                class_fqn,
                line_cov,
                coverage_gate,
            )
            return None

    mutation_started = time.perf_counter()
    mutation_by_class: Dict[str, Dict[str, Any]] = {}
    if _should_run_mutation(True, mutation_gate_score):
        for class_fqn, test_name in zip(batch, test_names):
            test_class_fqn = f"{'.'.join(class_fqn.split('.')[:-1])}.{test_name}"
            pitest_ok, pitest_output = run_pitest(repo_path, class_fqn, test_class_fqn, module)
            if not pitest_ok:
                logger.info("[%s] Existing-test precheck PIT failed; continuing with LLM workflow: %s", class_fqn, pitest_output[:200])
                return None
            report_path = find_latest_pitest_report(repo_path, module)
            if not report_path:
                logger.info("[%s] Existing-test precheck did not find a PIT report; continuing with LLM workflow", class_fqn)
                return None
            stats = compute_mutation_stats(report_path, class_fqn)
            score = float(stats.get("score", 0.0) or 0.0)
            if score < mutation_gate_score:
                logger.info(
                    "[%s] Existing-test precheck mutation %.1f%% < gate %d%%; continuing with LLM workflow",
                    class_fqn,
                    score,
                    mutation_gate_score,
                )
                return None
            mutation_by_class[class_fqn] = stats
    mutation_seconds = time.perf_counter() - mutation_started

    logger.info("Existing-test precheck satisfied all gates for batch=%s; skipping LLM workflow", batch)
    new_results = state["results"].copy()
    session_ids = list(state.get("session_ids", []) or [])
    per_class_test_seconds = test_seconds / max(len(batch), 1)
    per_class_mutation_seconds = mutation_seconds / max(len(batch), 1)
    for class_fqn in batch:
        stats = mutation_by_class.get(class_fqn, {})
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        test_file_abs = Path(repo_path) / test_file_rel
        try:
            test_file_content = test_file_abs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            test_file_content = ""
        new_results[class_fqn] = {
            "status": "PASS",
            "coverage": coverage_by_class[class_fqn],
            "tests_pass": True,
            "mutation_score": float(stats.get("score", 0.0) or 0.0),
            "surviving_mutants": int(stats.get("survived", 0) or 0),
            "total_mutants": int(stats.get("total", 0) or 0),
            "killed_mutants": int(stats.get("killed", 0) or 0),
            "no_coverage_mutants": int(stats.get("no_coverage", 0) or 0),
            "timed_out_mutants": int(stats.get("timed_out", 0) or 0),
            "non_viable_mutants": int(stats.get("non_viable", 0) or 0),
            "memory_error_mutants": int(stats.get("memory_error", 0) or 0),
            "run_error_mutants": int(stats.get("run_error", 0) or 0),
            "mutation_status_counts": stats.get("status_counts", {}),
            "output": output_by_class.get(class_fqn, "")[:2000],
            "test_file_path": test_file_rel,
            "test_file_content": test_file_content,
            "elapsed_seconds": per_class_test_seconds + per_class_mutation_seconds,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": per_class_test_seconds,
            "mutation_seconds": per_class_mutation_seconds,
            "session_id": session_ids[-1] if session_ids else None,
            "session_ids": session_ids,
            "precheck_existing_tests": True,
        }

    return {
        "results": new_results,
        "current_batch": [],
        "current_class": None,
        "current_stage": "precheck_existing_tests",
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=time.perf_counter() - node_started,
            test_execution_seconds=test_seconds,
            mutation_seconds=mutation_seconds,
        ),
    }


def _precheck_existing_tests_diff_enforcer(
    *,
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    coverage_gate: int,
    mutation_gate_score: int,
    node_started: float,
) -> Optional[Dict[str, Any]]:
    """Run the diff-based Maven enforcer before LLM work for CI repair tasks."""
    logger.info("Prechecking existing tests with Maven diff enforcer before LLM work for batch=%d", len(batch))
    _set_stage(state, "precheck_existing_tests", f"maven_enforcer batch={len(batch)}")
    started = time.perf_counter()
    gate_result = _run_delegated_quality_gate_once(state, repo_path)
    gate_seconds = time.perf_counter() - started
    if gate_result.get("passed"):
        logger.info("Maven diff enforcer precheck passed; skipping LLM workflow")
        return {
            "results": _delegated_gate_batch_results(
                state=state,
                repo_path=repo_path,
                module=module,
                batch=batch,
                gate_result=gate_result,
                gate_seconds=gate_seconds,
                status="PASS",
                precheck_existing_tests=True,
            ),
            "current_batch": [],
            "current_class": None,
            "current_stage": "precheck_existing_tests",
            "finished": True,
            "stopped_early": True,
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                mutation_seconds=gate_seconds,
            ),
        }
    logger.info(
        "Maven diff enforcer precheck failed; routing repair from %s",
        _delegated_gate_failure_stage(gate_result),
    )
    return {
        "_precheck_action": "delegated_repair",
        "delegated_quality_gate": gate_result,
        "delegated_quality_gate_seconds": gate_seconds,
    }


def _precheck_existing_tests(
    *,
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    coverage_gate: int,
    mutation_gate_score: int,
    node_started: float,
) -> Optional[Dict[str, Any]]:
    return _existing_tests_prechecker_for(state).run(
        state=state,
        repo_path=repo_path,
        module=module,
        batch=batch,
        coverage_gate=coverage_gate,
        mutation_gate_score=mutation_gate_score,
        node_started=node_started,
    )


def _run_generation_test_gate(
    *,
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    generation_session_id: str,
    client: OpenCodeClient,
    maven_module_flag: str,
    max_fix_attempts: int = 3,
) -> tuple[bool, float, Optional[str]]:
    """Require targeted tests to pass before generation output is accepted."""
    logger.info("Step 2b: Test gate before accepting generation output")
    _set_stage(state, "test_verification", f"batch={len(batch)}")
    test_started = time.perf_counter()
    test_names = [f"{class_fqn.split('.')[-1]}Test" for class_fqn in batch]
    test_selector = ",".join(test_names)
    test_fix_session_id: Optional[str] = None
    progress = None
    fix_index_query_command = _index_query_command(module, section="fix_summary")

    attempt = 0
    rate_limit_hits = 0
    while attempt < max_fix_attempts:
        test_ok, test_output = _run_test_selector(repo_path, test_selector, module=module)
        if test_ok:
            logger.info("Generation test gate passed (attempt %d)", attempt + 1)
            return True, time.perf_counter() - test_started, test_fix_session_id
        test_output = _refresh_test_failure_summary(repo_path, test_selector, module, test_output)

        logger.warning(
            "Generation test gate failed (attempt %d/%d), sending fix prompt",
            attempt + 1,
            max_fix_attempts,
        )
        if not test_fix_session_id:
            test_fix_session_id = client.create_session(model_id=uta_settings.opencode_model)
            progress = _session_progress_logger(batch, session_id=test_fix_session_id, stage="generation_test_fix")
        if len(batch) == 1:
            first_fqn = batch[0]
            test_file_path = _expected_test_file_rel(module, first_fqn)
            prompt = (
                f"The generated test file `{test_file_path}` now compiles but the targeted test run still fails.\n\n"
                f"### TEST FAILURES\n```\n{test_output}\n```\n\n"
                "Fix the existing test file in this same session. Do NOT create a new file. "
                "Fix the full current failing suite below, not just the first failing test. "
                "If several failures share setup or stubbing, fix the shared seam first. "
                "Use the repo-local fix index before raw source hunting when you need an exact method/signature/import fact. "
                f"Preferred command: `{fix_index_query_command} --class-fqn {first_fqn} --method <METHOD> --symbol <SYMBOL> --json-output`. "
                "Do NOT re-open broad repo context unless a concrete signature or behavior fact is still missing.\n"
                f"After fixing, run: mvn test -Dtest={test_selector} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
            )
        else:
            prompt = (
                "The generated batch now compiles but the targeted batch test run still fails.\n\n"
                f"### TEST FAILURES\n```\n{test_output}\n```\n\n"
                "Fix the existing generated test files in this same session. Do NOT create new files. "
                "Fix the full current failing suite below, not just the first failing test. "
                "If several failures share setup or stubbing, fix the shared seam first. "
                f"Use the repo-local fix index first when you need an exact method/signature/import fact: `{fix_index_query_command} --class-fqn <CLASS> --method <METHOD> --symbol <SYMBOL> --json-output`. "
                "Do NOT restart broad exploration.\n"
                f"After fixing, run: mvn test -Dtest={test_selector} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
            )
        prompt += (
            "\n\n### GENERATION SESSION CONTEXT\n"
            f"- previous generation session: `{generation_session_id}`\n"
            "Continue from the current generated files on disk and the surefire failures above."
        )
        prompt += _stage_introspect_section(repo_path, "test_fix")
        patch_count_before = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else 0
        client.send_message(test_fix_session_id, prompt, model_id=uta_settings.opencode_model)
        try:
            event = _raise_for_rate_limit_event(
                event=_poll_with_continue_recovery(
                    client=client,
                    session_id=test_fix_session_id,
                    timeout=_llm_repair_timeout(uta_settings.opencode_model),
                    phase="test_fix",
                    batch=batch,
                    on_update=progress if uta_settings.opencode_stream_progress else None,
                    state=state,
                ),
                session_id=test_fix_session_id,
                client=client,
                phase="test_fix",
            )
        except ProviderRateLimitError:
            rate_limit_hits += 1
            logger.warning(
                "Generation test-fix hit provider/model rate limit (%d/%d transient gate retries); "
                "retrying without consuming the repair budget",
                rate_limit_hits,
                max_fix_attempts,
            )
            if rate_limit_hits >= max_fix_attempts:
                raise
            continue
        if _event_needs_fresh_session(event):
            logger.warning(
                "Generation test-fix session %s ended with %s; starting a fresh repair session on the next attempt",
                test_fix_session_id,
                event.get("type"),
            )
            test_fix_session_id = None
            progress = None
            attempt += 1
            continue
        while True:
            patch_count_after = _session_patch_count(client, test_fix_session_id) if test_fix_session_id else patch_count_before
            current_ok, current_output = _run_test_selector(repo_path, test_selector, module=module)
            if current_ok:
                logger.info("Generation test gate passed (attempt %d)", attempt + 1)
                return True, time.perf_counter() - test_started, test_fix_session_id
            current_output = _refresh_test_failure_summary(repo_path, test_selector, module, current_output)
            if patch_count_after <= patch_count_before:
                break
            followup = _test_fix_continue_prompt(
                scope_label=f"`{test_selector}` in generation repair",
                failures=current_output,
                test_selector=test_selector,
                maven_module_flag=maven_module_flag,
            )
            client.send_message(test_fix_session_id, followup, model_id=uta_settings.opencode_model)
            try:
                event = _raise_for_rate_limit_event(
                    event=_poll_with_continue_recovery(
                        client=client,
                        session_id=test_fix_session_id,
                        timeout=_llm_repair_timeout(uta_settings.opencode_model),
                        phase="test_fix",
                        batch=batch,
                        on_update=progress if uta_settings.opencode_stream_progress else None,
                        state=state,
                    ),
                    session_id=test_fix_session_id,
                    client=client,
                    phase="test_fix",
                )
            except ProviderRateLimitError:
                rate_limit_hits += 1
                logger.warning(
                    "Generation test-fix continuation hit provider/model rate limit (%d/%d transient gate retries); "
                    "retrying without consuming the repair budget",
                    rate_limit_hits,
                    max_fix_attempts,
                )
                if rate_limit_hits >= max_fix_attempts:
                    raise
                continue
            if _event_needs_fresh_session(event):
                logger.warning(
                    "Generation test-fix session %s ended with %s during in-session continuation; starting a fresh repair session on the next attempt",
                    test_fix_session_id,
                    event.get("type"),
                )
                test_fix_session_id = None
                progress = None
                attempt += 1
                continue
            patch_count_before = patch_count_after
        attempt += 1

    return False, time.perf_counter() - test_started, test_fix_session_id


def _repair_from_delegated_precheck(
    *,
    state: AgentState,
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    client: "OpenCodeClient",
    batch_session_ids: List[str],
    phase_session_ids: Dict[str, List[str]],
    delegated_precheck_result: Dict[str, Any],
    node_started: float,
    max_fix_attempts: int,
) -> Dict[str, Any]:
    try:
        gate_ok, gate_result, gate_seconds, delegated_gate_session_ids = _run_delegated_quality_gate_fix_loop(
            state=state,
            repo_path=repo_path,
            batch=batch,
            client=client,
            generation_session_id="precheck_existing_tests",
            max_fix_attempts=max_fix_attempts,
            initial_result=delegated_precheck_result,
        )
        for gate_session_id in delegated_gate_session_ids:
            _append_session_id(state, batch_session_ids, gate_session_id)
            _append_phase_session_id(phase_session_ids, "delegated_quality_gate", gate_session_id)
    except ProviderRateLimitError as exc:
        elapsed = time.perf_counter() - node_started
        new_results = state["results"].copy()
        for class_fqn in batch:
            new_results[class_fqn] = {
                "status": "PROVIDER_RATE_LIMITED",
                "coverage": 0.0,
                "tests_pass": False,
                "mutation_score": 0.0,
                "surviving_mutants": 0,
                "output": exc.output,
                "test_file_path": _expected_test_file_rel(module, class_fqn),
                "elapsed_seconds": elapsed,
                "generation_seconds": 0.0,
                "compile_seconds": 0.0,
                "test_seconds": 0.0,
                "mutation_seconds": 0.0,
                "session_id": exc.session_id,
                "session_ids": list(batch_session_ids),
                "rate_limit": exc.rate_limit,
            }
        return {
            "results": new_results,
            "finished": True,
            "stopped_early": True,
            "current_batch": [],
            "current_stage": _delegated_gate_failure_stage(delegated_precheck_result),
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=elapsed,
            ),
        }

    return {
        "results": _delegated_gate_batch_results(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            gate_result=gate_result,
            gate_seconds=gate_seconds,
            status="PASS" if gate_ok else "FAIL",
            session_id=(delegated_gate_session_ids or [None])[-1],
            session_ids=list(batch_session_ids),
        ),
        "current_stage": "delegated_quality_gate" if gate_ok else _delegated_gate_failure_stage(gate_result),
        "session_retrospect": _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        ),
        "session_token_usage": _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=batch_session_ids,
        ),
        "phase_token_usage": _capture_phase_token_usage(
            state=state,
            client=client,
            phase_session_ids=phase_session_ids,
        ),
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=time.perf_counter() - node_started,
            mutation_seconds=gate_seconds,
        ),
    }


def generate_and_validate(state: AgentState) -> Dict[str, Any]:
    """Generate test, verify compilation/execution, collect coverage, run mutation testing.

    Supports batch mode: when current_batch has multiple classes, sends a single
    prompt to the agent covering all classes in the batch. This avoids the overhead
    of re-reading context files for each class.
    """
    node_started = time.perf_counter()
    batch = state.get("current_batch", [])
    if not batch:
        batch = [state["current_class"]]
    repo_path = state["repo_path"]
    module = state["module"]
    graph = state["graph"]
    flows = state["flows"]
    session_id = state.get("session_id")
    batch_session_ids: List[str] = []
    coverage_gate = state["coverage_gate"]
    mutation_gate_score = state["mutation_gate"]
    quality_mode = state.get("quality_mode") or "class_batch"
    quality_gate_backend = state.get("quality_gate_backend") or "builtin"
    max_fix_attempts = 3

    logger.info("Starting generate_and_validate for batch of %d: %s",
                len(batch), [fqn.split(".")[-1] for fqn in batch])
    _set_stage(state, "generate_prompt", f"batch={len(batch)}")

    delegated_precheck_result: Optional[Dict[str, Any]] = None
    prechecked = _precheck_existing_tests(
        state=state,
        repo_path=repo_path,
        module=module,
        batch=batch,
        coverage_gate=coverage_gate,
        mutation_gate_score=mutation_gate_score,
        node_started=node_started,
    )
    if prechecked:
        if prechecked.get("_precheck_action") == "delegated_repair":
            delegated_precheck_result = prechecked.get("delegated_quality_gate") or {}
        else:
            return prechecked

    if not session_id and not state.get("allow_opencode_autostart"):
        new_results = state["results"].copy()
        for class_fqn in batch:
            new_results[class_fqn] = {
                "status": "SKIP",
                "coverage": 0.0,
                "tests_pass": False,
                "mutation_score": 0.0,
                "surviving_mutants": 0,
                "output": "Skipped because no OpenCode session was provided",
                "test_file_path": _expected_test_file_rel(module, class_fqn),
                "elapsed_seconds": time.perf_counter() - node_started,
                "generation_seconds": 0.0,
                "compile_seconds": 0.0,
                "test_seconds": 0.0,
                "mutation_seconds": 0.0,
                "session_id": None,
                "session_ids": list(state.get("session_ids") or []),
            }
        return {
            "results": new_results,
            "current_class": None,
            "current_batch": [],
            "current_stage": "generate_prompt",
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
            ),
        }

    client = OpenCodeClient(repo_path=repo_path)
    plan_text = ""
    phase_session_ids: Dict[str, List[str]] = {
        "plan": [],
        "generate": [],
        "compile_fix": [],
        "generation_test_fix": [],
        "coverage_fix": [],
        "mutation_test_fix": [],
        "mutation_fix": [],
        "delegated_quality_gate": [],
    }

    if delegated_precheck_result is not None and quality_gate_backend == "maven_enforcer":
        return _repair_from_delegated_precheck(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            client=client,
            batch_session_ids=batch_session_ids,
            phase_session_ids=phase_session_ids,
            delegated_precheck_result=delegated_precheck_result,
            node_started=node_started,
            max_fix_attempts=max_fix_attempts,
        )

    ctx_provider = _JavaWorkflowContextProvider(repo_path, graph, flows)
    context_dir = ctx_provider.export_project_context()
    sync_project_summaries(repo_path, graph, module, language="java")
    ctx_builder = getattr(ctx_provider, "builder", ctx_provider)
    maven_module_flag = f" -pl {module} -am" if module else ""
    project_paths = prompt_template_paths(repo_path, context_dir)
    complexity_by_class = {
        class_fqn: _source_complexity_summary(ctx_builder.get_class_source_path(class_fqn), coverage_gate)
        for class_fqn in batch
    }
    strict_coverage_classes = [
        {
            "class_fqn": class_fqn,
            "line_count": meta["line_count"],
            "public_method_count": meta["public_method_count"],
        }
        for class_fqn, meta in complexity_by_class.items()
        if meta.get("strict_coverage")
    ]
    _set_stage(state, "target_context", f"batch={len(batch)}")
    target_context_paths: Dict[str, Dict[str, str]] = {}
    for class_fqn in batch:
        if hasattr(ctx_builder, "export_target_context_files"):
            target_context_paths[class_fqn] = ctx_builder.export_target_context_files(
                class_fqn,
                module=module,
                test_file_rel=_expected_test_file_rel(module, class_fqn),
            )
        else:
            fallback_name = class_fqn.split(".")[-1]
            target_context_paths[class_fqn] = {
                "context_abs": str((Path(context_dir) / f"{fallback_name}.context.md").resolve()),
                "symbols_abs": str((Path(context_dir) / f"{fallback_name}.symbols.md").resolve()),
            }

    planning_session_id = session_id if state.get("resume") and session_id else _create_phase_session(
        state=state,
        client=client,
        session_ids=batch_session_ids,
        model_id=uta_settings.opencode_model,
    )
    state["session_id"] = planning_session_id
    _append_phase_session_id(phase_session_ids, "plan", planning_session_id)
    # Preseed prior-run resolved symbol artifacts without injecting replay hints
    # into first-pass planning/generation prompts. The replay guidance made the
    # model more cautious and search-heavy in real runs.
    for class_fqn in batch:
        try:
            from uta.learning import preseed_compile_context
            preseed_compile_context(
                repo_path=repo_path,
                class_fqn=class_fqn,
                symbols_abs=target_context_paths.get(class_fqn, {}).get("symbols_abs"),
            )
        except Exception:
            logger.debug("[%s] preseed_compile_context skipped", class_fqn, exc_info=True)

    # Compute and export ROI scores per class
    roi_enabled = uta_settings.roi_enabled and graph is not None
    method_efforts_by_class: Dict[str, List[Dict[str, Any]]] = {}
    if roi_enabled:
        from uta.language.java.scoring.coverage_roi import compute_class_roi, is_degenerate_roi_data
        for class_fqn in batch:
            try:
                roi_data = compute_class_roi(class_fqn, graph)
                method_efforts_by_class[class_fqn] = list(roi_data.get("methods") or [])
                if is_degenerate_roi_data(roi_data):
                    logger.warning("[%s] Initial ROI scores are degenerate; skipping ROI artifact export", class_fqn)
                    try:
                        ctx_builder.clear_roi_scores(class_fqn)
                    except Exception:
                        logger.warning("[%s] Failed to clear stale ROI artifact", class_fqn, exc_info=True)
                    target_context_paths[class_fqn]["roi_abs"] = ""
                    continue
                source_path = ctx_builder.get_class_source_path(class_fqn)
                roi_abs = ctx_builder.export_roi_scores(
                    class_fqn, roi_data, source_path=source_path, debug=uta_settings.roi_debug,
                )
                target_context_paths[class_fqn]["roi_abs"] = roi_abs
                logger.info("[%s] ROI scores exported: %s", class_fqn, roi_abs)
            except Exception:
                logger.warning("[%s] Failed to compute ROI scores", class_fqn, exc_info=True)
                target_context_paths[class_fqn]["roi_abs"] = ""
    # --- Step 1a: Ask for a coverage/branch plan first ---
    from uta.prompts.loader import render_prompt_split as _render_split
    _set_stage(state, "plan_tests", f"batch={len(batch)} session={planning_session_id}")
    plan_context_parts = []
    for class_fqn in batch:
        paths = target_context_paths[class_fqn]
        part = (
            f"- `{class_fqn}`\n"
            f"  - target context: `{paths['context_abs']}`\n"
            f"  - symbol map: `{paths['symbols_abs']}`"
        )
        roi_path = paths.get("roi_abs", "")
        if roi_path:
            part += f"\n  - roi scores: `{roi_path}`"
        plan_context_parts.append(part)
    plan_target_context = "\n".join(plan_context_parts)

    # K: pre-compute wave table from target context so model doesn't need to re-derive it.
    wave_table_section = ""
    try:
        from uta.engine.wave_assigner import assign_waves_from_context, format_wave_table
        for class_fqn in batch:
            ctx_abs = target_context_paths[class_fqn].get("context_abs", "")
            if ctx_abs and Path(ctx_abs).exists():
                ctx_text = Path(ctx_abs).read_text(encoding="utf-8", errors="replace")
                waves = assign_waves_from_context(ctx_text)
                if waves:
                    wave_table_section += f"\n\n### PRE-COMPUTED WAVE TABLE — `{class_fqn}`\n"
                    wave_table_section += format_wave_table(waves)
    except Exception:
        logger.debug("Wave table pre-computation skipped", exc_info=True)

    _plan_index_query_command = _index_query_command(module)
    _generation_index_query_command = _index_query_command(module)
    _fix_index_query_command = _index_query_command(module, section="fix_summary")

    _plan_kwargs = dict(
        batch=batch,
        coverage_gate=coverage_gate,
        quality_mode=quality_mode,
        ci_diff_coverage_gate=uta_settings.ci_diff_coverage_gate,
        ci_diff_mutation_gate=uta_settings.ci_diff_mutation_gate,
        strict_coverage_classes=strict_coverage_classes,
        target_context_files=plan_target_context,
        roi_enabled=roi_enabled,
        index_query_command=_plan_index_query_command,
        stage_introspect_abs=ensure_stage_introspect_file(repo_path, "plan"),
    )
    _plan_stable, _plan_volatile = _render_split("plan_tests", **_plan_kwargs)
    _plan_volatile += wave_table_section
    plan_progress = _session_progress_logger(batch, session_id=planning_session_id, stage="plan")
    planning_timeout = _llm_timeout(
        max(120, int(uta_settings.opencode_planning_timeout_seconds or 600)),
        uta_settings.opencode_model,
    )
    resumed_from_plan_artifact = False
    if state.get("resume"):
        plan_text = _load_generation_plan_for_resume(repo_path, batch)
        if plan_text.strip():
            resumed_from_plan_artifact = True
            logger.info("Resuming from existing generation plan artifact: %s", _generation_plan_path(repo_path))

    if not resumed_from_plan_artifact:
        logger.info("Sending planning prompt for %d class(es)", len(batch))
        _clear_generation_plan(repo_path)
        client.send_message_split(planning_session_id, _plan_stable, _plan_volatile,
                                   model_id=uta_settings.opencode_model)
        plan_event = _poll_with_continue_recovery(
            client=client,
            session_id=planning_session_id,
            timeout=planning_timeout,
            phase="plan",
            batch=batch,
            on_update=plan_progress if uta_settings.opencode_stream_progress else None,
            state=state,
        )
        if plan_event.get("type") == "rate_limited":
            logger.warning("Planning hit provider/model rate limit for batch %s", batch)
            return _rate_limited_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                generation_started_at=time.time(),
                generation_finished_at=time.time(),
                generation_seconds=0.0,
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        if plan_event.get("type") == "error":
            logger.error("Planning hit provider/model error for batch %s: %s", batch, _event_error_message(plan_event))
            return _provider_error_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                event=plan_event,
                stage="plan_tests",
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        planning_rate_limit = _detect_provider_limit_after_event(client, planning_session_id, plan_event)
        if planning_rate_limit:
            logger.warning("Planning hit provider/model quota for batch %s", batch)
            return _rate_limited_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                generation_started_at=time.time(),
                generation_finished_at=time.time(),
                generation_seconds=0.0,
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        if plan_event.get("type") == "timeout":
            logger.warning(
                "Planning timed out after %ss for batch %s before emitting a final plan",
                planning_timeout,
                batch,
            )
            return _planning_timeout_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                planning_timeout=planning_timeout,
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        plan_text = (plan_event or {}).get("result", "") or ""
        if not plan_text.strip():
            recovered_plan_text = _recover_plan_text_from_session_artifact(
                repo_path=repo_path,
                session_id=planning_session_id,
                client=client,
            )
            if recovered_plan_text:
                logger.info("Recovered planning content from latest_generation_plan.md artifact for session %s", planning_session_id)
                plan_text = recovered_plan_text
        if not plan_text.strip():
            logger.info("Planning returned no final text; requesting one forced final plan emission")
            finalize_prompt = (
                "Emit the final plan now using only the required plan format. "
                "Do not call more tools. Do not add commentary about missing context. "
                "Use the facts already gathered in this session and output only the final plan."
            )
            client.send_message(planning_session_id, finalize_prompt, model_id=uta_settings.opencode_model)
            finalize_event = _poll_with_continue_recovery(
                client=client,
                session_id=planning_session_id,
                timeout=min(_llm_timeout(120, uta_settings.opencode_model), planning_timeout),
                phase="plan",
                batch=batch,
                on_update=plan_progress if uta_settings.opencode_stream_progress else None,
                state=state,
            )
            plan_text = (finalize_event or {}).get("result", "") or plan_text
            finalize_rate_limit = _detect_provider_limit_after_event(client, planning_session_id, finalize_event)
            if finalize_rate_limit:
                logger.warning("Planning finalization hit provider/model quota for batch %s", batch)
                return _rate_limited_result(
                    state=state,
                    batch=batch,
                    session_id=planning_session_id,
                    generation_started_at=time.time(),
                    generation_finished_at=time.time(),
                    generation_seconds=0.0,
                    generate_validate_seconds=time.perf_counter() - node_started,
                    repo_path=repo_path,
                    module=module,
                    client=client,
                )
            if finalize_event.get("type") == "timeout":
                logger.warning(
                    "Planning finalization timed out after %ss for batch %s before emitting a final plan",
                    min(120, planning_timeout),
                    batch,
                )
                return _planning_timeout_result(
                    state=state,
                    batch=batch,
                    session_id=planning_session_id,
                    planning_timeout=planning_timeout,
                    generate_validate_seconds=time.perf_counter() - node_started,
                    repo_path=repo_path,
                    module=module,
                    client=client,
                )
            if finalize_event.get("type") == "error":
                logger.error("Planning finalization hit provider/model error for batch %s: %s", batch, _event_error_message(finalize_event))
                return _provider_error_result(
                    state=state,
                    batch=batch,
                    session_id=planning_session_id,
                    event=finalize_event,
                    stage="plan_tests",
                    generate_validate_seconds=time.perf_counter() - node_started,
                    repo_path=repo_path,
                    module=module,
                    client=client,
                )
            if not plan_text.strip():
                recovered_plan_text = _recover_plan_text_from_session_artifact(
                    repo_path=repo_path,
                    session_id=planning_session_id,
                    client=client,
                )
                if recovered_plan_text:
                    logger.info("Recovered planning content after forced finalization for session %s", planning_session_id)
                    plan_text = recovered_plan_text
    replan_reasons: List[str] = []
    if plan_text.strip() and not resumed_from_plan_artifact and _plan_needs_stricter_replan(plan_text, strict_coverage_classes):
        strict_names = ", ".join(item["class_fqn"].split(".")[-1] for item in strict_coverage_classes)
        logger.info("Planning output is too narrow for strict coverage class(es): %s — requesting one replan", strict_names)
        replan_reasons.append(
            "The prior plan is structurally too narrow for a strict coverage class. "
            "It must include concrete `METHODS REQUIRED FOR GATE`, `ESTIMATED REACH`, `BLOCKERS`, and `IMPLEMENTATION WAVES`."
        )

    plan_breadth_notes: List[str] = []
    plan_feasibility_notes: List[str] = []
    if plan_text.strip() and not resumed_from_plan_artifact:
        try:
            from uta.engine.validation import (
                BreadthVerdict,
                FeasibilityVerdict,
                validate_plan_breadth,
                validate_plan_feasibility,
            )

            for class_fqn in strict_coverage_classes and [item["class_fqn"] for item in strict_coverage_classes] or []:
                ctx_abs = target_context_paths.get(class_fqn, {}).get("context_abs", "")
                if ctx_abs and Path(ctx_abs).exists():
                    ctx_text = Path(ctx_abs).read_text(encoding="utf-8", errors="replace")
                    breadth = validate_plan_breadth(plan_text, ctx_text)
                    if breadth.verdict == BreadthVerdict.UNDER:
                        msg = _plan_breadth_replan_reason(class_fqn, breadth)
                        if not msg:
                            continue
                        plan_breadth_notes.append(msg)
                        logger.warning(msg)
                    elif breadth.verdict == BreadthVerdict.OVER:
                        msg = f"[{class_fqn}] {breadth.message}"
                        logger.warning("%s Proceeding because over-broad plans are non-fatal when feasibility passes.", msg)
                    else:
                        logger.info("[%s] Plan breadth PASS: %s", class_fqn, breadth.message)

                roi_methods = method_efforts_by_class.get(class_fqn) or []
                if roi_methods:
                    feasibility = validate_plan_feasibility(
                        plan_text,
                        roi_methods,
                        coverage_gate=coverage_gate,
                    )
                    if feasibility.verdict == FeasibilityVerdict.UNDER:
                        msg = f"[{class_fqn}] {feasibility.message}"
                        plan_feasibility_notes.append(msg)
                        logger.warning("[%s] Plan feasibility UNDER: %s", class_fqn, feasibility.message)
                    else:
                        logger.info("[%s] Plan feasibility PASS: %s", class_fqn, feasibility.message)
        except Exception:
            logger.debug("Plan strict-feasibility validation skipped", exc_info=True)

    if plan_breadth_notes:
        replan_reasons.extend(
            f"Plan breadth validator found the plan too thin: {note}"
            for note in plan_breadth_notes
        )
    if plan_feasibility_notes:
        replan_reasons.extend(
            f"Plan feasibility validator found the gate-method mix too weak: {note}"
            for note in plan_feasibility_notes
        )

    if not plan_text.strip():
        logger.warning("Planning did not return usable content; continuing without an approved plan")
        plan_text = ""
    elif replan_reasons:
        candidate_plan_path = _write_generation_plan_candidate(
            repo_path=repo_path,
            session_id=planning_session_id,
            batch=batch,
            plan_text=plan_text,
            replan_reasons=replan_reasons,
        )
        logger.info("Saved candidate generation plan before replan: %s", candidate_plan_path)
        replan_prompt = (
            "Revise the plan for the strict coverage class(es) now. "
            "The prior plan did not plausibly cover enough public branch reach for the required coverage gate. "
            "For each strict coverage class, expand or rebalance scope until the plan can plausibly reach the gate. "
            "Do not say to defer heavy branches, do not optimize for only cheap or high-value wrappers first, "
            "and do not say to avoid class-wide completeness unless you still prove the gate is reachable. "
            "Keep the required plan format, but revise `METHODS REQUIRED FOR GATE`, `ESTIMATED REACH`, "
            "`BLOCKERS`, and `IMPLEMENTATION WAVES` so they reflect the full public branch families needed "
            "for the gate.\n\n"
            "Deterministic critique to address:\n- " + "\n- ".join(replan_reasons[:8])
        )
        client.send_message(planning_session_id, replan_prompt, model_id=uta_settings.opencode_model)
        plan_event = _poll_with_continue_recovery(
            client=client,
            session_id=planning_session_id,
            timeout=planning_timeout,
            phase="plan",
            batch=batch,
            on_update=plan_progress if uta_settings.opencode_stream_progress else None,
            state=state,
        )
        planning_rate_limit = _detect_provider_limit_after_event(client, planning_session_id, plan_event)
        if planning_rate_limit:
            logger.warning("Replanning hit provider/model quota for batch %s", batch)
            return _rate_limited_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                generation_started_at=time.time(),
                generation_finished_at=time.time(),
                generation_seconds=0.0,
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        if plan_event.get("type") == "timeout":
            logger.warning(
                "Replanning timed out after %ss for batch %s before emitting a final plan",
                planning_timeout,
                batch,
            )
            return _planning_timeout_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                planning_timeout=planning_timeout,
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        if plan_event.get("type") == "error":
            logger.error("Replanning hit provider/model error for batch %s: %s", batch, _event_error_message(plan_event))
            return _provider_error_result(
                state=state,
                batch=batch,
                session_id=planning_session_id,
                event=plan_event,
                stage="plan_tests",
                generate_validate_seconds=time.perf_counter() - node_started,
                repo_path=repo_path,
                module=module,
                client=client,
            )
        plan_text = (plan_event or {}).get("result", "") or plan_text
        if not plan_text.strip():
            recovered_plan_text = _recover_plan_text_from_session_artifact(
                repo_path=repo_path,
                session_id=planning_session_id,
                client=client,
            )
            if recovered_plan_text:
                logger.info("Recovered replanning content from latest_generation_plan.md artifact for session %s", planning_session_id)
                plan_text = recovered_plan_text
    elif resumed_from_plan_artifact:
        logger.info("Skipping planning revalidation during resume; continuing with saved plan artifact")
    plan_path = _write_generation_plan(repo_path, planning_session_id, batch, plan_text)
    # K: validate plan breadth; log under-spec/over-spec for learning after plan is finalized.
    if plan_text.strip():
        try:
            from uta.engine.validation import BreadthVerdict, validate_plan_breadth
            for class_fqn in batch:
                ctx_abs = target_context_paths[class_fqn].get("context_abs", "")
                if ctx_abs and Path(ctx_abs).exists():
                    ctx_text = Path(ctx_abs).read_text(encoding="utf-8", errors="replace")
                    breadth = validate_plan_breadth(plan_text, ctx_text)
                    if breadth.verdict == BreadthVerdict.UNDER:
                        logger.warning("[%s] Plan breadth UNDER: %s", class_fqn, breadth.message)
                    elif breadth.verdict == BreadthVerdict.OVER:
                        logger.warning("[%s] Plan breadth OVER: %s", class_fqn, breadth.message)
                    else:
                        logger.info("[%s] Plan breadth PASS: %s", class_fqn, breadth.message)
        except Exception:
            logger.debug("Plan breadth validation skipped", exc_info=True)

    if _should_stop_after(state, "plan_tests"):
        logger.info("Stopping early after planning stage by request")
        session_token_usage = _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=batch_session_ids,
        )
        session_retrospect = _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        )
        return {
            "results": state.get("results", {}),
            "finished": True,
            "stopped_early": True,
            "current_batch": [],
            "current_class": None,
            "current_stage": "plan_tests",
            "session_token_usage": session_token_usage,
            "session_retrospect": session_retrospect,
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
            ),
        }

    generation_session_id = _create_phase_session(
        state=state,
        client=client,
        session_ids=batch_session_ids,
        model_id=uta_settings.opencode_model,
        permission=[
            {"permission": "todowrite", "action": "deny", "pattern": "*"},
        ],
    )
    _append_phase_session_id(phase_session_ids, "generate", generation_session_id)

    # --- Step 1b: Send generation prompt (batch or single) ---
    _run_id = str(int(time.time()))
    if len(batch) == 1:
        # Single class — use standard prompt
        class_fqn = batch[0]
        source_path = ctx_builder.get_class_source_path(class_fqn)
        test_class_name = f"{class_fqn.split('.')[-1]}Test"
        target_paths = target_context_paths[class_fqn]
        maven_instr = (
            f"\n\nAfter writing the test, verify with:\n"
            f"  mvn test-compile{maven_module_flag}\n"
            f"  mvn test -Dtest={test_class_name} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
        )
        _gen_kwargs = dict(
            class_fqn=class_fqn,
            source_path=source_path,
            context_dir=str(context_dir),
            target_context_abs=target_paths["context_abs"],
            target_symbols_abs=target_paths["symbols_abs"],
            index_query_command=_generation_index_query_command,
            wave_one_only=bool(strict_coverage_classes),
            maven_instructions=maven_instr,
            maven_module_flag=maven_module_flag,
            test_class_name=test_class_name,
            coverage_gate=coverage_gate,
            quality_mode=quality_mode,
            ci_diff_coverage_gate=uta_settings.ci_diff_coverage_gate,
            ci_diff_mutation_gate=uta_settings.ci_diff_mutation_gate,
            run_id=_run_id,
            stage_introspect_abs=ensure_stage_introspect_file(repo_path, "generate"),
            mockito_api_guidance=_mockito_api_guidance(repo_path),
            **project_paths,
        )
        _gen_stable, prompt = _render_split("generate_test", **_gen_kwargs)
    else:
        # Batch mode — build a multi-class prompt
        class_sections = []
        all_test_names = []
        for fqn in batch:
            source_path = ctx_builder.get_class_source_path(fqn)
            test_name = f"{fqn.split('.')[-1]}Test"
            all_test_names.append(test_name)
            target_paths = target_context_paths[fqn]
            class_sections.append(
                f"- **{fqn}** → source: `{source_path}` → test: `{test_name}`\n"
                f"  - target context: `{target_paths['context_abs']}`\n"
                f"  - symbol map: `{target_paths['symbols_abs']}`"
            )

        test_list = ",".join(all_test_names)
        _gen_kwargs = dict(
            class_fqn=batch[0],
            source_path=ctx_builder.get_class_source_path(batch[0]),
            context_dir=str(context_dir),
            target_context_abs=target_context_paths[batch[0]]["context_abs"],
            target_symbols_abs=target_context_paths[batch[0]]["symbols_abs"],
            index_query_command=_generation_index_query_command,
            wave_one_only=False,
            maven_instructions=(
                f"\n\nAfter writing ALL test files, verify with:\n"
                f"  mvn test-compile{maven_module_flag}\n"
                f"  mvn test -Dtest={test_list} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
            ),
            maven_module_flag=maven_module_flag,
            test_class_name=all_test_names[0],
            coverage_gate=coverage_gate,
            quality_mode=quality_mode,
            ci_diff_coverage_gate=uta_settings.ci_diff_coverage_gate,
            ci_diff_mutation_gate=uta_settings.ci_diff_mutation_gate,
            run_id=_run_id,
            stage_introspect_abs=ensure_stage_introspect_file(repo_path, "generate"),
            mockito_api_guidance=_mockito_api_guidance(repo_path),
            **project_paths,
        )
        _gen_stable, prompt = _render_split("generate_test", **_gen_kwargs)
        # Append batch instructions after the volatile tail
        batch_addendum = (
            f"\n\n### BATCH MODE — GENERATE TESTS FOR ALL {len(batch)} CLASSES\n"
            f"You must generate a SEPARATE test file for EACH of these classes:\n"
            + "\n".join(class_sections) +
            f"\n\nFor each class, follow the same process: read source → plan tests → write test file → compile.\n"
            f"Write ALL test files first, then compile once with `mvn test-compile{maven_module_flag}`.\n"
            f"If compilation fails, fix ALL test files before proceeding.\n"
            f"Then run all tests: `mvn test -Dtest={test_list} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}`"
        )
        prompt += batch_addendum

    # J: inject stub catalog patterns for known dependency types only when explicitly enabled.
    if uta_settings.inject_stub_catalog_in_generation:
        try:
            from uta.learning import load_project_summary
            from uta.templates.stub_catalog import format_stub_catalog_md, seed_from_project_summary
            all_dep_types: List[str] = []
            for class_fqn in batch:
                ctx_abs = target_context_paths[class_fqn].get("context_abs", "")
                if ctx_abs and Path(ctx_abs).exists():
                    import re as _re
                    ctx_text = Path(ctx_abs).read_text(encoding="utf-8", errors="replace")
                    dep_m = _re.search(r"## Dependency Types\n(.*?)(?=\n##|\Z)", ctx_text, _re.DOTALL)
                    if dep_m:
                        all_dep_types += _re.findall(r"`([A-Za-z][A-Za-z0-9_.]*)`", dep_m.group(1))
            seeded_catalog = seed_from_project_summary(load_project_summary(repo_path))
            if seeded_catalog:
                all_dep_types += list(seeded_catalog.keys())
            stub_section = format_stub_catalog_md(all_dep_types)
            if stub_section:
                prompt += "\n\n" + stub_section
        except Exception:
            logger.debug("Stub catalog injection skipped", exc_info=True)

    # K: inject deterministic test skeletons only when explicitly enabled.
    if uta_settings.inject_test_skeleton_in_generation:
        try:
            from uta.templates.test_skeleton import generate_skeleton_from_context_file
            skeleton_sections: List[str] = []
            for class_fqn in batch:
                ctx_abs = target_context_paths[class_fqn].get("context_abs", "")
                if not ctx_abs or not Path(ctx_abs).exists():
                    continue
                skeleton = generate_skeleton_from_context_file(ctx_abs, class_fqn)
                if not skeleton.strip():
                    continue
                test_name = f"{class_fqn.split('.')[-1]}Test"
                skeleton_sections.append(
                    f"### `{test_name}` deterministic starter skeleton\n```java\n{skeleton}\n```"
                )
            if skeleton_sections:
                prompt += (
                    "\n\n### DETERMINISTIC STARTER SKELETONS\n"
                    "Use these Python-generated skeletons as the starting structure instead of re-deriving package/import/mock boilerplate.\n\n"
                    + "\n\n".join(skeleton_sections)
                )
        except Exception:
            logger.debug("Deterministic skeleton injection skipped", exc_info=True)

    compressed_plan = _compress_plan_for_generation(plan_text) if plan_text else ""
    prompt += (
        "\n\n### APPROVED TEST PLAN\n"
        f"Plan file: `{plan_path}`\n\n"
        "Use the following plan as the basis for generation. Satisfy it before optimizing minor details.\n\n"
        f"{compressed_plan or '_No plan text was captured; fall back to the prompt requirements above._'}"
    )
    _set_stage(state, "generate", f"batch={len(batch)} session={generation_session_id}")
    logger.info("Sending generation prompt for %d class(es)", len(batch))
    progress = _session_progress_logger(batch, session_id=generation_session_id, stage="generate")
    generation_started_at = time.time()
    generation_started_perf = time.perf_counter()
    client.send_message_split(generation_session_id, _gen_stable, prompt,
                               model_id=uta_settings.opencode_model)
    # Scale timeout with batch size and complexity: 40 min per class × config ratio × complexity multiplier.
    generation_timeout = _llm_timeout(max(
        300,
        int(
            2400
            * len(batch)
            * float(uta_settings.opencode_generation_timeout_ratio or 1.0)
            * _batch_complexity_multiplier(complexity_by_class)
        ),
    ), uta_settings.opencode_model)
    event = _poll_with_continue_recovery(
        client=client,
        session_id=generation_session_id,
        timeout=generation_timeout,
        phase="generate",
        batch=batch,
        on_update=progress if uta_settings.opencode_stream_progress else None,
        state=state,
    )
    generation_seconds = time.perf_counter() - generation_started_perf
    generation_finished_at = time.time()
    if event.get("type") == "rate_limited":
        logger.warning("Generation hit provider/model rate limit for batch %s", batch)
        return _rate_limited_result(
            state=state,
            batch=batch,
            session_id=generation_session_id,
            generation_started_at=generation_started_at,
            generation_finished_at=generation_finished_at,
            generation_seconds=generation_seconds,
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )
    if event.get("type") == "timeout":
        logger.warning(
            "Generation timed out after %ss for batch %s",
            generation_timeout,
            batch,
        )
        session_retrospect = _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        )
        new_results = state["results"].copy()
        for class_fqn in batch:
            test_file_rel = _expected_test_file_rel(module, class_fqn)
            new_results[class_fqn] = {
                "status": "GENERATION_TIMEOUT",
                "coverage": 0.0,
                "tests_pass": False,
                "mutation_score": 0.0,
                "surviving_mutants": 0,
                "output": f"OpenCode generation timed out after {generation_timeout}s before completion",
                "test_file_path": test_file_rel,
                "elapsed_seconds": generation_seconds,
                "generation_seconds": generation_seconds,
                "compile_seconds": 0.0,
                "test_seconds": 0.0,
                "mutation_seconds": 0.0,
                "session_id": generation_session_id,
                "session_ids": list(batch_session_ids),
                "generation_started_at": generation_started_at,
                "generation_finished_at": generation_finished_at,
            }
        return {
            "results": new_results,
            "current_class": None,
            "current_stage": "generate_prompt",
            "stopped_early": True,
            "session_retrospect": session_retrospect,
            "session_token_usage": _capture_session_token_usage(
                state=state,
                client=client,
                session_ids=batch_session_ids,
            ),
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
            ),
        }
    if event.get("type") == "error":
        logger.error("Generation hit provider/model error for batch %s: %s", batch, _event_error_message(event))
        return _provider_error_result(
            state=state,
            batch=batch,
            session_id=generation_session_id,
            event=event,
            stage="generate_prompt",
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )

    detect_rate_limit = getattr(client, "detect_rate_limit_issue", None)
    generation_rate_limit = detect_rate_limit(generation_session_id) if callable(detect_rate_limit) else None
    if generation_rate_limit:
        logger.warning("Generation hit provider/model quota for batch %s", batch)
        return _rate_limited_result(
            state=state,
            batch=batch,
            session_id=generation_session_id,
            generation_started_at=generation_started_at,
            generation_finished_at=generation_finished_at,
            generation_seconds=generation_seconds,
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )

    try:
        missing_after_generation = _prompt_for_missing_batch_files(
            repo_path=repo_path,
            module=module,
            batch=batch,
            session_id=generation_session_id,
            client=client,
            progress=progress,
            state=state,
        )
    except ProviderRateLimitError:
        logger.warning("Missing-file recovery hit provider/model rate limit for batch %s", batch)
        return _rate_limited_result(
            state=state,
            batch=batch,
            session_id=generation_session_id,
            generation_started_at=generation_started_at,
            generation_finished_at=time.time(),
            generation_seconds=generation_seconds,
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )
    if missing_after_generation:
        logger.error("Batch generation missing files after retry: %s", missing_after_generation)
        session_retrospect = _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        )
        new_results = state["results"].copy()
        for class_fqn in batch:
            test_file_rel = _expected_test_file_rel(module, class_fqn)
            missing = test_file_rel in missing_after_generation
            new_results[class_fqn] = {
                "status": "INCOMPLETE_BATCH" if missing else "SKIP",
                "coverage": 0.0,
                "tests_pass": False,
                "mutation_score": 0.0,
                "surviving_mutants": 0,
                "output": (
                    "Batch generation ended before all expected test files were written"
                    if missing else "Skipped because batch generation was incomplete"
                ),
                "test_file_path": test_file_rel,
                "elapsed_seconds": generation_seconds,
                "generation_seconds": generation_seconds,
                "compile_seconds": 0.0,
                "test_seconds": 0.0,
                "mutation_seconds": 0.0,
                "session_id": generation_session_id,
                "session_ids": list(batch_session_ids),
                "generation_started_at": generation_started_at,
                "generation_finished_at": generation_finished_at,
            }
        return {
            "results": new_results,
            "current_class": None,
            "current_stage": "generate_prompt",
            "session_retrospect": session_retrospect,
            "session_token_usage": _capture_session_token_usage(
                state=state,
                client=client,
                session_ids=batch_session_ids,
            ),
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
            ),
        }

    compile_fix_session_id: Optional[str] = None
    _set_stage(state, "compile_verification", f"batch={len(batch)}")
    try:
        compile_ok, compile_seconds, compile_fix_session_id = _run_generation_compile_gate(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            generation_session_id=generation_session_id,
            client=client,
            maven_module_flag=maven_module_flag,
            target_context_paths=target_context_paths,
            max_fix_attempts=max_fix_attempts,
        )
        _append_session_id(state, batch_session_ids, compile_fix_session_id)
        _append_phase_session_id(phase_session_ids, "compile_fix", compile_fix_session_id)
    except ProviderRateLimitError:
        logger.warning("Compile-fix hit provider/model rate limit for batch %s", batch)
        return _rate_limited_result(
            state=state,
            batch=batch,
            session_id=compile_fix_session_id or generation_session_id,
            generation_started_at=generation_started_at,
            generation_finished_at=time.time(),
            generation_seconds=generation_seconds,
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )
    generation_finished_at = time.time()

    if not compile_ok:
        logger.error("COMPILE_FAIL after %d attempts for batch", max_fix_attempts)
        new_results = state["results"].copy()
        for class_fqn in batch:
            new_results[class_fqn] = {
                "status": "COMPILE_FAIL",
                "coverage": 0.0,
                "output": f"Generation compile gate failed after {max_fix_attempts} fix attempts",
                "generation_seconds": generation_seconds,
                "compile_seconds": compile_seconds,
                "elapsed_seconds": generation_seconds + compile_seconds,
                "session_id": compile_fix_session_id or generation_session_id,
                "session_ids": list(batch_session_ids),
                "generation_started_at": generation_started_at,
                "generation_finished_at": generation_finished_at,
            }
        return {
            "results": new_results,
            "current_class": None,
            "current_stage": "compile_verification",
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
                compile_verification_seconds=compile_seconds,
            ),
        }

    # --- Step 2b: Targeted test gate before accepting generation output ---
    generation_test_session_id: Optional[str] = None
    _set_stage(state, "test_verification", f"batch={len(batch)}")
    try:
        generation_test_ok, generation_test_seconds, generation_test_session_id = _run_generation_test_gate(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            generation_session_id=generation_session_id,
            client=client,
            maven_module_flag=maven_module_flag,
            max_fix_attempts=max_fix_attempts,
        )
        _append_session_id(state, batch_session_ids, generation_test_session_id)
        _append_phase_session_id(phase_session_ids, "generation_test_fix", generation_test_session_id)
    except ProviderRateLimitError:
        logger.warning("Generation test-fix hit provider/model rate limit for batch %s", batch)
        return _rate_limited_result(
            state=state,
            batch=batch,
            session_id=generation_test_session_id or generation_session_id,
            generation_started_at=generation_started_at,
            generation_finished_at=time.time(),
            generation_seconds=generation_seconds,
            generate_validate_seconds=time.perf_counter() - node_started,
            repo_path=repo_path,
            module=module,
            client=client,
        )

    if not generation_test_ok:
        logger.error("TEST_FAIL after %d attempts in generation gate for batch", max_fix_attempts)
        new_results = state["results"].copy()
        for class_fqn in batch:
            new_results[class_fqn] = {
                "status": "TEST_FAIL",
                "coverage": 0.0,
                "tests_pass": False,
                "mutation_score": 0.0,
                "surviving_mutants": 0,
                "output": f"Generation test gate failed after {max_fix_attempts} fix attempts",
                "generation_seconds": generation_seconds,
                "compile_seconds": compile_seconds,
                "test_seconds": generation_test_seconds,
                "elapsed_seconds": generation_seconds + compile_seconds + generation_test_seconds,
                "session_id": generation_test_session_id or generation_session_id,
                "session_ids": list(batch_session_ids),
                "generation_started_at": generation_started_at,
                "generation_finished_at": generation_finished_at,
            }
        return {
            "results": new_results,
            "current_class": None,
            "current_stage": "test_verification",
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
                compile_verification_seconds=compile_seconds,
                test_execution_seconds=generation_test_seconds,
            ),
        }

    if _should_stop_after(state, "generation"):
        logger.info("Stopping early after generation stage by request")
        session_token_usage = _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=batch_session_ids,
        )
        session_retrospect = _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        )
        return {
            "results": state.get("results", {}),
            "finished": True,
            "stopped_early": True,
            "current_batch": [],
            "current_class": None,
            "current_stage": "generation",
            "session_token_usage": session_token_usage,
            "session_retrospect": session_retrospect,
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
                compile_verification_seconds=compile_seconds,
                test_execution_seconds=generation_test_seconds,
            ),
        }

    if quality_gate_backend == "maven_enforcer":
        delegated_gate_session_ids: List[str] = []
        try:
            gate_ok, gate_result, gate_seconds, delegated_gate_session_ids = _run_delegated_quality_gate_fix_loop(
                state=state,
                repo_path=repo_path,
                batch=batch,
                client=client,
                generation_session_id=generation_session_id,
                max_fix_attempts=max_fix_attempts,
            )
            for gate_session_id in delegated_gate_session_ids:
                _append_session_id(state, batch_session_ids, gate_session_id)
                _append_phase_session_id(phase_session_ids, "delegated_quality_gate", gate_session_id)
        except ProviderRateLimitError as exc:
            elapsed = time.perf_counter() - node_started
            new_results = state["results"].copy()
            for class_fqn in batch:
                new_results[class_fqn] = {
                    "status": "PROVIDER_RATE_LIMITED",
                    "coverage": 0.0,
                    "tests_pass": False,
                    "mutation_score": 0.0,
                    "surviving_mutants": 0,
                    "output": exc.output,
                    "test_file_path": _expected_test_file_rel(module, class_fqn),
                    "elapsed_seconds": elapsed,
                    "generation_seconds": generation_seconds,
                    "compile_seconds": compile_seconds,
                    "test_seconds": generation_test_seconds,
                    "mutation_seconds": 0.0,
                    "session_id": exc.session_id,
                    "session_ids": list(batch_session_ids),
                    "generation_started_at": generation_started_at,
                    "generation_finished_at": generation_finished_at,
                    "rate_limit": exc.rate_limit,
                }
            return {
                "results": new_results,
                "finished": True,
                "stopped_early": True,
                "current_batch": [],
                "current_stage": _delegated_gate_failure_stage({}),
                "phase_timings": _merge_phase_timings(
                    state,
                    generate_validate_seconds=elapsed,
                    generation_session_seconds=generation_seconds,
                    compile_verification_seconds=compile_seconds,
                    test_execution_seconds=generation_test_seconds,
                ),
            }

        session_retrospect = _capture_session_retrospect(
            state=state,
            repo_path=repo_path,
            client=client,
            session_ids=batch_session_ids,
        )
        phase_token_usage = _capture_phase_token_usage(
            state=state,
            client=client,
            phase_session_ids=phase_session_ids,
        )
        session_token_usage = _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=batch_session_ids,
        )
        new_results = state["results"].copy()
        primary_session_id = (delegated_gate_session_ids or [generation_test_session_id or compile_fix_session_id or generation_session_id])[-1]
        gate_output = _delegated_quality_gate_feedback(gate_result)
        for class_fqn in batch:
            test_file_rel = _expected_test_file_rel(module, class_fqn)
            test_file_abs = Path(repo_path) / test_file_rel
            try:
                test_file_content = test_file_abs.read_text(encoding="utf-8", errors="replace")
            except Exception:
                test_file_content = ""
            new_results[class_fqn] = {
                "status": "PASS" if gate_ok else "FAIL",
                "coverage": None,
                "tests_pass": bool(gate_ok),
                "mutation_score": None,
                "surviving_mutants": 0,
                "total_mutants": 0,
                "output": gate_output[:2000],
                "test_file_path": test_file_rel,
                "test_file_content": test_file_content,
                "elapsed_seconds": generation_seconds + compile_seconds + generation_test_seconds + gate_seconds,
                "generation_seconds": generation_seconds,
                "compile_seconds": compile_seconds,
                "test_seconds": generation_test_seconds,
                "mutation_seconds": gate_seconds,
                "session_id": primary_session_id,
                "session_ids": list(batch_session_ids),
                "generation_started_at": generation_started_at,
                "generation_finished_at": generation_finished_at,
                "delegated_quality_gate": gate_result,
            }
        return {
            "results": new_results,
            "current_stage": _delegated_gate_failure_stage(gate_result),
            "session_retrospect": session_retrospect,
            "session_token_usage": session_token_usage,
            "phase_token_usage": phase_token_usage,
            "phase_timings": _merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                generation_session_seconds=generation_seconds,
                compile_verification_seconds=compile_seconds,
                test_execution_seconds=generation_test_seconds,
                mutation_seconds=gate_seconds,
            ),
        }

    # --- Step 3: Initial batch test execution with Jacoco coverage ---
    logger.info("Step 3: Batch test execution with Jacoco coverage")
    _set_stage(state, "test_execution", f"batch={len(batch)}")
    batch_test_started = time.perf_counter()
    batch_test_names = [f"{class_fqn.split('.')[-1]}Test" for class_fqn in batch]
    batch_test_ok, batch_test_output = run_tests_with_jacoco_batch(repo_path, batch_test_names, module)
    batch_test_seconds = time.perf_counter() - batch_test_started
    jacoco_path = find_jacoco_report(repo_path, module)
    batch_surefire_results = parse_surefire_results(repo_path, batch_test_names, module)
    batch_line_coverage = {
        class_fqn: (parse_jacoco_report(jacoco_path, class_fqn).get("line", 0.0) if jacoco_path else 0.0)
        for class_fqn in batch
    }

    # --- Steps 4-5: Per-class validation (repair only as needed, then mutation) ---
    new_results = state["results"].copy()
    primary_session_id = generation_test_session_id or compile_fix_session_id or generation_session_id
    for class_fqn in batch:
        class_started = time.perf_counter()
        test_class_name = f"{class_fqn.split('.')[-1]}Test"
        test_file_rel = _expected_test_file_rel(module, class_fqn)
        test_file_abs = Path(repo_path) / test_file_rel

        logger.info("[%s] Step 4: Test execution / coverage evaluation", class_fqn)
        _set_stage(state, "test_execution", class_fqn)
        line_cov = batch_line_coverage.get(class_fqn, 0.0)
        shared_test_seconds = batch_test_seconds / max(len(batch), 1)
        test_started = time.perf_counter()
        class_test_result = batch_surefire_results.get(test_class_name, {})
        test_ok = bool(class_test_result.get("passed", batch_test_ok))
        test_output = class_test_result.get("output", "")
        if not test_output and not batch_test_ok:
            test_output = batch_test_output

        if not test_ok:
            logger.warning("[%s] Tests failed:\n%s", class_fqn, test_output[:1000])
            class_test_fix_session_id = _create_phase_session(
                state=state,
                client=client,
                session_ids=batch_session_ids,
                model_id=uta_settings.opencode_model,
            )
            class_progress = _session_progress_logger([class_fqn], session_id=class_test_fix_session_id, stage="test_fix")
            fix_msg = (
                f"The test `{test_class_name}` compiled but failed when running.\n\n"
                f"### TEST FAILURES\n```\n{test_output}\n```\n\n"
                f"Fix the test file so all tests pass. Edit the existing file — do not create a new one.\n"
                f"If this is an actor/message-driven test, prefer replacing risky framework/carrier mocks "
                f"(for example `DiffEvent`, `ActorMessage`, `OrderData`, or similar message wrappers) "
                f"with real/minimal objects or tiny fake implementations instead of Mockito mocks.\n"
                f"Previous generation/test session: `{primary_session_id}`.\n"
                f"After fixing, run: mvn test -Dtest={test_class_name} -Dsurefire.failIfNoSpecifiedTests=false{maven_module_flag}"
            )
            fix_msg += _stage_introspect_section(repo_path, "test_fix")
            client.send_message(class_test_fix_session_id, fix_msg, model_id=uta_settings.opencode_model)
            try:
                _raise_for_rate_limit_event(
                    event=_poll_with_continue_recovery(
                        client=client,
                        session_id=class_test_fix_session_id,
                        timeout=_llm_repair_timeout(uta_settings.opencode_model),
                        phase="test_fix",
                        batch=[class_fqn],
                        on_update=class_progress if uta_settings.opencode_stream_progress else None,
                        state=state,
                    ),
                    session_id=class_test_fix_session_id,
                    client=client,
                    phase="test_fix",
                )
            except ProviderRateLimitError as exc:
                elapsed_seconds = time.perf_counter() - class_started
                new_results[class_fqn] = {
                    "status": "PROVIDER_RATE_LIMITED",
                    "coverage": line_cov,
                    "tests_pass": False,
                    "mutation_score": 0.0,
                    "surviving_mutants": 0,
                    "output": exc.output,
                    "test_file_path": test_file_rel,
                    "elapsed_seconds": elapsed_seconds,
                    "generation_seconds": generation_seconds,
                    "compile_seconds": compile_seconds,
                    "test_seconds": shared_test_seconds + (time.perf_counter() - test_started),
                    "mutation_seconds": 0.0,
                    "session_id": exc.session_id,
                    "session_ids": list(batch_session_ids),
                    "generation_started_at": generation_started_at,
                    "generation_finished_at": generation_finished_at,
                    "rate_limit": exc.rate_limit,
                }
                logger.warning("[%s] Test-fix hit provider/model rate limit", class_fqn)
                continue
            test_ok, test_output = run_test_with_jacoco(repo_path, test_class_name, module)
            jacoco_path = find_jacoco_report(repo_path, module)
            if jacoco_path:
                cov_stats = parse_jacoco_report(jacoco_path, class_fqn)
                line_cov = cov_stats.get("line", 0.0)
        test_seconds = shared_test_seconds + (time.perf_counter() - test_started)

        if test_ok:
            logger.info("[%s] Tests PASSED", class_fqn)
        else:
            logger.error("[%s] Tests FAILED after fix attempt:\n%s", class_fqn, test_output[:1000])

        logger.info("[%s] Line coverage: %.1f%%", class_fqn, line_cov)

        if test_ok and line_cov < coverage_gate:
            node = graph.nodes.get(class_fqn)
            source_file_abs = Path(node.file_path) if node and getattr(node, "file_path", None) else None
            target_paths = target_context_paths.get(class_fqn, {})
            coverage_started = time.perf_counter()
            try:
                coverage_ok, line_cov, coverage_output, coverage_session_ids = run_coverage_fix_loop(
                    repo_path=repo_path,
                    module=module,
                    class_fqn=class_fqn,
                    session_id=primary_session_id,
                    client=client,
                    test_class_name=test_class_name,
                    test_file_abs=test_file_abs,
                    source_file_abs=source_file_abs,
                    coverage_gate=coverage_gate,
                    current_coverage=line_cov,
                    maven_module_flag=maven_module_flag,
                    target_context_abs=target_paths.get("context_abs", ""),
                    target_symbols_abs=target_paths.get("symbols_abs", ""),
                    roi_abs=target_paths.get("roi_abs", ""),
                    graph=graph,
                    state=state,
                )
                for coverage_session_id in coverage_session_ids:
                    _append_session_id(state, batch_session_ids, coverage_session_id)
                    _append_phase_session_id(phase_session_ids, "coverage_fix", coverage_session_id)
            except ProviderRateLimitError as exc:
                elapsed_seconds = time.perf_counter() - class_started
                new_results[class_fqn] = {
                    "status": "PROVIDER_RATE_LIMITED",
                    "coverage": line_cov,
                    "tests_pass": test_ok,
                    "mutation_score": 0.0,
                    "surviving_mutants": 0,
                    "output": exc.output,
                    "test_file_path": test_file_rel,
                    "elapsed_seconds": elapsed_seconds,
                    "generation_seconds": generation_seconds,
                    "compile_seconds": compile_seconds,
                    "test_seconds": shared_test_seconds + (time.perf_counter() - test_started),
                    "mutation_seconds": 0.0,
                    "session_id": exc.session_id,
                    "session_ids": list(batch_session_ids),
                    "generation_started_at": generation_started_at,
                    "generation_finished_at": generation_finished_at,
                    "rate_limit": exc.rate_limit,
                }
                logger.warning("[%s] Coverage-fix hit provider/model rate limit", class_fqn)
                continue
            extra_test_seconds = time.perf_counter() - coverage_started
            test_seconds += extra_test_seconds
            if coverage_output:
                test_output = coverage_output
            if coverage_ok:
                logger.info("[%s] Coverage gate reached after hardening: %.1f%%", class_fqn, line_cov)
            else:
                logger.warning("[%s] Coverage remains below gate after hardening: %.1f%% < %d%%", class_fqn, line_cov, coverage_gate)

        # --- Step 4: Mutation testing (Pitest) ---
        mutation_score = 0.0
        surviving_mutants = 0
        mutation_stats: Dict[str, Any] = {}
        mutation_started = time.perf_counter()
        if _should_run_mutation(test_ok, mutation_gate_score):
            logger.info("[%s] Step 4: Mutation testing (Pitest)", class_fqn)
            _set_stage(state, "mutation_testing", class_fqn)
            if line_cov < coverage_gate:
                logger.info(
                    "[%s] Coverage %.1f%% is still below gate %d%% after hardening, "
                    "but continuing into mutation testing before final failure",
                    class_fqn,
                    line_cov,
                    coverage_gate,
                )
            package = ".".join(class_fqn.split(".")[:-1])
            test_class_fqn = f"{package}.{test_class_name}"
            node = graph.nodes.get(class_fqn)
            source_file_abs = Path(node.file_path) if node and getattr(node, "file_path", None) else None
            target_paths = target_context_paths.get(class_fqn, {})
            mutation_rate_limited = False

            max_mutation_attempts = _mutation_enhancement_attempts()
            for attempt in range(1, max_mutation_attempts + 1):
                pitest_ok, pitest_output = run_pitest(repo_path, class_fqn, test_class_fqn, module)

                if not pitest_ok:
                    green_suite_failure = parse_pitest_green_suite_failure(pitest_output)
                    if green_suite_failure:
                        logger.warning(
                            "[%s] PIT precheck reported a non-green suite on attempt %d; routing into mutation test-fix",
                            class_fqn,
                            attempt,
                        )
                        mutation_test_fix_ok, mutation_test_output, mutation_test_fix_session_id = _run_mutation_test_fix_loop(
                            repo_path=repo_path,
                            module=module,
                            class_fqn=class_fqn,
                            test_class_name=test_class_name,
                            current_output=pitest_output,
                            client=client,
                            maven_module_flag=maven_module_flag,
                            state=state,
                        )
                        _append_session_id(state, batch_session_ids, mutation_test_fix_session_id)
                        _append_phase_session_id(phase_session_ids, "mutation_test_fix", mutation_test_fix_session_id)
                        if mutation_test_fix_ok:
                            logger.info(
                                "[%s] Mutation test-fix restored targeted test execution after PIT precheck failure; rerunning PIT",
                                class_fqn,
                            )
                            if mutation_test_output:
                                test_output = mutation_test_output
                            continue
                        if mutation_test_output:
                            test_output = mutation_test_output
                            pitest_output = mutation_test_output
                    logger.error("[%s] Pitest failed to run (attempt %d): %s", class_fqn, attempt, pitest_output[:200])
                    break
                
                report_path = find_latest_pitest_report(repo_path, module)
                if not report_path:
                    logger.warning("[%s] No Pitest report found after run (attempt %d)", class_fqn, attempt)
                    break
                
                stats = compute_mutation_stats(report_path, class_fqn)
                mutation_stats = stats
                mutation_score = stats["score"]
                surviving_mutants = stats["survived"]
                survivors = parse_pitest_report(report_path, class_fqn)
                
                logger.info("[%s] Mutation (attempt %d): %d total, %d killed, %d survived — score %.1f%%",
                            class_fqn, attempt, stats["total"], stats["killed"],
                            stats["survived"], mutation_score)

                if mutation_score >= mutation_gate_score or not survivors:
                    break
                
                if attempt < max_mutation_attempts:
                    logger.info("[%s] Mutation score %.1f%% < gate %d%%, sending enhancement prompt (attempt %d)",
                                class_fqn, mutation_score, mutation_gate_score, attempt)
                    try:
                        patched = False
                        for followup_round in range(1, 3):
                            mutation_fix_result = _run_focused_mutation_fix_round(
                                repo_path=repo_path,
                                module=module,
                                class_fqn=class_fqn,
                                session_client=client,
                                source_file_abs=source_file_abs,
                                test_file_abs=test_file_abs,
                                target_context_abs=target_paths.get("context_abs", ""),
                                target_symbols_abs=target_paths.get("symbols_abs", ""),
                                current_coverage=line_cov,
                                mutation_gate_score=mutation_gate_score,
                                attempt=attempt,
                                mutation_score=mutation_score,
                                mutation_stats=mutation_stats,
                                report_path=report_path,
                                method_efforts=method_efforts_by_class.get(class_fqn),
                                state=state,
                            )
                            mutation_fix_session_id = mutation_fix_result.get("session_id")
                            _append_session_id(state, batch_session_ids, mutation_fix_session_id)
                            _append_phase_session_id(phase_session_ids, "mutation_fix", mutation_fix_session_id)
                            patched = bool(mutation_fix_result.get("patched", False))
                            if patched:
                                break
                            ranked_methods = [m for m in mutation_fix_result.get("ranked_methods", []) if m]
                            logger.warning(
                                "[%s] Mutation round %d follow-up %d produced diagnosis only (no patch). "
                                "Escalating to the next ranked survivor family. Ranked methods: %s",
                                class_fqn,
                                attempt,
                                followup_round,
                                ", ".join(ranked_methods[:5]) or "unknown",
                            )
                            if followup_round >= 2:
                                logger.warning(
                                    "[%s] Mutation round %d exhausted diagnosis-only escalations for this PIT snapshot",
                                    class_fqn,
                                    attempt,
                                )
                    except ProviderRateLimitError as exc:
                        elapsed_seconds = time.perf_counter() - class_started
                        new_results[class_fqn] = {
                            "status": "PROVIDER_RATE_LIMITED",
                            "coverage": line_cov,
                            "tests_pass": test_ok,
                            "mutation_score": mutation_score,
                            "surviving_mutants": surviving_mutants,
                            "total_mutants": mutation_stats.get("total", 0),
                            "killed_mutants": mutation_stats.get("killed", 0),
                            "no_coverage_mutants": mutation_stats.get("no_coverage", 0),
                            "timed_out_mutants": mutation_stats.get("timed_out", 0),
                            "non_viable_mutants": mutation_stats.get("non_viable", 0),
                            "memory_error_mutants": mutation_stats.get("memory_error", 0),
                            "run_error_mutants": mutation_stats.get("run_error", 0),
                            "mutation_status_counts": mutation_stats.get("status_counts", {}),
                            "output": exc.output,
                            "test_file_path": test_file_rel,
                            "elapsed_seconds": elapsed_seconds,
                            "generation_seconds": generation_seconds,
                            "compile_seconds": compile_seconds,
                            "test_seconds": test_seconds,
                            "mutation_seconds": time.perf_counter() - mutation_started,
                            "session_id": exc.session_id,
                            "session_ids": list(batch_session_ids),
                            "generation_started_at": generation_started_at,
                            "generation_finished_at": generation_finished_at,
                            "rate_limit": exc.rate_limit,
                        }
                        logger.warning("[%s] Mutation-fix hit provider/model rate limit", class_fqn)
                        mutation_rate_limited = True
                        break
                else:
                    logger.info("[%s] Reached maximum mutation enhancement attempts (%d)", class_fqn, attempt)
            if mutation_rate_limited:
                continue
        elif not test_ok:
            logger.info("[%s] Skipping mutation testing — tests did not pass", class_fqn)
        else:
            logger.info("[%s] Mutation testing disabled (gate=0)", class_fqn)
        mutation_seconds = time.perf_counter() - mutation_started

        # --- Read generated test file for result record ---
        test_file_content = ""
        if test_file_abs.exists():
            try:
                test_file_content = test_file_abs.read_text(errors="replace")
                logger.info("[%s] Generated test file: %s (%d lines)",
                            class_fqn, test_file_rel, test_file_content.count("\n"))
            except Exception as e:
                logger.warning("[%s] Could not read test file: %s", class_fqn, e)
        else:
            logger.warning("[%s] Expected test file not found at %s", class_fqn, test_file_abs)

        status = _status_with_mutation_gate(
            test_ok=test_ok,
            line_cov=line_cov,
            coverage_gate=coverage_gate,
            mutation_gate_score=mutation_gate_score,
            mutation_score=mutation_score,
        )
        elapsed_seconds = time.perf_counter() - class_started
        new_results[class_fqn] = {
            "status": status,
            "coverage": line_cov,
            "tests_pass": test_ok,
            "mutation_score": mutation_score,
            "surviving_mutants": surviving_mutants,
            "total_mutants": mutation_stats.get("total", 0),
            "killed_mutants": mutation_stats.get("killed", 0),
            "no_coverage_mutants": mutation_stats.get("no_coverage", 0),
            "timed_out_mutants": mutation_stats.get("timed_out", 0),
            "non_viable_mutants": mutation_stats.get("non_viable", 0),
            "memory_error_mutants": mutation_stats.get("memory_error", 0),
            "run_error_mutants": mutation_stats.get("run_error", 0),
            "mutation_status_counts": mutation_stats.get("status_counts", {}),
            "output": event.get("result", "")[:2000],
            "test_file_path": test_file_rel,
            "test_file_content": test_file_content,
            "elapsed_seconds": elapsed_seconds,
            "generation_seconds": generation_seconds,
            "compile_seconds": compile_seconds,
            "test_seconds": test_seconds,
            "mutation_seconds": mutation_seconds,
            "session_id": primary_session_id,
            "session_ids": list(batch_session_ids),
            "generation_started_at": generation_started_at,
            "generation_finished_at": generation_finished_at,
        }
        logger.info("[%s] Result: status=%s, coverage=%.1f%%, mutation=%.1f%%, survived=%d",
                    class_fqn, status, line_cov, mutation_score, surviving_mutants)

    session_retrospect = _capture_session_retrospect(
        state=state,
        repo_path=repo_path,
        client=client,
        session_ids=batch_session_ids,
    )
    phase_token_usage = _capture_phase_token_usage(
        state=state,
        client=client,
        phase_session_ids=phase_session_ids,
    )
    try:
        from uta.learning import record_run_inefficiencies
        for class_fqn in batch:
            record_run_inefficiencies(
                repo_path=repo_path,
                class_fqn=class_fqn,
                phase_tokens=phase_token_usage,
                repeated_tools=list((session_retrospect or {}).get("repeated_tools") or []),
            )
    except Exception:
        logger.debug("L1 phase token recording skipped", exc_info=True)

    result_payload = {
        "results": new_results,
        "current_class": None,
        "session_retrospect": session_retrospect,
        "session_token_usage": _capture_session_token_usage(
            state=state,
            client=client,
            session_ids=batch_session_ids,
        ),
        "phase_token_usage": phase_token_usage,
        "current_stage": "generate_and_validate",
        "phase_timings": _merge_phase_timings(
            state,
            generate_validate_seconds=time.perf_counter() - node_started,
            generation_session_seconds=generation_seconds,
            compile_verification_seconds=compile_seconds,
            test_execution_seconds=sum(
                float(new_results[fqn].get("test_seconds", 0.0) or 0.0) for fqn in batch
            ),
            mutation_seconds=sum(
                float(new_results[fqn].get("mutation_seconds", 0.0) or 0.0) for fqn in batch
            ),
        ),
    }
    if any((new_results.get(fqn) or {}).get("status") == "PROVIDER_RATE_LIMITED" for fqn in batch):
        result_payload.update({"finished": True, "stopped_early": True, "current_batch": []})
    return result_payload


def commit_to_branch(state: AgentState) -> Dict[str, Any]:
    """Commit generated test files from the current batch and cache to the configured branch."""
    repo_path = state["repo_path"]
    results = state.get("results", {})
    batch = state.get("current_batch") or []
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    manager = None
    ci_auto_push_context = None
    _set_stage(state, "commit_to_branch", "stage generated files")

    files_to_add: List[str] = []
    summary_parts: List[str] = []

    if not batch:
        logger.warning("commit_to_branch: empty current_batch — skipping (avoid staging unrelated results)")
        return {}

    if task_id and task_db_path:
        from uta.tasks.manager import TaskManager

        manager = TaskManager(task_db_path)
        ci_auto_push_context = _ci_auto_push_context(manager, int(task_id), state.get("branch_name", "unit-code-gen"), batch)

    for fqn in batch:
        res = results.get(fqn) or {}
        test_file = res.get("test_file_path")
        if not test_file:
            continue
        test_abs = Path(repo_path) / test_file
        if test_abs.exists():
            files_to_add.append(test_file)
        short = fqn.split(".")[-1]
        st = res.get("status", "?")
        cov = res.get("coverage")
        cov_label = "n/a"
        if cov is not None:
            try:
                cov_label = f"{float(cov):.0f}%"
            except (TypeError, ValueError):
                cov_label = "n/a"
        summary_parts.append(f"{short}[{st},{cov_label}]")

    if not ci_auto_push_context:
        cache_dir = Path(repo_path) / ".uta_cache"
        if cache_dir.exists():
            files_to_add.append(".uta_cache/")

    for rel_path in state.get("deterministic_change_paths") or []:
        candidate = Path(repo_path) / rel_path
        if candidate.exists():
            files_to_add.append(rel_path)

    if ci_auto_push_context:
        return _commit_ci_repair_to_branch(
            state,
            manager=manager,
            context=ci_auto_push_context,
            class_fqns=batch,
            results=results,
        )

    if not files_to_add:
        return {}

    files_to_add = list(dict.fromkeys(files_to_add))
    _git_run(repo_path, "add", *files_to_add, capture_output=True, check=False)

    if summary_parts:
        msg = f"uta: tests — {', '.join(summary_parts)}"
    else:
        msg = "uta: cache and dependency updates"

    result = _git_run(
        repo_path,
        "commit",
        "-m",
        msg,
        "--allow-empty",
        capture_output=True, check=False,
    )
    commit_sha = None
    if result.returncode == 0:
        logger.info("Committed: %s", msg)
        head = _git_run(
            repo_path,
            "rev-parse",
            "HEAD",
            capture_output=True,
            check=False,
            text=True,
        )
        if head.returncode == 0:
            commit_sha = head.stdout.strip()
    else:
        stderr = result.stderr.decode(errors="replace") if result.stderr else ""
        if "nothing to commit" not in stderr:
            logger.warning("Commit failed: %s", stderr[:200])
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager

            manager = manager or TaskManager(task_db_path)
            manager.record_commit(
                int(task_id),
                class_fqns=batch,
                commit_sha=commit_sha,
            )
            branch_name = state.get("branch_name", "unit-code-gen")
            if commit_sha:
                push = _push_branch_with_rebase_retry(repo_path, branch_name)
                if push.returncode == 0:
                    remote_head_result = _git_run(
                        repo_path,
                        "ls-remote",
                        "origin",
                        f"refs/heads/{branch_name}",
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    remote_head = ""
                    if remote_head_result.returncode == 0 and remote_head_result.stdout.strip():
                        remote_head = remote_head_result.stdout.split()[0].strip()
                    manager.record_push_verified(
                        int(task_id),
                        branch_name=branch_name,
                        local_head=commit_sha,
                        remote_head=remote_head,
                    )
                    manager.record_commit(
                        int(task_id),
                        class_fqns=batch,
                        commit_sha=commit_sha,
                        pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        remote_ref=remote_head,
                    )
                else:
                    stderr = ((push.stderr or "") + (push.stdout or ""))[:500]
                    manager.record_push_failed(
                        int(task_id),
                        branch_name=branch_name,
                        message=stderr or "git push failed",
                        class_fqns=batch,
                    )
            # Sync per-batch status/metrics to DB immediately so monitoring
            # reflects the true class outcome without waiting for pipeline end.
            batch_results = {fqn: results[fqn] for fqn in batch if fqn in results}
            if batch_results:
                manager.sync_results(
                    int(task_id),
                    batch_results,
                    module=state.get("module"),
                    phase_token_usage=state.get("phase_token_usage"),
                    elapsed_seconds=None,
                )
        except Exception:
            logger.debug("Failed to record production task commit", exc_info=True)

    return {"current_stage": "commit_to_branch"}


def _ci_auto_push_context(manager: Any, task_id: int, branch_name: str, class_fqns: List[str]) -> Optional[Any]:
    from uta.tasks.autopush import ci_auto_push_context_from_task

    return ci_auto_push_context_from_task(manager, task_id, branch_name, class_fqns)


def _commit_ci_repair_to_branch(
    state: AgentState,
    *,
    manager: Any,
    context: Any,
    class_fqns: List[str],
    results: Dict[str, Any],
) -> Dict[str, Any]:
    from uta.ci_plugin.auto_push import AutoPushConflictError, AutoPushPolicyError
    from uta.tasks.autopush import commit_ci_repair_results, record_existing_repair_commit

    task_id = int(state["task_id"])
    try:
        commit_ci_repair_results(
            repo=state["repo_path"],
            manager=manager,
            task_id=task_id,
            branch_name=context.branch_name,
            results=results,
            target_ids=class_fqns,
            ci_context=None,
            module=state.get("module"),
            phase_token_usage=state.get("phase_token_usage"),
        )
    except AutoPushPolicyError as exc:
        if str(exc) == "CI repair auto-push found no test changes to commit":
            if record_existing_repair_commit(
                manager=manager,
                task_id=task_id,
                target_ids=class_fqns,
                results=results,
                module=state.get("module"),
                phase_token_usage=state.get("phase_token_usage"),
            ):
                return {"current_stage": "commit_to_branch"}
            manager.record_commit(
                task_id,
                class_fqns=class_fqns,
                commit_sha=None,
                remote_ref=None,
            )
            return {"current_stage": "commit_to_branch"}
        manager.record_push_failed(
            task_id,
            branch_name=context.branch_name,
            message=str(exc),
            class_fqns=class_fqns,
        )
        raise TaskUnsafeDiffError(str(exc)) from exc
    except AutoPushConflictError as exc:
        manager.record_push_failed(
            task_id,
            branch_name=context.branch_name,
            message=str(exc),
            class_fqns=class_fqns,
        )
        raise

    return {"current_stage": "commit_to_branch"}


def _push_branch_with_rebase_retry(repo_path: str, branch_name: str) -> tuple:
    push = _git_run(
        repo_path,
        "push",
        "-u",
        "origin",
        branch_name,
        capture_output=True,
        check=False,
        text=True,
    )
    if push.returncode == 0:
        return push
    combined = f"{push.stdout or ''}\n{push.stderr or ''}".lower()
    retryable = any(marker in combined for marker in ("fetch first", "non-fast-forward", "rejected"))
    if not retryable:
        return push
    _abort_in_progress_rebase(repo_path)
    checkout = _git_run(
        repo_path,
        "checkout",
        branch_name,
        capture_output=True,
        check=False,
        text=True,
    )
    if checkout.returncode != 0:
        return checkout
    stash = _git_run(
        repo_path,
        "stash",
        "save",
        "-u",
        "uta-autostash-before-push",
        capture_output=True,
        check=False,
        text=True,
    )
    stash_output = f"{stash.stdout or ''}\n{stash.stderr or ''}"
    stashed = stash.returncode == 0 and "No local changes" not in stash_output
    rebase = _git_run(
        repo_path,
        "pull",
        "--rebase",
        "origin",
        branch_name,
        capture_output=True,
        check=False,
        text=True,
    )
    if rebase.returncode != 0:
        if stashed:
            _git_run(repo_path, "stash", "pop", capture_output=True, check=False, text=True)
        return rebase
    retry = _git_run(
        repo_path,
        "push",
        "-u",
        "origin",
        branch_name,
        capture_output=True,
        check=False,
        text=True,
    )
    if stashed:
        _git_run(repo_path, "stash", "pop", capture_output=True, check=False, text=True)
    return retry


def _abort_in_progress_rebase(repo_path: str) -> None:
    git_dir_result = _git_run(
        repo_path,
        "rev-parse",
        "--git-dir",
        capture_output=True,
        check=False,
        text=True,
    )
    if git_dir_result.returncode != 0:
        return
    raw_git_dir = (git_dir_result.stdout or "").strip()
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = Path(repo_path) / git_dir
    if not ((git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists()):
        return
    _git_run(
        repo_path,
        "rebase",
        "--abort",
        capture_output=True,
        check=False,
        text=True,
    )


def store_and_push(state: AgentState) -> Dict[str, Any]:
    """Store final report and push the configured generation branch to remote."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    results = state.get("results", {})
    branch_name = state.get("branch_name", "unit-code-gen")
    _set_stage(state, "store_and_push", f"save report and push {branch_name}")

    if not results:
        return {}

    # Save report
    import datetime
    from uta.output.reporter import Reporter

    report_dir = Path(repo_path) / ".uta_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    module = state.get("module", "all")
    report_path = report_dir / f"summary_{module}_{timestamp}.json"
    reporter = Reporter(repo_path)

    # L3: build/update project-level learning summary from all per-class JSONL records.
    regression_warnings: List[str] = []
    try:
        from uta.learning import build_project_summary, check_phase_regression
        build_project_summary(repo_path)
        observed_phase_tokens = state.get("phase_token_usage") or {}
        if not observed_phase_tokens:
            session_token_usage = state.get("session_token_usage") or {}
            total_tokens = session_token_usage.get("total_tokens") or {}
            if total_tokens:
                observed_phase_tokens = {"generate_validate": {
                    k: int(total_tokens.get(k) or 0)
                    for k in ("input", "output", "cache_read", "cache_write", "total")
                }}
        if observed_phase_tokens:
            regression_warnings = check_phase_regression(repo_path, observed_phase_tokens)
            for w in regression_warnings:
                logger.warning(w)
    except Exception:
        logger.debug("L3/L4 project summary update skipped", exc_info=True)

    metadata = {
        "repo_path": repo_path,
        "module": module,
        "branch_name": branch_name,
        "total_candidates": len(state.get("candidates", [])),
        "session_retrospect": state.get("session_retrospect", {}),
        "session_token_usage": state.get("session_token_usage", {}),
        "phase_token_usage": state.get("phase_token_usage", {}),
        "phase_timings": _merge_phase_timings(
            state,
            store_and_push_seconds=time.perf_counter() - started,
        ),
        "total_elapsed_seconds": time.time() - float(state.get("started_at", time.time())),
        "regression_warnings": regression_warnings,
        "deterministic_change_paths": state.get("deterministic_change_paths") or [],
    }
    reporter.save_report(results, report_path.name, metadata=metadata)
    logger.info("Report saved to %s", report_path)

    # Incrementally roll up token/cost totals to repo_task so the live dashboard
    # shows non-zero values after each batch (not just at the very end).
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager, _estimate_cost_from_tokens
            _mgr = TaskManager(task_db_path)
            _st = state.get("session_token_usage") or {}
            _pt = state.get("phase_token_usage") or {}
            _totals = _mgr._token_totals(_st, _pt)
            _provider_cost = _mgr._extract_provider_cost(_st, _pt)
            _task_cfg = _mgr.db.get_repo_task(int(task_id)) or {}
            _cfg_snap = json.loads((_task_cfg.get("config_snapshot_json") or "{}"))
            _model = _cfg_snap.get("opencode_model") or uta_settings.opencode_model
            _est_cost = _provider_cost if _provider_cost is not None else _estimate_cost_from_tokens(
                model=_model,
                input_tokens=_totals["input"],
                output_tokens=_totals["output"],
                cache_read_tokens=_totals["cache_read"],
                reasoning_tokens=_totals["reasoning"],
            )
            _mgr.db.update_repo_task(
                int(task_id),
                input_tokens=_totals["input"],
                output_tokens=_totals["output"],
                cache_read_tokens=_totals["cache_read"],
                cache_write_tokens=_totals["cache_write"],
                reasoning_tokens=_totals["reasoning"],
                total_tokens=sum(_totals.values()),
                actual_input_tokens=_totals["input"],
                actual_output_tokens=_totals["output"],
                actual_cache_read_tokens=_totals["cache_read"],
                actual_cache_write_tokens=_totals["cache_write"],
                actual_cost=_est_cost,
                provider_cost_usd=_provider_cost,
            )
        except Exception:
            logger.debug("Failed to write incremental token rollup to repo_task", exc_info=True)

    # Reports are runtime artifacts; keep them in the workspace but never commit them.
    result = _push_branch_with_rebase_retry(repo_path, branch_name)
    if result.returncode == 0:
        logger.info("Pushed %s to origin", branch_name)
        local_head = _git_run(
            repo_path,
            "rev-parse",
            "HEAD",
            capture_output=True,
            check=False,
            text=True,
        ).stdout.strip()
        remote_head_result = _git_run(
            repo_path,
            "ls-remote",
            "origin",
            f"refs/heads/{branch_name}",
            capture_output=True,
            check=False,
            text=True,
        )
        remote_head = ""
        if remote_head_result.returncode == 0 and remote_head_result.stdout.strip():
            remote_head = remote_head_result.stdout.split()[0].strip()
        task_id = state.get("task_id")
        task_db_path = state.get("task_db_path")
        if task_id and task_db_path:
            try:
                from uta.tasks.manager import TaskManager

                TaskManager(task_db_path).record_push_verified(
                    int(task_id),
                    branch_name=branch_name,
                    local_head=local_head,
                    remote_head=remote_head,
                )
            except Exception:
                logger.debug("Failed to record production task push verification", exc_info=True)
    else:
        stderr = ((result.stderr or "") + (result.stdout or ""))[:500]
        logger.warning("Push failed (may be expected in local-only repos): %s", stderr[:200])
        task_id = state.get("task_id")
        task_db_path = state.get("task_db_path")
        if task_id and task_db_path:
            try:
                from uta.tasks.manager import TaskManager

                TaskManager(task_db_path).record_push_failed(
                    int(task_id),
                    branch_name=branch_name,
                    message=stderr or "git push failed",
                    class_fqns=list(results.keys()),
                )
            except Exception:
                logger.debug("Failed to record production task push failure", exc_info=True)

    return {
        "current_stage": "store_and_push",
        "phase_timings": _merge_phase_timings(
            state,
            store_and_push_seconds=time.perf_counter() - started,
        )
    }
