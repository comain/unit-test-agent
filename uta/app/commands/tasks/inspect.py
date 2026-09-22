"""Commands that read task state and change nothing.

Everything here is safe to run against a live system: listings, a single
task's detail, the refreshing watch and dashboard views, the JSON/CSV export
and the pre-deploy generation-cutover audit. They are grouped by that
property, because it is the property an operator cares about at 3am.

`audit-generation-cutover` is here rather than with the control commands for
the same reason: it exits non-zero to report blockers, but it writes nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.live import Live
from rich.table import Table

from uta.shared.config import settings


def register_inspect_commands(
    tasks_group,
    *,
    console,
    fmt_int,
    fmt_optional_float,
    fmt_seconds,
) -> None:
    """Attach the read-only task commands to the tasks group."""
    _fmt_int = fmt_int
    _fmt_optional_float = fmt_optional_float
    _fmt_seconds = fmt_seconds

    @tasks_group.command("audit-generation-cutover")
    @click.option("--task-db", required=True, type=click.Path(exists=True, dir_okay=False))
    def audit_generation_cutover_command(task_db):
        """Read-only pre-deploy audit for durable generation snapshots."""
        from uta.tasks.generation_engine import audit_generation_cutover

        payload = audit_generation_cutover(task_db)
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if payload["blocker_count"]:
            raise click.exceptions.Exit(2)

    @tasks_group.command("list")
    @click.option("--status", default=None, help="Filter by repo task status")
    @click.option("--repo", default=None, type=click.Path(), help="Filter by repo path")
    @click.option("--limit", default=50, show_default=True, type=int)
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_list(status, repo, limit, task_db):
        from uta.tasks.manager import TaskManager

        manager = TaskManager(task_db)
        repo_path = str(Path(repo).expanduser().resolve()) if repo else None
        rows = manager.list_tasks(status=status, repo_path=repo_path, limit=limit)
        table = Table(title=f"UTA Tasks ({manager.db_path})")
        table.add_column("ID", justify="right")
        table.add_column("Priority", justify="right")
        table.add_column("Status")
        table.add_column("Stage")
        table.add_column("Repo")
        table.add_column("Branch")
        table.add_column("Est Cost", justify="right")
        table.add_column("Tokens", justify="right")
        for row in rows:
            table.add_row(
                str(row["id"]),
                str(row["priority"]),
                row["status"],
                row.get("current_stage") or "",
                row["repo_slug"],
                row.get("branch_name") or "",
                "" if row.get("estimated_cost") is None else f"{float(row['estimated_cost']):.4f}",
                f"{int(row.get('actual_input_tokens') or 0)}/{int(row.get('actual_output_tokens') or 0)}",
            )
        console.print(table)

    @tasks_group.command("show")
    @click.argument("task_id", type=int)
    @click.option("--sessions", "--show-sessions", is_flag=True, help="Show OpenCode session IDs")
    @click.option("--detail", is_flag=True, help="Show all class rows instead of compact output")
    @click.option("--show-classes", is_flag=True, help="Compatibility alias for --detail")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_show(task_id, sessions, detail, show_classes, task_db):
        from uta.tasks.manager import TaskManager
        from uta.tasks.render import render_task_table

        manager = TaskManager(task_db)
        payload = manager.status_payload(task_id)
        render_task_table(console, payload, show_sessions=sessions, detail=detail or show_classes)

    @tasks_group.command("watch")
    @click.argument("task_id", type=int)
    @click.option("--interval", default=5.0, show_default=True, type=float, help="Refresh interval in seconds")
    @click.option("--once", is_flag=True, help="Render once and exit")
    @click.option("--sessions", "--show-sessions", is_flag=True, help="Show OpenCode session IDs")
    @click.option("--detail", is_flag=True, help="Show all class rows instead of compact output")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_watch(task_id, interval, once, sessions, detail, task_db):
        from uta.tasks.manager import TaskManager
        from uta.tasks.render import build_task_renderables

        manager = TaskManager(task_db)

        def _missing_task_error() -> click.ClickException:
            available = [str(row["id"]) for row in manager.list_tasks(limit=20)]
            suffix = f" Available repo task ids: {', '.join(available)}." if available else " No repo tasks exist in this DB."
            return click.ClickException(f"Repo task {task_id} not found in {manager.db_path}.{suffix}")

        def make_renderable():
            try:
                payload = manager.status_payload(task_id)
            except KeyError as exc:
                raise _missing_task_error() from exc
            return build_task_renderables(payload, show_sessions=sessions, detail=detail)

        if once:
            console.print(make_renderable())
            return
        with Live(make_renderable(), console=console, refresh_per_second=1 / max(interval, 0.1), screen=True) as live:
            try:
                while True:
                    time.sleep(interval)
                    live.update(make_renderable())
            except KeyboardInterrupt:
                pass

    @tasks_group.command("summary")
    @click.argument("task_id", type=int)
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    @click.option("--json", "as_json", is_flag=True, help="Emit the task summary as JSON")
    def task_summary(task_id, task_db, as_json):
        from uta.tasks.manager import TaskManager

        manager = TaskManager(task_db)
        summary = manager.build_task_summary(task_id, recalc_project_coverage=True)
        if as_json:
            click.echo(json.dumps(summary, indent=2, sort_keys=True))
            return

        task = summary["task"]
        classes = summary["classes"]
        coverage = summary["coverage"]
        mutation = summary["mutation"]
        timing = summary["timing"]
        tokens = summary["tokens"]

        overview = Table(title=f"Task {task_id} Summary")
        overview.add_column("Metric", style="cyan")
        overview.add_column("Value")
        overview.add_row("Repo", str(task.get("repo_path") or ""))
        overview.add_row("Status", str(task.get("status") or ""))
        overview.add_row("Stage", str(task.get("current_stage") or ""))
        overview.add_row("Branch", str(task.get("branch_name") or ""))
        overview.add_row(
            "Classes",
            f"generated={classes['generated']} total={classes['total']} completed={classes['completed']} passed={classes['passed']} failed={classes['failed']}",
        )
        overview.add_row("Started", str(timing.get("started_at") or "-"))
        overview.add_row("Finished", str(timing.get("finished_at") or "-"))
        overview.add_row("Elapsed", _fmt_seconds(timing.get("elapsed_seconds")))
        overview.add_row("Coverage", f"count={coverage['count']} total={_fmt_optional_float(coverage['total'])}% avg={_fmt_optional_float(coverage['avg'])}% max={_fmt_optional_float(coverage['max'])}% min={_fmt_optional_float(coverage['min'])}%")
        overview.add_row("Mutation", f"count={mutation['count']} total={_fmt_optional_float(mutation['total'])}% avg={_fmt_optional_float(mutation['avg'])}% max={_fmt_optional_float(mutation['max'])}% min={_fmt_optional_float(mutation['min'])}%")
        console.print(overview)

        if summary.get("project_coverage_recalc", {}).get("ran"):
            recalc = summary["project_coverage_recalc"]
            recalc_table = Table(title="Project Coverage Recalculation")
            recalc_table.add_column("Metric", style="cyan")
            recalc_table.add_column("Value")
            recalc_table.add_row("Matched Classes", _fmt_int(recalc.get("matched_classes")))
            recalc_table.add_row("Covered Lines", "-" if recalc.get("covered_lines") is None else _fmt_int(recalc.get("covered_lines")))
            recalc_table.add_row("Missed Lines", "-" if recalc.get("missed_lines") is None else _fmt_int(recalc.get("missed_lines")))
            recalc_table.add_row("Project Line Coverage", f"{_fmt_optional_float(coverage['total'])}%")
            if int(recalc.get("matched_classes") or 0) == 0:
                recalc_table.add_row("Note", "JaCoCo rerun did not match any target classes; total coverage fell back to stored class metrics")
            console.print(recalc_table)

        token_table = Table(title="Recorded Token And Cost Accounting")
        token_table.add_column("Source", style="cyan")
        token_table.add_column("Input", justify="right")
        token_table.add_column("Cache Read", justify="right")
        token_table.add_column("Output", justify="right")
        token_table.add_column("Reasoning", justify="right")
        token_table.add_column("Total", justify="right")
        token_table.add_column("Cost USD", justify="right")
        row = tokens["recorded"]
        token_table.add_row(
            "Task ledger",
            _fmt_int(row.get("input")),
            _fmt_int(row.get("cache_read")),
            _fmt_int(row.get("output")),
            _fmt_int(row.get("reasoning")),
            _fmt_int(row.get("total")),
            _fmt_optional_float(row.get("cost_usd"), decimals=4),
        )
        console.print(token_table)

        verification = Table(title="Accounting Coverage")
        verification.add_column("Metric", style="cyan")
        verification.add_column("Value", justify="right")
        verification.add_row("Cost provenance", str(row.get("cost_provenance") or "unavailable"))
        verification.add_row("Classes with usage", _fmt_int(tokens["coverage"]["classes_with_usage"]))
        verification.add_row("Classes without usage", _fmt_int(tokens["coverage"]["classes_without_usage"]))
        console.print(verification)

    @tasks_group.command("export")
    @click.argument("task_id", type=int)
    @click.option("--format", "fmt", type=click.Choice(["json", "html"]), default="json", show_default=True)
    @click.option("--output", default=None, type=click.Path(dir_okay=False), help="Output file. Defaults to stdout.")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_export(task_id, fmt, output, task_db):
        from uta.tasks.manager import TaskManager
        from uta.tasks.render import html_for_payload

        manager = TaskManager(task_db)
        payload = manager.status_payload(task_id)
        text = html_for_payload(payload) if fmt == "html" else json.dumps(payload, indent=2, sort_keys=True)
        if output:
            Path(output).write_text(text, encoding="utf-8")
            console.print(f"[green]Wrote {output}[/green]")
        else:
            click.echo(text)

    @tasks_group.command("dashboard")
    @click.option("--interval", default=2.0, show_default=True, type=float, help="Refresh interval in seconds")
    @click.option("--once", is_flag=True, help="Render once and exit")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_dashboard(interval, once, task_db):
        """Live batch dashboard: active workers, queue depth, cost, throughput."""
        from uta.tasks.db import TaskDB
        from rich.panel import Panel

        db = TaskDB(task_db)
        db.init()

        def _make_dashboard():
            with db.connect() as conn:
                rows = conn.execute(
                    "SELECT id, repo_path, status FROM repo_tasks ORDER BY updated_at DESC"
                ).fetchall()
                status_counts: dict = {}
                for row in rows:
                    status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

                running_rows = [r for r in rows if r["status"] == "RUNNING"]
                total_cost = db.total_provider_cost_usd()

                # Per-running-task stage info from most recent event
                worker_lines = []
                for rrow in running_rows:
                    tid = rrow["id"]
                    repo = rrow["repo_path"]
                    slug = Path(repo).name if repo else str(tid)
                    evt = conn.execute(
                        "SELECT event_type, message FROM task_events WHERE repo_task_id=? ORDER BY id DESC LIMIT 1",
                        (tid,),
                    ).fetchone()
                    stage = evt["event_type"] if evt else "?"
                    task_cost = conn.execute(
                        "SELECT COALESCE(SUM(provider_cost_usd), 0) FROM class_tasks WHERE repo_task_id=?",
                        (tid,),
                    ).fetchone()[0] or 0.0
                    worker_lines.append(f"  [bold cyan]#{tid}[/] {slug[:30]:<30} {stage:<20} ${task_cost:.4f}")

                cap = settings.batch_cap_usd
                cap_str = f" / cap ${cap:.2f}" if cap else ""
                header = (
                    f"[bold]Batch — [cyan]{status_counts.get('RUNNING', 0)} running[/cyan]"
                    f" / [yellow]{status_counts.get('QUEUED', 0) + status_counts.get('CREATED', 0)} queued[/yellow]"
                    f" / [green]{status_counts.get('COMPLETED', 0)} done[/green]"
                    f" / [red]{status_counts.get('FAILED', 0)} failed[/red]"
                    f" / [magenta]{status_counts.get('POISONED', 0)} poisoned[/magenta][/bold]"
                )
                cost_line = f"Total spend: [bold green]${total_cost:.4f}[/bold green]{cap_str}"

                lines = [header, cost_line, ""]
                if worker_lines:
                    lines.append("[bold]Active workers:[/bold]")
                    lines.extend(worker_lines)
                else:
                    lines.append("[dim]No active workers[/dim]")

                return Panel("\n".join(lines), title="UTA Batch Dashboard", border_style="blue")

        if once:
            console.print(_make_dashboard())
            return
        with Live(_make_dashboard(), console=console, refresh_per_second=1 / max(interval, 0.1), screen=True) as live:
            try:
                while True:
                    time.sleep(interval)
                    live.update(_make_dashboard())
            except KeyboardInterrupt:
                pass
