import logging
import datetime
import time
import tempfile
import json
import subprocess
import sys
from typing import Optional, Dict, Any, List, Iterable
from pathlib import Path

import click
from rich.console import Console

from uta.shared.config import settings
from uta.shared.opencode_snapshot import opencode_config_snapshot
from uta.app.harness_startup import (
    ensure_model_auth,
    prepare_workspace as _prepare_workspace,
    probe_harness_readiness,
    provider_from_model as _provider_from_model,
)
from uta.app.source_discovery import load_index_payload as _load_index_payload
from uta.app.task_daemon import (
    _cleanup_daemon_pool,
    _daemon_child_popen_kwargs,
    _ensure_daemon_java_home,
    _task_ids_from_process_listing,
)

#: `highlight=False` because rich's automatic highlighter emboldens anything
#: that looks like a number or a path, which turns "Created task 1" into
#: "Created task \x1b[1m1\x1b[0m" -- styling the CLI never asked for.
#:
#: `soft_wrap=True` because the default wraps at the console width, and a task
#: database path broken across two lines cannot be copied out of a terminal.
#: That is a usability bug the tests happened to catch.
console = Console(highlight=False, soft_wrap=True)

#: Clone and fetch here are interactive, so generous: the point is that a
#: stalled link eventually says so rather than hanging silently forever.
_CLI_GIT_TIMEOUT = 1800
logger = logging.getLogger("uta")



