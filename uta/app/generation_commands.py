"""Generation-run and gate-resume CLI commands."""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from uta.shared.config import settings
from uta.app.harness_startup import prepare_workspace as _prepare_workspace


def register_generation_commands(main, *, cli_api, console, logger) -> None:
    """Attach generation execution commands to main."""

    @main.command()
    @click.option("--repo", required=False, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--module", default=None, help="Target Maven module name")
    @click.option("--days", default=settings.default_days, help="Scan git log for last N days")
    @click.option("--max-files", default=settings.default_max_files, help="Maximum files to process")
    @click.option("--all", "select_all_files", is_flag=True, help="Use all production files instead of git-history ranking")
    @click.option(
        "--class-fqn",
        "explicit_class_fqns",
        multiple=True,
        help="Explicit class FQN(s) to process. Repeat to bypass git-history candidate selection.",
    )
    @click.option("--target", "explicit_targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
    @click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
    @click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
    @click.option(
        "--classes-per-run",
        "--batch-size",
        "classes_per_run",
        default=settings.classes_per_agent_run,
        type=int,
        show_default=True,
        help="Number of classes to generate per OpenCode session (also: --batch-size; env UTA_CLASSES_PER_AGENT_RUN)",
    )
    @click.option("--fix-code", is_flag=True, help="Allow fixing production code bugs")
    @click.option(
        "--stop-after-stage",
        default=None,
        help="Stop the workflow immediately after the named stage. Supported checkpoints: plan_tests, generation.",
    )
    @click.option(
        "--resume",
        "resume",
        is_flag=True,
        help="Resume from a previous partial stop. Currently reuses latest_generation_plan.md and continues after planning.",
    )
    @click.option("--branch-name", default="unit-code-gen", show_default=True, help="Generation branch name.")
    @click.option(
        "--existing-branch",
        default=None,
        help="Reuse an existing local generation branch without resetting local changes. Implies --branch-name.",
    )
    @click.option(
        "--preserve-branch",
        is_flag=True,
        help="Do not recreate/reset the generation branch. Intended for chaining calibration runs in the same worktree.",
    )
    @click.option("--production", is_flag=True, help="Record this run in the production task DB")
    @click.option("--task-id", default=None, type=int, help="Run and update an existing repo task")
    @click.option("--resume-task", default=None, type=int, help="Resume an existing repo task and run it")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    @click.option(
        "--spec-context",
        "spec_context",
        default=None,
        help="Optional behavior context for generation prompts: a text file path or inline text (bounded to 16 KB).",
    )
    @click.option("--verbose", is_flag=True, help="Enable verbose logging")
    def run(repo, language, module, days, max_files, select_all_files, explicit_class_fqns, explicit_targets, coverage_gate, mutation_gate, classes_per_run, fix_code, stop_after_stage, resume, branch_name, existing_branch, preserve_branch, production, task_id, resume_task, task_db, spec_context, verbose):
        """Run the full test generation pipeline."""
        from uta.testgen.spec_context import resolve_spec_context
        from uta.language.java.batch import JavaBatchGenerationRequest
        from uta.testgen.runner import run_batch_generation

        resolved_spec_context = resolve_spec_context(spec_context)
        from uta.reporting import Reporter

        if classes_per_run < 1:
            raise click.BadParameter("--classes-per-run must be at least 1")

        task_manager = None
        effective_task_id = task_id or resume_task
        quality_mode = "class_batch"
        quality_gate_backend = "builtin"
        quality_gate_command = ""
        rdc_context = {}
        if production or effective_task_id:
            from uta.tasks.manager import TaskManager
            from uta.tasks.models import json_loads

            task_manager = TaskManager(task_db)
            if effective_task_id:
                task = task_manager.get_task(effective_task_id)
                if task is None:
                    raise click.ClickException(f"Task {effective_task_id} not found")
                # Run on the model the task was created with, not on whatever
                # the current settings resolve to. The daemon runs generation in
                # a fresh child process, so without this a task silently follows
                # configuration changes made after it was queued.
                cli_api._apply_task_opencode_selection(
                    json_loads(task.get("config_snapshot_json") or "{}")
                )
                from uta.tasks.generation_engine import require_durable_generation_task

                try:
                    require_durable_generation_task(task, action="run directly")
                except RuntimeError as exc:
                    raise click.ClickException(str(exc)) from exc
                selection = json_loads(task.get("selection_json"))
                rdc_context = json_loads(task.get("rdc_context_json") or "{}")
                repo = repo or task["repo_path"]
                module = module if module is not None else task.get("module_filter")
                if not explicit_class_fqns:
                    explicit_class_fqns = tuple(selection.get("class_fqns") or [])
                if not explicit_targets:
                    explicit_targets = tuple(target.get("target_id") for target in selection.get("targets") or [] if target.get("target_id"))
                language = str(selection.get("language") or language or "auto")
                select_all_files = select_all_files or bool(selection.get("all"))
                quality_gate_backend = str(selection.get("quality_gate_backend") or "builtin")
                quality_gate_command = str(selection.get("quality_gate_command") or "")
                quality_mode, coverage_gate, mutation_gate = cli_api._task_quality_options(
                    task,
                    selection,
                    coverage_gate,
                    mutation_gate,
                )
                existing_branch = existing_branch or task.get("branch_name")
                branch_name = task.get("branch_name") or branch_name
                preserve_branch = True
                if resume_task:
                    task_manager.resume_task(effective_task_id)
                task_manager.mark_running(effective_task_id, stage="startup", detail="uta run started")
            else:
                if not repo:
                    raise click.ClickException("--repo is required unless --task-id/--resume-task is provided")
                create_decision = cli_api._resolve_cli_language(repo, language, class_fqns=explicit_class_fqns, targets=explicit_targets)
                if create_decision.language == "python":
                    python_select_all = select_all_files
                    target_refs = cli_api._python_targets_for_run(
                        repo,
                        explicit_targets,
                        max_files=max_files,
                        days=days,
                        module=module,
                        select_all_files=select_all_files,
                    )
                    if not target_refs:
                        raise click.ClickException("No Python targets found. Use --target path.py or --all for repository selection.")
                    explicit_targets = tuple(target.target_id for target in target_refs)
                    effective_task_id = task_manager.create_task_targets(
                        repo_path=repo,
                        targets=target_refs,
                        module=module,
                        select_all=python_select_all,
                        priority=100,
                        branch_name=existing_branch or (branch_name if branch_name != "unit-code-gen" else None),
                        coverage_gate=coverage_gate,
                        mutation_gate=mutation_gate,
                        config_snapshot=cli_api._task_config_snapshot(),
                        budget_snapshot=cli_api._task_budget_snapshot(),
                        language="python",
                    )
                else:
                    if explicit_targets and not explicit_class_fqns:
                        explicit_class_fqns = tuple(explicit_targets)
                    effective_task_id = task_manager.create_task(
                        repo_path=repo,
                        module=module,
                        class_fqns=explicit_class_fqns,
                        select_all=select_all_files,
                        priority=100,
                        branch_name=existing_branch or (branch_name if branch_name != "unit-code-gen" else None),
                        coverage_gate=coverage_gate,
                        mutation_gate=mutation_gate,
                        config_snapshot=cli_api._task_config_snapshot(),
                        budget_snapshot=cli_api._task_budget_snapshot(),
                    )
                task_manager.mark_running(effective_task_id, stage="startup", detail="uta run started")
                task = task_manager.get_task(effective_task_id)
                existing_branch = existing_branch or task.get("branch_name")
                branch_name = task.get("branch_name") or branch_name
                preserve_branch = True

        if not repo:
            raise click.ClickException("--repo is required unless --task-id/--resume-task is provided")

        repo = os.path.abspath(repo)
        decision = cli_api._resolve_cli_language(repo, language, class_fqns=explicit_class_fqns, targets=explicit_targets)
        if decision.language == "python":
            return cli_api._run_python_batch_cli(
                repo=repo,
                explicit_targets=explicit_targets,
                max_files=max_files,
                days=days,
                module=module,
                select_all_files=select_all_files,
                coverage_gate=coverage_gate,
                mutation_gate=mutation_gate,
                quality_mode=quality_mode,
                task_manager=task_manager,
                effective_task_id=effective_task_id,
                task_db=task_db,
                verbose=verbose,
                spec_context=resolved_spec_context,
            )
        if explicit_targets and not explicit_class_fqns:
            explicit_class_fqns = tuple(explicit_targets)
        effective_branch_name = existing_branch or branch_name
        preserve_branch = preserve_branch or bool(existing_branch)
        run_log_path = cli_api._configure_run_logging(repo, verbose)
        run_started_at = time.time()
        console.print(f"[bold green]Starting UTA on {repo}[/bold green]")
        console.print(f"[dim]Run log: {run_log_path}[/dim]")

        # 1. Setup OpenCode
        _prepare_workspace(repo)
        auth_started = time.perf_counter()
        try:
            cli_api._ensure_model_auth(repo)
        except Exception as e:
            logger.exception("OpenCode auth check failed during run startup")
            console.print(f"[bold red]OpenCode auth error: {e}[/bold red]")
        auth_seconds = time.perf_counter() - auth_started

        console.print("[blue]Executing pipeline...[/blue]")
        final_error = None
        task_stopped = False
        try:
            java_result = run_batch_generation(
                JavaBatchGenerationRequest.from_class_fqns(
                    repo_path=Path(repo),
                    class_fqns=explicit_class_fqns,
                    module=module,
                    task_id=effective_task_id,
                    task_db_path=Path(task_manager.db_path) if task_manager else None,
                    coverage_gate=coverage_gate,
                    mutation_gate=mutation_gate,
                    days=days,
                    max_files=max_files,
                    select_all_files=select_all_files,
                    explicit_targets=list(explicit_targets),
                    classes_per_run=classes_per_run,
                    branch_name=effective_branch_name,
                    started_at=run_started_at,
                    stop_after_stage=stop_after_stage,
                    resume=resume,
                    preserve_branch=preserve_branch,
                    quality_mode=quality_mode,
                    quality_gate_backend=quality_gate_backend,
                    quality_gate_command=quality_gate_command,
                    rdc_context=rdc_context,
                    session_id=None,
                    session_ids=[],
                    run_log_path=run_log_path,
                    production=bool(production or effective_task_id),
                    language_decision=decision.as_dict(),
                    phase_timings={"auth_probe_seconds": auth_seconds},
                    spec_context=resolved_spec_context,
                )
            )
            final_state = java_result.final_state
            results = java_result.results
            final_error = java_result.final_error
            if final_error:
                console.print(f"[red]Pipeline error: {final_error}[/red]")
        except Exception as e:
            if e.__class__.__name__ == "TaskStopRequested":
                logger.warning("Pipeline stopped by production task control: %s", e)
                console.print(f"[yellow]Pipeline stopped: {e}[/yellow]")
                results = {}
                final_error = None
                task_stopped = True
                if task_manager and effective_task_id:
                    task_manager.mark_stopped(effective_task_id, reason=str(e), stage="stopped")
            elif e.__class__.__name__ == "TaskUnsafeDiffError":
                logger.exception("Pipeline stopped because an unsafe LLM-authored diff was detected")
                console.print(f"[bold red]Pipeline stopped by unsafe diff guard: {e}[/bold red]")
                results = {}
                final_error = str(e)
                if task_manager and effective_task_id:
                    task_manager.mark_failed(effective_task_id, str(e), stage="unsafe_diff")
            elif e.__class__.__name__ == "TaskBudgetExceeded":
                logger.warning("Pipeline stopped by production budget guard: %s", e)
                console.print(f"[bold red]Pipeline stopped by budget guard: {e}[/bold red]")
                results = {}
                final_error = str(e)
                if task_manager and effective_task_id:
                    task_manager.mark_budget_exceeded(effective_task_id, str(e))
            else:
                logger.exception("Pipeline failed during run execution")
                console.print(f"[bold red]Pipeline failed: {e}[/bold red]")
                results = {}
                final_error = str(e)
                if task_manager and effective_task_id:
                    task_manager.mark_failed(effective_task_id, final_error, stage="pipeline_failed")
        finally:
            pass

        if task_manager and effective_task_id and not task_stopped:
            elapsed = time.time() - run_started_at
            report_path = cli_api._latest_report_or_none(repo)
            task_manager.sync_results(
                effective_task_id,
                results,
                module=module,
                session_token_usage=final_state.get("session_token_usage", {}) if 'final_state' in locals() else {},
                phase_token_usage=final_state.get("phase_token_usage", {}) if 'final_state' in locals() else {},
                report_path=report_path,
                run_log_path=run_log_path,
                elapsed_seconds=elapsed,
                final_error=final_error,
            )
            live_paths = task_manager.write_live_status(effective_task_id)
            console.print(f"[dim]Task status: {live_paths['html']}[/dim]")

        # 3. Report (pipeline already stores report via store_and_push node,
        # but also display locally)
        reporter = Reporter(repo)
        reporter.display_summary(
            results,
            metadata={
                "repo_path": repo,
                "module": module,
                "branch_name": effective_branch_name,
                "total_candidates": len(final_state.get("candidates", [])) if 'final_state' in locals() else 0,
                "session_retrospect": final_state.get("session_retrospect", {}) if 'final_state' in locals() else {},
                "session_token_usage": final_state.get("session_token_usage", {}) if 'final_state' in locals() else {},
                "phase_token_usage": final_state.get("phase_token_usage", {}) if 'final_state' in locals() else {},
                "run_log_path": run_log_path,
                "task_id": effective_task_id,
                "task_db_path": str(task_manager.db_path) if task_manager else None,
                "phase_timings": final_state.get("phase_timings", {}) if 'final_state' in locals() else {"auth_probe_seconds": auth_seconds},
                "total_elapsed_seconds": time.time() - run_started_at,
            },
        )



    return {"run": run}

__all__ = ["register_generation_commands"]
