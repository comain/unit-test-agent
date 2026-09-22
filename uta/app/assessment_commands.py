"""Neutral session-diagnostics CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

from uta.app.session_assessment import assess_sessions, compare_sessions
from uta.shared.config import settings


def register_assessment_commands(main, *, console) -> None:
    """Attach the bounded, provider-neutral assessment command to UTA."""

    @main.command("assess")
    @click.option(
        "--session-id",
        "session_ids",
        required=True,
        multiple=True,
        help="Durable agent session locator. Repeat to aggregate sessions.",
    )
    @click.option(
        "--baseline-session-id",
        "baseline_session_ids",
        multiple=True,
        help="Optional baseline durable locator. Repeat to aggregate sessions.",
    )
    @click.option(
        "--harness",
        "harness_name",
        default=lambda: settings.agent_harness,
        show_default="configured harness",
        help="Neutral harness name that owns the locators.",
    )
    @click.option(
        "--db-path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Compatibility override for the configured harness diagnostics store.",
    )
    @click.option(
        "--json-output",
        is_flag=True,
        help="Print machine-readable JSON instead of rich tables.",
    )
    def assess(session_ids, baseline_session_ids, harness_name, db_path, json_output):
        """Inspect bounded usage, timing, tools, patches, and diagnostic signals."""
        current = assess_sessions(
            session_ids, db_path, harness_name=harness_name
        )
        baseline = (
            assess_sessions(
                baseline_session_ids, db_path, harness_name=harness_name
            )
            if baseline_session_ids
            else None
        )

        if json_output:
            payload = {"current": _json_projection(current)}
            if baseline is not None:
                payload["baseline"] = _json_projection(baseline)
                payload["comparison"] = compare_sessions(current, baseline)
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return

        _print_assessment(console, current, title="UTA Session Assessment")
        if baseline is not None:
            _print_comparison(console, current, baseline)


def _json_projection(value):
    return {
        "session_id": value.session_id,
        "session_ids": value.session_ids,
        "session_refs": value.session_refs,
        "status": value.status,
        "reason_codes": value.reason_codes,
        "duration_seconds": value.duration_seconds,
        "step_count": value.step_count,
        "tool_call_count": value.tool_call_count,
        "patch_count": value.patch_count,
        "tokens": {
            "input": value.input_tokens,
            "output": value.output_tokens,
            "reasoning": value.reasoning_tokens,
            "cache_read": value.cache_read_tokens,
            "cache_write": value.cache_write_tokens,
            "total": value.total_tokens,
        },
        "usage_by_model": value.usage_by_model,
        "top_tools": value.top_tools(),
        "signals": value.signals,
        "truncated": value.truncated,
    }


def _print_assessment(console, value, *, title):
    summary = Table(title=f"{title}: {', '.join(value.session_ids)}")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("Status", value.status)
    summary.add_row("Reasons", ", ".join(value.reason_codes) or "-")
    summary.add_row("Duration", _number(value.duration_seconds, suffix="s"))
    summary.add_row("Steps", _number(value.step_count))
    summary.add_row("Tool Calls", _number(value.tool_call_count))
    summary.add_row("Patches", _number(value.patch_count))
    console.print(summary)

    tokens = Table(title="Tokens")
    tokens.add_column("Bucket", style="cyan")
    tokens.add_column("Value", justify="right")
    for label, amount in (
        ("Input", value.input_tokens),
        ("Output", value.output_tokens),
        ("Reasoning", value.reasoning_tokens),
        ("Cache Read", value.cache_read_tokens),
        ("Cache Write", value.cache_write_tokens),
        ("Total", value.total_tokens),
    ):
        tokens.add_row(label, _number(amount))
    console.print(tokens)

    models = Table(title="Usage by Model")
    models.add_column("Model", style="cyan")
    models.add_column("Total", justify="right")
    for model, usage in sorted(value.usage_by_model.items()):
        models.add_row(model, str(usage["total"]))
    console.print(models)

    tools = Table(title="Top Tools")
    tools.add_column("Tool", style="cyan")
    tools.add_column("Calls", justify="right")
    for tool_name, count in value.top_tools():
        tools.add_row(tool_name, str(count))
    console.print(tools)


def _print_comparison(console, current, baseline):
    table = Table(title=f"Comparison vs {baseline.session_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Current", justify="right")
    table.add_column("Baseline", justify="right")
    table.add_column("Delta", justify="right")
    table.add_column("Delta %", justify="right")
    for metric, values in compare_sessions(current, baseline).items():
        table.add_row(
            metric,
            _number(values["current"]),
            _number(values["baseline"]),
            _number(values["delta"]),
            _number(values["pct_delta"], suffix="%"),
        )
    console.print(table)


def _number(value, *, suffix="") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


__all__ = ["register_assessment_commands"]
