"""Commit, report, and publish artifacts produced by test generation."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from uta.shared.delivery import (
    PushConflictError,
    PushPolicyError,
    RdcDeliveryContext,
    result_targets_and_paths,
)
from uta.testgen.git import output_text as _output_text
from uta.testgen.git import run_git as _git_run
from uta.testgen.graph.state import AgentState
from uta.testgen.ports.registry import task_ports_from_state
from uta.testgen.progress import merge_phase_timings as _merge_phase_timings
from uta.testgen.progress import set_stage as _set_stage
from uta.testgen.targets import active_target_ids
from uta.testgen.workspace_guard import TaskUnsafeDiffError, workspace_policy_for
from uta.testgen.workspace_guard import extra_authored_test_paths as _extra_authored_test_paths
from uta.testgen.workspace_guard import git_status_paths as _git_status_paths

logger = logging.getLogger("uta")


# Prompt artifacts can contain source-derived or provider-sensitive text. They
# are application state, never a generated repository deliverable. Keep these
# exclusions on the Git command itself so a broad, historically supported
# `.uta_cache/` add cannot pick up a stale legacy or crash-leftover artifact.
_PROMPT_ARTIFACT_PATHSPECS = (
    ":(exclude,glob)agent_turns/**",
    ":(exclude,glob)**/agent_turns/**",
    ":(exclude,glob)cycle-prompts/**",
    ":(exclude,glob)**/cycle-prompts/**",
    ":(exclude,glob)workflow-state/**",
    ":(exclude,glob)**/workflow-state/**",
    ":(exclude,glob).uta_cache/**/inputs.json",
    ":(exclude,glob)**/.uta_cache/**/inputs.json",
)


def _resolve_push_branch(state, ports, task_id) -> str:
    """The branch delivery should push, preferring what the run itself knows."""
    from_state = str(state.get("branch_name") or "").strip()
    if from_state:
        return from_state
    if ports is not None and task_id:
        try:
            if hasattr(ports, "get_task_branch"):
                from_task = ports.get_task_branch(str(task_id))
                if from_task:
                    return from_task
            if hasattr(ports, "get_task"):
                task = ports.get_task(int(task_id))
                from_task = str((task or {})["branch_name"] or "").strip() if task else ""
                if from_task:
                    return from_task
            if hasattr(ports, "get_repo_task"):
                task = ports.get_repo_task(int(task_id))
                from_task = str((task or {})["branch_name"] or "").strip() if task else ""
                if from_task:
                    return from_task
        except Exception:  # noqa: BLE001 - never fail delivery over a lookup
            logger.warning("could not read the task branch; using the default", exc_info=True)
    return "unit-code-gen"


def _sync_batch_results(
    ports: Any,
    task_id: Any,
    state: AgentState,
    batch: List[str],
    results: Dict[str, Any],
) -> None:
    """Move this batch's class rows to the state its results describe.

    This node is the only place that does it, so it cannot be conditional on
    delivery having found something to commit. A unit whose checkpoint
    completes while its class rows stay non-terminal is reselected on every
    daemon poll and reused without running anything: an unbreakable loop that
    holds a worker slot until a human notices. Terminalizing is an obligation
    of delivery, not a side effect of a successful commit.

    Both ways of producing that state are loud here. Silence is what let the
    loop reach seventy iterations on beta before anyone looked.
    """
    if not (task_id and ports and hasattr(ports, "sync_results")):
        return
    batch_results = {fqn: results[fqn] for fqn in batch if fqn in results}
    if not batch_results:
        if results:
            # Results exist but under different keys than the batch was
            # selected with. Nothing will ever match, so say which keys.
            logger.warning(
                "task %s produced results for %s but the batch holds %s; no "
                "class row can be synced and the batch will be reselected",
                task_id,
                sorted(results)[:8],
                sorted(batch)[:8],
            )
        return
    try:
        ports.sync_results(
            str(task_id),
            batch_results,
            module=state.get("module"),
            phase_token_usage=state.get("phase_token_usage"),
            elapsed_seconds=None,
        )
    except Exception:
        logger.warning(
            "could not sync generation results for task %s (%d target(s)); "
            "their class rows stay non-terminal and the batch will be "
            "reselected",
            task_id,
            len(batch_results),
            exc_info=True,
        )


def commit_to_branch(state: AgentState) -> Dict[str, Any]:
    """Commit generated test files from the current batch and cache to the configured branch."""
    repo_path = state["repo_path"]
    results = state.get("results", {})
    batch = active_target_ids(state)
    task_id = state.get("task_id")
    ports = task_ports_from_state(state)
    rdc_delivery_context = None
    _set_stage(state, "commit_to_branch", "stage generated files")

    files_to_add: List[str] = []
    summary_parts: List[str] = []

    if not batch:
        logger.warning("commit_to_branch: empty target batch — skipping (avoid staging unrelated results)")
        return {}

    if task_id and ports and hasattr(ports, "get_rdc_delivery_context"):
        rdc_delivery_context = ports.get_rdc_delivery_context(
            str(task_id), _resolve_push_branch(state, ports, task_id), batch
        )

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

    if not rdc_delivery_context:
        cache_dir = Path(repo_path) / ".uta_cache"
        if cache_dir.exists():
            files_to_add.append(".uta_cache/")

    for rel_path in state.get("deterministic_change_paths") or []:
        candidate = Path(repo_path) / rel_path
        if candidate.exists():
            files_to_add.append(rel_path)

    if rdc_delivery_context:
        return _commit_rdc_repair_to_branch(
            state,
            ports=ports,
            context=rdc_delivery_context,
            class_fqns=batch,
            results=results,
        )

    if not files_to_add:
        # A unit that produced no file is still a unit that ran. Its class rows
        # have to reach a terminal state here or nothing else will move them.
        _sync_batch_results(ports, task_id, state, batch, results)
        return {}

    files_to_add = list(dict.fromkeys(files_to_add))
    _git_run(
        repo_path,
        "add",
        "--",
        *files_to_add,
        *_PROMPT_ARTIFACT_PATHSPECS,
        capture_output=True,
        check=False,
    )

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
        stderr = _output_text(result.stderr)
        if "nothing to commit" not in stderr:
            logger.warning("Commit failed: %s", stderr[:200])
    if task_id and ports:
        try:
            if hasattr(ports, "record_commit"):
                ports.record_commit(
                    str(task_id),
                    commit_sha=commit_sha,
                    class_fqns=batch,
                )
            branch_name = _resolve_push_branch(state, ports, task_id)
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
                    if hasattr(ports, "record_push_verified"):
                        ports.record_push_verified(
                            str(task_id),
                            branch_name=branch_name,
                            local_head=commit_sha,
                            remote_head=remote_head,
                        )
                    if hasattr(ports, "record_commit"):
                        ports.record_commit(
                            str(task_id),
                            class_fqns=batch,
                            commit_sha=commit_sha,
                            pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            remote_ref=remote_head,
                        )
                else:
                    stderr = ((push.stderr or "") + (push.stdout or ""))[:500]
                    if hasattr(ports, "record_push_failed"):
                        ports.record_push_failed(
                            str(task_id),
                            branch_name=branch_name,
                            reason=stderr or "git push failed",
                            class_fqns=batch,
                        )
        except Exception:
            logger.warning(
                "could not record the commit for task %s; the class rows are "
                "still synced below",
                task_id,
                exc_info=True,
            )
        # Deliberately outside the block above. Recording a commit and
        # terminalizing the product rows are different obligations, and a push
        # that failed to record is not a reason to strand the unit.
        _sync_batch_results(ports, task_id, state, batch, results)

    return {"current_stage": "commit_to_branch"}


def _commit_rdc_repair_to_branch(
    state: AgentState,
    *,
    ports: Any = None,
    manager: Any = None,
    context: Any,
    class_fqns: List[str],
    results: Dict[str, Any],
) -> Dict[str, Any]:
    ports = ports or manager
    task_id = str(state["task_id"])
    scoped_results = {class_fqn: results[class_fqn] for class_fqn in class_fqns if class_fqn in results}
    _, commit_paths = result_targets_and_paths(state["repo_path"], scoped_results)
    workspace_policy = workspace_policy_for(state, class_fqns)
    for target_id in class_fqns:
        commit_paths.extend(workspace_policy.related_test_artifact_paths(state, target_id))
    deterministic_configs = [
        str(path)
        for path in state.get("deterministic_change_paths") or []
        if workspace_policy.is_repair_config_path(str(path).replace("\\", "/"))
    ]
    current_dirty = _git_status_paths(state["repo_path"])
    python_config_paths = [
        path
        for path in current_dirty
        if workspace_policy.is_repair_config_path(path.replace("\\", "/"))
    ]
    # Also commit shared base/helper test files the repair authored this run (e.g. a
    # superclass the target test extends) that aren't any target's primary test path.
    extra_authored_tests = _extra_authored_test_paths(
        current_dirty,
        state.get("run_initial_dirty_paths") or [],
        commit_paths + deterministic_configs + python_config_paths,
    )
    commit_paths = list(
        dict.fromkeys(commit_paths + deterministic_configs + python_config_paths + extra_authored_tests)
    )
    try:
        import sys
        from uta.shared.delivery import commit_rdc_repair_results as _default_commit, record_existing_repair_commit

        commit_fn = getattr(ports, "commit_rdc_repair_results", None)
        if commit_fn is None:
            rdc_mod = sys.modules.get("uta.tasks.rdc_delivery")
            if rdc_mod and hasattr(rdc_mod, "commit_rdc_repair_results"):
                commit_fn = rdc_mod.commit_rdc_repair_results
            else:
                commit_fn = _default_commit

        branch = (
            context.branch_name
            if hasattr(context, "branch_name")
            else str(context.get("branch_name") or "")
        )
        commit_fn(
            repo=state["repo_path"],
            manager=ports,
            task_id=task_id,
            branch_name=branch,
            results=results,
            target_ids=class_fqns,
            commit_paths=commit_paths,
            delivery_context=context if isinstance(context, RdcDeliveryContext) else None,
            module=state.get("module"),
            phase_token_usage=state.get("phase_token_usage"),
        )
    except PushPolicyError as exc:
        if str(exc) == "RDC repair delivery found no test changes to commit":
            if record_existing_repair_commit(
                manager=ports,
                task_id=task_id,
                target_ids=class_fqns,
                results=results,
                module=state.get("module"),
                phase_token_usage=state.get("phase_token_usage"),
            ):
                return {"current_stage": "commit_to_branch"}
            if ports is not None and hasattr(ports, "record_commit"):
                ports.record_commit(
                    task_id,
                    class_fqns=class_fqns,
                    commit_sha=None,
                    remote_ref=None,
                )
            return {"current_stage": "commit_to_branch"}
        if ports is not None and hasattr(ports, "record_push_failed"):
            ports.record_push_failed(
                task_id,
                branch_name=getattr(context, "branch_name", ""),
                message=str(exc),
                class_fqns=class_fqns,
            )
        raise TaskUnsafeDiffError(str(exc)) from exc
    except PushConflictError as exc:
        if ports is not None and hasattr(ports, "record_push_failed"):
            ports.record_push_failed(
                task_id,
                branch_name=getattr(context, "branch_name", ""),
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
    ports = task_ports_from_state(state)
    branch_name = _resolve_push_branch(state, ports, state.get("task_id"))
    _set_stage(state, "store_and_push", f"save report and push {branch_name}")

    if not results:
        return {}

    # Save report
    import datetime
    from uta.reporting import Reporter

    report_dir = Path(repo_path) / ".uta_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    module = state.get("module", "all")
    report_path = report_dir / f"summary_{module}_{timestamp}.json"
    reporter = Reporter(repo_path)

    # L3: build/update project-level learning summary from all per-class JSONL records.
    regression_warnings: List[str] = []
    try:
        from uta.testgen.learning import build_project_summary, check_phase_regression
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
    if task_id and ports and hasattr(ports, "rollup_tokens_and_costs"):
        try:
            _st = state.get("session_token_usage") or {}
            _pt = state.get("phase_token_usage") or {}
            ports.rollup_tokens_and_costs(str(task_id), _st, _pt)
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
        if task_id and ports and hasattr(ports, "record_push_verified"):
            try:
                ports.record_push_verified(
                    str(task_id),
                    branch_name=branch_name,
                    local_head=local_head,
                    remote_head=remote_head,
                )
            except Exception:
                logger.debug("Failed to record production task push verification", exc_info=True)
    else:
        stderr = ((result.stderr or "") + (result.stdout or ""))[:500]
        logger.warning("Push failed (may be expected in local-only repos): %s", stderr[:200])
        if task_id and ports and hasattr(ports, "record_push_failed"):
            try:
                ports.record_push_failed(
                    str(task_id),
                    branch_name=branch_name,
                    reason=stderr or "git push failed",
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