def _latest_summary_report(repo: str) -> Path:
    report_dir = Path(repo) / ".uta_reports"
    candidates = sorted(report_dir.glob("summary_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise click.ClickException(f"No summary report found under {report_dir}")
    return candidates[0]


def _load_report(report_path: Path) -> Dict[str, Any]:
    return json.loads(report_path.read_text(encoding="utf-8"))


def _pick_report_result(report: Dict[str, Any], class_fqn: Optional[str]) -> tuple[str, Dict[str, Any]]:
    results = report.get("results") or {}
    if not results:
        raise click.ClickException("Report contains no results to resume")
    if class_fqn:
        if class_fqn not in results:
            raise click.ClickException(f"Class {class_fqn} not found in report")
        return class_fqn, results[class_fqn]
    if len(results) == 1:
        only = next(iter(results.items()))
        return only[0], only[1]
    raise click.ClickException("Report contains multiple classes; provide --class-fqn")


def _dedupe_session_ids(session_ids: List[str]) -> List[str]:
    out: List[str] = []
    for session_id in session_ids:
        if session_id and session_id not in out:
            out.append(session_id)
    return out


def _task_quality_options(
    task: Dict[str, Any],
    selection: Dict[str, Any],
    coverage_gate: Any,
    mutation_gate: Any,
) -> tuple[str, int, int]:
    quality_mode = str(selection.get("quality_mode") or "class_batch")
    if task.get("coverage_gate") is not None:
        coverage_gate = task["coverage_gate"]
    if task.get("mutation_gate") is not None:
        mutation_gate = task["mutation_gate"]
    return quality_mode, int(float(coverage_gate)), int(float(mutation_gate))


def _merge_timing_details(original: Dict[str, Any], resumed: Dict[str, float]) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    for key, value in (original or {}).items():
        merged[key] = float(value or 0.0)
    for key, value in resumed.items():
        merged[key] = merged.get(key, 0.0) + float(value or 0.0)
    return merged


def _report_primary_model(report: Dict[str, Any]) -> Optional[str]:
    by_model = ((report.get("token_usage") or {}).get("by_model") or {})
    if len(by_model) == 1:
        return next(iter(by_model.keys()))
    if by_model:
        return max(
            by_model.items(),
            key=lambda item: int((item[1] or {}).get("total", 0) or 0),
        )[0]
    return None


def _configure_run_logging(repo: str, verbose: bool) -> str:
    log_level = logging.DEBUG if verbose else logging.INFO
    log_dir = Path(tempfile.gettempdir()) / "uta-run-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    repo_slug = Path(repo).name or "repo"
    run_log_path = log_dir / f"{repo_slug}_run_{timestamp}.log"

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(run_log_path)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Keep UTA/OpenCode logs visible while suppressing low-signal transport chatter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return str(run_log_path)


def _fmt_optional_float(value: Optional[float], *, decimals: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{decimals}f}"


def _fmt_int(value: Any) -> str:
    return f"{int(value or 0):,}"


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "-"
    seconds = int(round(float(value)))
    return str(datetime.timedelta(seconds=seconds))


# Startup readiness lives in `harness_startup`, and is reached through these
# two names because they are the seam tests replace: patching
# `cli._probe_harness_readiness` must still change what `_ensure_model_auth`
# calls, which is why the probe is passed in rather than looked up there.
def _probe_harness_readiness(repo: str) -> "object":
    return probe_harness_readiness(repo)


def _ensure_model_auth(repo: str) -> None:
    ensure_model_auth(repo, probe=_probe_harness_readiness)


@click.group()
def main():
    """Unit Test Agent (uta) - Generate unit tests for legacy Java code."""
    # Composition, at the one place every command passes through. Testgen asks
    # the registry for task ports and gets None until this runs, so registering
    # late would not raise -- it would quietly skip a safety guard, which is
    # exactly the failure mode worth avoiding.
    from uta.app.persistence import register_task_persistence
    from uta.composition.language_backends import register_language_backends
    from uta.app.agent_core_pin import verify_agent_core_pin

    # Before composition, so an unsupported UTA/agent-core pair fails here
    # rather than inside a generation cycle that has already spent budget.
    verify_agent_core_pin()
    register_language_backends()
    register_task_persistence()


def _task_config_snapshot() -> Dict[str, Any]:
    """What a task will run on, frozen at creation.

    The OpenCode fields are not decoration: `_apply_task_opencode_selection`
    reads them back in manual mode, so it keeps the model it was created
    with instead of following whatever `settings.opencode_model` has become.
    They come from `uta.shared.opencode_snapshot` because the CI fix-session
    handlers need the same fields and cannot import `uta.app`.
    """
    return {
        **opencode_config_snapshot(),
        "coverage_gate": settings.coverage_gate,
        "mutation_gate": settings.mutation_gate,
        "classes_per_agent_run": settings.classes_per_agent_run,
        "planning_timeout_seconds": getattr(settings, "planning_timeout_seconds", None),
        "generation_timeout_seconds": getattr(settings, "generation_timeout_seconds", None),
    }


def _provider_from_model(model: str) -> str:
    return str(model or "").split("/", 1)[0]


def _apply_task_opencode_selection(config_snapshot: Dict[str, Any]) -> None:
    """Pin manual runs only; discovery uses current host config at execution."""
    if settings.model_selection_config:
        return

    selected_model = config_snapshot.get("opencode_selected_model") or ""
    selected_provider = (
        config_snapshot.get("opencode_selected_provider") or _provider_from_model(selected_model)
    )
    if not selected_model:
        return
    settings.opencode_model = selected_model
    settings.opencode_small_model = selected_model
    if selected_provider:
        settings.opencode_provider = selected_provider

def _task_budget_snapshot() -> Dict[str, Any]:
    return {
        "max_phase_input_tokens": getattr(settings, "max_phase_input_tokens", None),
        "max_phase_output_tokens": getattr(settings, "max_phase_output_tokens", None),
        "max_session_input_tokens": getattr(settings, "max_session_input_tokens", None),
        "timeout_multiplier": getattr(settings, "timeout_multiplier", None),
    }


def _latest_report_or_none(repo: str) -> Optional[str]:
    try:
        return str(_latest_summary_report(repo))
    except click.ClickException:
        return None


def _language_registry():
    from uta.shared.languages import default_registry

    return default_registry()


def _resolve_cli_language(repo: str, language: str = "auto", *, class_fqns: Iterable[str] = (), targets: Iterable[str] = ()):
    from uta.shared.languages import AmbiguousLanguageError, UnsupportedLanguageError, resolve_language

    try:
        return resolve_language(
            _language_registry(),
            Path(repo),
            explicit_language=language,
            class_fqns=list(class_fqns or ()),
            targets=list(targets or ()),
        )
    except (AmbiguousLanguageError, UnsupportedLanguageError) as exc:
        raise click.ClickException(str(exc)) from exc


def _normalize_cli_targets(language: str, values: Iterable[Any]):
    from uta.shared.languages import RawTargetSelection, UnsupportedLanguageError

    registry = _language_registry()
    try:
        adapter = registry.adapter_for(language)
    except UnsupportedLanguageError as exc:
        raise click.ClickException(str(exc)) from exc
    targets = []
    for value in values or ():
        try:
            targets.append(adapter.normalize_target(RawTargetSelection.from_value(value)))
        except (TypeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
    return targets


def _manifest_targets(item: Dict[str, Any]) -> List[Any]:
    targets = item.get("targets") or []
    if isinstance(targets, (str, dict)):
        targets = [targets]
    out = list(targets)
    if item.get("target"):
        out.append(item["target"])
    return out


def _python_query_index_payload(repo: str, target_ref, decision) -> Dict[str, Any]:
    from uta.testgen.context import make_context_provider

    payload = make_context_provider("python", Path(repo)).query_target(target_ref)
    payload["languageDecision"] = decision.as_dict()
    payload["class"] = None
    return payload


def _python_targets_for_run(
    repo: str,
    explicit_targets: Iterable[Any],
    *,
    max_files: int,
    days: int,
    module: Optional[str],
    select_all_files: bool,
):
    targets = _normalize_cli_targets("python", explicit_targets)
    if targets:
        return targets
    from uta.testgen.source_selection import filter_files, get_all_source_files, get_changed_source_files

    if select_all_files:
        files = [path for path, _count in get_all_source_files("python", repo, module)]
    else:
        files = filter_files(get_changed_source_files("python", repo, days, module), max_files=max_files)
    return _normalize_cli_targets("python", files)


def _run_python_batch_cli(
    *,
    repo: str,
    explicit_targets: Iterable[Any],
    max_files: int,
    days: int,
    module: Optional[str],
    select_all_files: bool,
    coverage_gate: float,
    mutation_gate: float,
    quality_mode: str = "class_batch",
    task_manager: Any = None,
    effective_task_id: Optional[int] = None,
    task_db: Optional[str] = None,
    verbose: bool = False,
    spec_context: str = "",
) -> None:
    from uta.testgen.batch import BatchGenerationRequest
    from uta.reporting import Reporter
    from uta.testgen.runner import run_batch_generation

    target_refs = _python_targets_for_run(
        repo,
        explicit_targets,
        max_files=max_files,
        days=days,
        module=module,
        select_all_files=select_all_files,
    )
    if not target_refs:
        raise click.ClickException("No Python targets found. Use --target path.py or --all for repository selection.")
    run_log_path = _configure_run_logging(repo, verbose)
    run_started_at = time.time()
    console.print(f"[bold green]Starting Python UTA on {repo}[/bold green]")
    console.print(f"[dim]Run log: {run_log_path}[/dim]")
    _prepare_workspace(repo)
    try:
        request = BatchGenerationRequest(
            language="python",
            repo_path=Path(repo),
            targets=target_refs,
            task_id=effective_task_id,
            task_db_path=Path(task_manager.db_path) if task_manager else (Path(task_db) if task_db and effective_task_id else None),
            model_id=settings.opencode_model,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            quality_mode=quality_mode,
            spec_context=spec_context,
        )
        result = run_batch_generation(request)
    except Exception as exc:
        if task_manager and effective_task_id:
            if exc.__class__.__name__ == "TaskUnsafeDiffError":
                task_manager.mark_failed(effective_task_id, str(exc), stage="unsafe_diff")
            elif exc.__class__.__name__ == "TaskBudgetExceeded":
                task_manager.mark_budget_exceeded(effective_task_id, str(exc))
            elif exc.__class__.__name__ == "TaskStopRequested":
                task_manager.mark_stopped(effective_task_id, reason=str(exc), stage="stopped")
        raise click.ClickException(str(exc)) from exc
    metadata = {
        "repo_path": repo,
        "language": "python",
        "total_candidates": len(target_refs),
        "targets_by_id": {target.target_id: target.as_selection() for target in target_refs},
        "session_retrospect": result.session_retrospect,
        "session_token_usage": result.session_token_usage,
        "run_log_path": run_log_path,
        "task_id": effective_task_id,
        "task_db_path": str(task_manager.db_path) if task_manager else task_db,
        "total_elapsed_seconds": time.time() - run_started_at,
    }
    reporter = Reporter(repo)
    report_name = f"summary_python_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    reporter.save_report(result.results, report_name, metadata=metadata)
    reporter.display_summary(result.results, metadata=metadata)
    if task_manager and effective_task_id:
        live_paths = task_manager.write_live_status(effective_task_id)
        console.print(f"[dim]Task status: {live_paths['html']}[/dim]")



# Register command groups only after the root command and shared helpers are
# defined. ``uta.app.cli.main`` remains the stable public entrypoint.
from uta.app.assessment_commands import register_assessment_commands as _register_assessment_commands  # noqa: E402
from uta.app.enforcement_commands import register_enforcement_commands as _register_enforcement_commands  # noqa: E402
from uta.app.generation_commands import register_generation_commands as _register_generation_commands  # noqa: E402
from uta.app.index_commands import register_index_commands as _register_index_commands  # noqa: E402
from uta.app.task_commands import register_task_commands as _register_task_commands  # noqa: E402

_register_assessment_commands(main, console=console)
_register_enforcement_commands(
    main,
    console=console,
    normalize_cli_targets=_normalize_cli_targets,
    resolve_cli_language=_resolve_cli_language,
)
_generation_commands = _register_generation_commands(
    main,
    cli_api=sys.modules[__name__],
    console=console,
    logger=logger,
)
# Keep the historical import surface for tests and integrations that inspect
# or invoke the Click command objects directly.
run = _generation_commands["run"]
_register_index_commands(
    main,
    console=console,
    load_index_payload=_load_index_payload,
    normalize_cli_targets=_normalize_cli_targets,
    python_query_index_payload=_python_query_index_payload,
    resolve_cli_language=_resolve_cli_language,
)
_register_task_commands(
    main,
    console=console,
    cli_git_timeout=_CLI_GIT_TIMEOUT,
    fmt_int=_fmt_int,
    fmt_optional_float=_fmt_optional_float,
    fmt_seconds=_fmt_seconds,
    manifest_targets=_manifest_targets,
    normalize_cli_targets=_normalize_cli_targets,
    resolve_cli_language=_resolve_cli_language,
    task_budget_snapshot=_task_budget_snapshot,
    task_config_snapshot=_task_config_snapshot,
)


if __name__ == "__main__":
    main()

# Re-exported on purpose: these names are imported from this module
# elsewhere in the tree. Declaring them makes that a contract rather than
# an accident, and lets the linter tell a re-export from a dead import.
__all__ = [
    "_cleanup_daemon_pool",
    "_daemon_child_popen_kwargs",
    "_ensure_daemon_java_home",
    "_load_index_payload",
    "_prepare_workspace",
    "_provider_from_model",
    "_task_ids_from_process_listing",
    # Java source discovery moved to `uta.app.source_discovery`, but tests
    # still reach the process boundary as `uta.app.cli.subprocess.run`. The
    # import stays so that path resolves.
    "subprocess",
]
