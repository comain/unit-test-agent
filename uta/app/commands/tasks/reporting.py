"""The `uta tasks report` subgroup.

`batch` summarises every task in a database; `repo` summarises one repository
slug, target-first for Python and class-first for Java. Both produce an
operator-facing artefact rather than a view of live state, and both write a
JSON file next to the report, which is why they own their own subgroup rather
than sitting among the inspection commands.
"""

from __future__ import annotations

from pathlib import Path

import click


def register_reporting_commands(tasks_group, *, console) -> None:
    """Attach the `report` subgroup to the tasks group."""

    @tasks_group.group("report")
    def report_group():
        """Generate task reports."""

    @report_group.command("batch")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    @click.option("--json-output", is_flag=True, help="Output JSON only")
    def report_batch(task_db, json_output):
        """Print a batch summary of all tasks in the DB."""
        import json as _json
        from datetime import datetime as _dt
        from uta.tasks.db import TaskDB

        db = TaskDB(task_db)
        db.init()
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n,
                       COALESCE(SUM(provider_cost_usd), 0.0) AS cost,
                       COALESCE(AVG(NULLIF(coverage_avg, 0)), 0.0) AS cov_avg,
                       COALESCE(AVG(NULLIF(mutation_avg, 0)), 0.0) AS mut_avg,
                       COALESCE(SUM(elapsed_seconds), 0.0) AS total_elapsed
                FROM repo_tasks
                GROUP BY status ORDER BY n DESC
                """
            ).fetchall()
            total_cost = conn.execute("SELECT COALESCE(SUM(provider_cost_usd), 0.0) AS c FROM repo_tasks").fetchone()["c"]
            total_tasks = conn.execute("SELECT COUNT(*) AS n FROM repo_tasks").fetchone()["n"]

        summary = {
            "generated_at": _dt.utcnow().isoformat() + "Z",
            "total_tasks": total_tasks,
            "total_cost_usd": round(float(total_cost or 0), 4),
            "by_status": [
                {
                    "status": r["status"],
                    "count": r["n"],
                    "cost_usd": round(float(r["cost"] or 0), 4),
                    "coverage_avg": round(float(r["cov_avg"] or 0), 2),
                    "mutation_avg": round(float(r["mut_avg"] or 0), 2),
                    "elapsed_seconds": round(float(r["total_elapsed"] or 0), 1),
                }
                for r in rows
            ],
        }

        out_dir = Path(".uta_reports")
        out_dir.mkdir(exist_ok=True)
        ts = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"report_batch_{ts}.json"
        out_path.write_text(_json.dumps(summary, indent=2))

        if json_output:
            console.print(_json.dumps(summary, indent=2))
            return

        from rich.table import Table

        table = Table(title=f"Batch Report — {total_tasks} tasks  |  ${summary['total_cost_usd']:.4f} total cost")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        table.add_column("Cost USD", justify="right")
        table.add_column("Cov%", justify="right")
        table.add_column("Mut%", justify="right")
        for r in summary["by_status"]:
            table.add_row(r["status"], str(r["count"]), f"${r['cost_usd']:.4f}", f"{r['coverage_avg']:.1f}", f"{r['mutation_avg']:.1f}")
        console.print(table)
        console.print(f"[dim]JSON written to {out_path}[/dim]")

    @report_group.command("repo")
    @click.argument("slug")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    @click.option("--json-output", is_flag=True, help="Output JSON only")
    def report_repo(slug, task_db, json_output):
        """Print a per-class breakdown for a repo (matched by slug or path fragment)."""
        import json as _json
        from datetime import datetime as _dt
        from uta.tasks.db import TaskDB

        db = TaskDB(task_db)
        db.init()
        with db.connect() as conn:
            task = conn.execute(
                "SELECT * FROM repo_tasks WHERE repo_slug=? OR repo_path LIKE ? ORDER BY id DESC LIMIT 1",
                (slug, f"%{slug}%"),
            ).fetchone()
        if not task:
            raise click.ClickException(f"No task found matching slug/path: {slug}")
        task_id = task["id"]
        with db.connect() as conn:
            targets = conn.execute(
                """
                SELECT language, target_id, display_name, source_path, target_granularity,
                       class_fqn, status, coverage_line, mutation_score, provider_cost_usd, elapsed_seconds
                FROM class_tasks
                WHERE repo_task_id=?
                ORDER BY COALESCE(display_name, target_id, class_fqn)
                """,
                (task_id,),
            ).fetchall()

        def _target_payload(row):
            language = row["language"] or task["language"] or "java"
            target_id = row["target_id"] or row["class_fqn"]
            display_name = row["display_name"] or target_id
            return {
                "language": language,
                "target_id": target_id,
                "display_name": display_name,
                "source_path": row["source_path"],
                "target_granularity": row["target_granularity"] or ("class" if language == "java" else "file"),
                "fqn": row["class_fqn"],
                "class_fqn": row["class_fqn"],
                "status": row["status"],
                "coverage": round(float(row["coverage_line"] or 0), 2),
                "mutation": round(float(row["mutation_score"] or 0), 2),
                "cost_usd": round(float(row["provider_cost_usd"] or 0), 6),
                "elapsed_s": round(float(row["elapsed_seconds"] or 0), 1),
            }

        target_items = [_target_payload(row) for row in targets]

        summary = {
            "generated_at": _dt.utcnow().isoformat() + "Z",
            "task_id": task_id,
            "slug": task["repo_slug"],
            "language": task["language"] or "java",
            "status": task["status"],
            "total_cost_usd": round(float(task["provider_cost_usd"] or 0), 4),
            "targets": target_items,
            "classes": target_items,
        }

        out_dir = Path(".uta_reports")
        out_dir.mkdir(exist_ok=True)
        ts = _dt.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"report_{slug}_{ts}.json"
        out_path.write_text(_json.dumps(summary, indent=2))

        if json_output:
            console.print(_json.dumps(summary, indent=2))
            return

        from rich.table import Table

        target_label = "Class FQN" if summary["language"] == "java" else "Target"
        table = Table(title=f"{task['repo_slug']} — {task['status']}  |  ${summary['total_cost_usd']:.4f}")
        table.add_column(target_label, no_wrap=False)
        table.add_column("Status")
        table.add_column("Cov%", justify="right")
        table.add_column("Mut%", justify="right")
        table.add_column("Cost", justify="right")
        for c in summary["targets"]:
            table.add_row(c["display_name"], c["status"], f"{c['coverage']:.1f}", f"{c['mutation']:.1f}", f"${c['cost_usd']:.5f}")
        console.print(table)
        console.print(f"[dim]JSON written to {out_path}[/dim]")
