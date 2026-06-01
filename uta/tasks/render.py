import html
import json
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from uta.tasks.db import TaskDB
from uta.tasks.models import json_loads
from uta.tasks.targets import display_target


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def _is_java_task(task: Dict[str, Any], classes: list[Dict[str, Any]]) -> bool:
    language = task.get("language") or (task.get("selection") or {}).get("language") or "java"
    return language == "java" and all((row.get("language") or "java") == "java" for row in classes)


def build_status_payload(db: TaskDB, repo_task_id: int) -> Dict[str, Any]:
    task = db.get_repo_task(repo_task_id)
    if not task:
        raise KeyError(f"Task {repo_task_id} not found")
    classes = []
    for row in db.list_class_tasks(repo_task_id):
        item = _row_to_dict(row)
        item["target_display_name"] = display_target(item)
        try:
            item["session_ids"] = json.loads(item.get("session_ids_json") or "[]")
        except json.JSONDecodeError:
            item["session_ids"] = []
        item.pop("session_ids_json", None)
        classes.append(item)
    latest_events = [_row_to_dict(row) for row in db.latest_events(repo_task_id, limit=30)]
    latest_heartbeat = _row_to_dict(db.latest_heartbeat())
    task_dict = _row_to_dict(task)
    task_dict["selection"] = json_loads(task_dict.get("selection_json"))
    task_dict["config_snapshot"] = json_loads(task_dict.get("config_snapshot_json"))
    task_dict["budget_snapshot"] = json_loads(task_dict.get("budget_snapshot_json"))
    task_dict["estimate_snapshot"] = json_loads(task_dict.get("estimate_snapshot_json"))
    try:
        task_dict["session_ids"] = json.loads(task_dict.get("session_ids_json") or "[]")
    except json.JSONDecodeError:
        task_dict["session_ids"] = []
    task_dict["config_snapshot_hash"] = hashlib.sha256(
        json.dumps(task_dict["config_snapshot"], sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    task_dict["budget_snapshot_hash"] = hashlib.sha256(
        json.dumps(task_dict["budget_snapshot"], sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    for key in (
        "selection_json",
        "config_snapshot_json",
        "budget_snapshot_json",
        "budget_config_snapshot_json",
        "estimate_snapshot_json",
        "session_ids_json",
    ):
        task_dict.pop(key, None)
    input_tokens = int(task_dict.get("input_tokens") or task_dict.get("actual_input_tokens") or 0)
    cache_read = int(task_dict.get("cache_read_tokens") or task_dict.get("actual_cache_read_tokens") or 0)
    output_tokens = int(task_dict.get("output_tokens") or task_dict.get("actual_output_tokens") or 0)
    estimated_cost = task_dict.get("estimated_cost_usd") if task_dict.get("estimated_cost_usd") is not None else task_dict.get("estimated_cost")
    provider_cost = task_dict.get("provider_cost_usd")
    actual_cost = provider_cost if float(provider_cost or 0.0) > 0.0 else task_dict.get("actual_cost")
    budget_used_pct = None
    if estimated_cost and actual_cost is not None:
        budget_used_pct = (float(actual_cost) / max(float(estimated_cost) * 2.0, 0.000001)) * 100.0
    metrics = {
        "cache_hit_ratio": (cache_read / max(input_tokens + cache_read, 1)),
        "input_output_ratio": (input_tokens / max(output_tokens, 1)),
        "budget_used_pct": budget_used_pct,
        "projected_final_budget_pct": budget_used_pct,
        "estimated_cost": estimated_cost,
        "actual_cost": actual_cost,
        "estimated_total_tokens": task_dict.get("estimated_total_tokens"),
        "actual_total_tokens": task_dict.get("total_tokens") or task_dict.get("actual_input_tokens", 0) + task_dict.get("actual_output_tokens", 0),
        "estimated_elapsed_seconds": task_dict.get("estimated_elapsed_seconds") or task_dict.get("estimated_seconds"),
        "actual_elapsed_seconds": task_dict.get("actual_elapsed_seconds") or task_dict.get("elapsed_seconds"),
        "remaining_estimated_cost": None if estimated_cost is None or actual_cost is None else max(float(estimated_cost) * 2.0 - float(actual_cost), 0.0),
        "remaining_estimated_tokens": None if task_dict.get("estimated_total_tokens") is None else max(int(task_dict.get("estimated_total_tokens") or 0) - int(task_dict.get("total_tokens") or 0), 0),
        "remaining_estimated_seconds": None if (task_dict.get("estimated_elapsed_seconds") or task_dict.get("estimated_seconds")) is None or (task_dict.get("actual_elapsed_seconds") or task_dict.get("elapsed_seconds")) is None else max(float(task_dict.get("estimated_elapsed_seconds") or task_dict.get("estimated_seconds") or 0.0) - float(task_dict.get("actual_elapsed_seconds") or task_dict.get("elapsed_seconds") or 0.0), 0.0),
        "highest_cost_stage": task_dict.get("current_stage"),
    }
    return {
        "task": task_dict,
        "classes": classes,
        "latest_events": latest_events,
        "latest_heartbeat": latest_heartbeat,
        "aggregates": db.aggregate_repo_task(repo_task_id),
        "metrics": metrics,
    }


def html_for_payload(payload: Dict[str, Any]) -> str:
    task = payload["task"]
    classes = payload["classes"]
    events = payload["latest_events"]
    aggregates = payload.get("aggregates") or {}
    metrics = payload.get("metrics") or {}
    heartbeat = payload.get("latest_heartbeat") or {}
    title = f"UTA Task {task['id']} - {task['status']}"
    def _fmt_tok(n) -> str:
        n = int(n or 0)
        if n == 0:
            return "0"
        return f"{n//1000}K" if n < 1_000_000 else f"{n/1_000_000:.1f}M"

    def _fmt_tokens_html_total(rows: list) -> str:
        inp = sum(int(r.get("input_tokens") or r.get("actual_input_tokens") or 0) for r in rows)
        cr = sum(int(r.get("cache_read_tokens") or r.get("actual_cache_read_tokens") or 0) for r in rows)
        out = sum(int(r.get("output_tokens") or r.get("actual_output_tokens") or 0) for r in rows)
        rsn = sum(int(r.get("reasoning_tokens") or 0) for r in rows)
        total = inp + cr + out + rsn
        parts = [f"in={_fmt_tok(inp)}", f"cr={_fmt_tok(cr)}", f"out={_fmt_tok(out)}"]
        if rsn:
            parts.append(f"rsn={_fmt_tok(rsn)}")
        parts.append(f"total={_fmt_tok(total)}")
        return " ".join(parts)

    def _fmt_tokens_html(row: Dict[str, Any]) -> str:
        inp = int(row.get("input_tokens") or row.get("actual_input_tokens") or 0)
        cr = int(row.get("cache_read_tokens") or row.get("actual_cache_read_tokens") or 0)
        out = int(row.get("output_tokens") or row.get("actual_output_tokens") or 0)
        rsn = int(row.get("reasoning_tokens") or 0)
        if not (inp or cr or out or rsn):
            return ""
        parts = [f"in={_fmt_tok(inp)}", f"cr={_fmt_tok(cr)}", f"out={_fmt_tok(out)}"]
        if rsn:
            parts.append(f"rsn={_fmt_tok(rsn)}")
        return " ".join(parts)

    class_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row['id']))}</td>"
        f"<td>{html.escape(row.get('target_display_name') or row.get('class_fqn') or '')}</td>"
        f"<td>{html.escape(row.get('status') or '')}</td>"
        f"<td>{html.escape(row.get('current_stage') or '')}</td>"
        f"<td>{row.get('coverage_line') if row.get('coverage_line') is not None else ''}</td>"
        f"<td>{row.get('mutation_score') if row.get('mutation_score') is not None else ''}</td>"
        f"<td>{row.get('test_count') if row.get('test_count') is not None else ''}</td>"
        f"<td>{_fmt_tokens_html(row)}</td>"
        f"<td>{html.escape(', '.join(row.get('session_ids') or []))}</td>"
        "</tr>"
        for row in classes
    )
    event_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(row.get('created_at') or '')}</td>"
        f"<td>{html.escape(row.get('severity') or '')}</td>"
        f"<td>{html.escape(row.get('stage') or '')}</td>"
        f"<td>{html.escape(row.get('message') or '')}</td>"
        "</tr>"
        for row in events
    )
    status_counts = ", ".join(f"{key}: {value}" for key, value in (aggregates.get("statuses") or {}).items())
    budget_used = "" if metrics.get("budget_used_pct") is None else f"{metrics['budget_used_pct']:.1f}%"
    heartbeat_text = "none"
    if heartbeat:
        heartbeat_text = f"{heartbeat.get('runner_id') or ''} {heartbeat.get('status') or ''} {heartbeat.get('heartbeat_at') or heartbeat.get('updated_at') or ''}"
    heartbeat_hash = heartbeat.get("loaded_config_hash") or ""
    is_java_task = _is_java_task(task, classes)
    target_label = "Class" if is_java_task else "Target"
    target_section_label = "Classes" if is_java_task else "Targets"
    total_tokens_label = "Total Tokens (all classes)" if is_java_task else "Total Tokens (all targets)"
    status_counts_label = "Class Statuses" if is_java_task else "Target Statuses"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; color: #17202a; background: #f7f4ee; }}
    h1 {{ margin: 0 0 8px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
    .card {{ background: #fff; border: 1px solid #dfd6c8; border-radius: 10px; padding: 14px; box-shadow: 0 1px 2px rgba(0,0,0,.05); }}
    .label {{ color: #6b6257; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }}
    .value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff; margin: 16px 0 28px; }}
    th, td {{ border: 1px solid #e0d8cc; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #efe6d7; }}
    code {{ background: #efe6d7; padding: 2px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div><code>{html.escape(task.get('repo_path') or '')}</code></div>
  <div class="cards">
    <div class="card"><div class="label">Stage</div><div class="value">{html.escape(task.get('current_stage') or '')}</div></div>
    <div class="card"><div class="label">Priority</div><div class="value">{task.get('priority')}</div></div>
    <div class="card"><div class="label">Branch</div><div class="value">{html.escape(task.get('branch_name') or '')}</div></div>
    <div class="card"><div class="label">Estimated Cost</div><div class="value">{task.get('estimated_cost') if task.get('estimated_cost') is not None else ''}</div></div>
    <div class="card"><div class="label">Actual Cost</div><div class="value">{metrics.get('actual_cost') if metrics.get('actual_cost') is not None else ''}</div></div>
    <div class="card"><div class="label">Budget Used</div><div class="value">{html.escape(budget_used)}</div></div>
    <div class="card"><div class="label">Cache Hit Ratio</div><div class="value">{float(metrics.get('cache_hit_ratio') or 0.0):.1%}</div></div>
    <div class="card"><div class="label">Remaining Cost</div><div class="value">{metrics.get('remaining_estimated_cost') if metrics.get('remaining_estimated_cost') is not None else ''}</div></div>
    <div class="card"><div class="label">Remaining Tokens</div><div class="value">{metrics.get('remaining_estimated_tokens') if metrics.get('remaining_estimated_tokens') is not None else ''}</div></div>
    <div class="card"><div class="label">Daemon</div><div class="value">{html.escape(heartbeat_text)}</div></div>
    <div class="card"><div class="label">Daemon Config</div><div class="value">{html.escape(heartbeat_hash)}</div></div>
    <div class="card"><div class="label">{total_tokens_label}</div><div class="value">{html.escape(_fmt_tokens_html_total(classes))}</div></div>
    <div class="card"><div class="label">{status_counts_label}</div><div class="value">{html.escape(status_counts)}</div></div>
    <div class="card"><div class="label">Config Hash</div><div class="value">{html.escape(task.get('config_snapshot_hash') or '')}</div></div>
    <div class="card"><div class="label">Restart Boundary</div><div class="value">task snapshots fixed; daemon env changes require restart</div></div>
  </div>
  <h2>{target_section_label}</h2>
  <table>
    <tr><th>ID</th><th>{target_label}</th><th>Status</th><th>Stage</th><th>Coverage</th><th>Mutation</th><th>Tests</th><th>Tokens</th><th>Session IDs</th></tr>
    {class_rows}
  </table>
  <h2>Events</h2>
  <table>
    <tr><th>Time</th><th>Severity</th><th>Stage</th><th>Message</th></tr>
    {event_rows}
  </table>
</body>
</html>
"""


def write_live_status(db: TaskDB, repo_task_id: int, *, repo_path: Optional[str] = None) -> Dict[str, str]:
    payload = build_status_payload(db, repo_task_id)
    base = Path(repo_path or payload["task"]["repo_path"]) / ".uta_reports"
    base.mkdir(parents=True, exist_ok=True)
    json_path = base / "live_status.json"
    html_path = base / "status.html"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    html_path.write_text(html_for_payload(payload), encoding="utf-8")
    db.update_repo_task(repo_task_id, latest_live_status_path=str(html_path))
    return {"json": str(json_path), "html": str(html_path)}


FOCUSED_CLASS_STATUSES_FOR_DISPLAY = {"RUNNING", "QUEUED", "PENDING", "STOPPED"}
FAILED_CLASS_STATUSES_FOR_DISPLAY = {
    "FAIL",
    "MUTATION_FAIL",
    "BUDGET_EXCEEDED",
    "PROVIDER_ERROR",
    "PROVIDER_RATE_LIMITED",
    "PUSH_FAILED",
    "UNSAFE_DIFF",
    "LLM_STALLED",
    "CANCELLED",
}


def _visible_class_rows(classes: list[Dict[str, Any]], *, detail: bool, limit: int) -> tuple[list[Dict[str, Any]], int]:
    if detail or len(classes) <= limit:
        return classes, 0
    focused = [row for row in classes if (row.get("status") or "") in FOCUSED_CLASS_STATUSES_FOR_DISPLAY]
    failed = [row for row in classes if (row.get("status") or "") in FAILED_CLASS_STATUSES_FOR_DISPLAY]
    recent = sorted(classes, key=lambda row: (row.get("updated_at") or "", row.get("id") or 0), reverse=True)
    selected: list[Dict[str, Any]] = []
    seen = set()
    for row in focused + failed + classes + recent:
        row_id = row.get("id")
        if row_id in seen:
            continue
        selected.append(row)
        seen.add(row_id)
        if len(selected) >= limit:
            break
    return selected, max(0, len(classes) - len(selected))


def build_task_renderables(
    payload: Dict[str, Any],
    *,
    show_sessions: bool = False,
    detail: bool = False,
    class_limit: int = 25,
) -> Group:
    """Return a Rich Group renderable for the task status — no side effects."""
    task = payload["task"]
    metrics = payload.get("metrics") or {}
    classes = payload.get("classes") or []
    aggregates = payload.get("aggregates") or {}
    is_java_task = _is_java_task(task, classes)
    target_label = "Class" if is_java_task else "Target"
    target_section_label = "Class Tasks" if is_java_task else "Targets"
    row_label = "class rows" if is_java_task else "target rows"
    count_label = "classes" if is_java_task else "targets"
    budget_text = "-" if metrics.get("budget_used_pct") is None else f"{metrics['budget_used_pct']:.1f}%"
    class_count = len(classes)
    shown_classes, hidden_count = _visible_class_rows(classes, detail=detail, limit=max(1, int(class_limit or 25)))
    status_counts = aggregates.get("statuses") or {}
    status_text = ", ".join(f"{status}:{count}" for status, count in sorted(status_counts.items())) or "-"

    header = Text.from_markup(
        f"[bold]Task {task['id']}[/bold] {task['status']} "
        f"stage={task.get('current_stage') or '-'} branch={task.get('branch_name') or '-'}"
    )
    # Aggregate token totals across all classes
    total_inp = sum(int(r.get("input_tokens") or r.get("actual_input_tokens") or 0) for r in classes)
    total_cr = sum(int(r.get("cache_read_tokens") or r.get("actual_cache_read_tokens") or 0) for r in classes)
    total_out = sum(int(r.get("output_tokens") or r.get("actual_output_tokens") or 0) for r in classes)
    total_rsn = sum(int(r.get("reasoning_tokens") or 0) for r in classes)
    total_tok = total_inp + total_cr + total_out + total_rsn

    def _fmt_tok_hdr(n: int) -> str:
        return f"{n//1000}K" if n < 1_000_000 else f"{n/1_000_000:.1f}M"

    tok_parts = [
        f"in={_fmt_tok_hdr(total_inp)}",
        f"cr={_fmt_tok_hdr(total_cr)}",
        f"out={_fmt_tok_hdr(total_out)}",
    ]
    if total_rsn:
        tok_parts.append(f"rsn={_fmt_tok_hdr(total_rsn)}")
    tok_parts.append(f"total={_fmt_tok_hdr(total_tok)}")
    tok_summary = " ".join(tok_parts)

    passed = int(aggregates.get("passed_classes") or 0)
    pct = f"{passed/class_count:.1%}" if class_count else "0.0%"
    cost_line = Text(
        f"progress={passed}/{class_count} ({pct}) "
        "cost actual/est="
        f"{metrics.get('actual_cost') if metrics.get('actual_cost') is not None else '-'}"
        f"/{metrics.get('estimated_cost') if metrics.get('estimated_cost') is not None else '-'} "
        f"cache_hit={float(metrics.get('cache_hit_ratio') or 0.0):.1%} "
        f"budget_used={budget_text}"
    )
    tok_line = Text(f"tokens {tok_summary}")
    summary_line = Text(
        f"{count_label} total={class_count} completed={aggregates.get('completed_classes') or 0} "
        f"passed={aggregates.get('passed_classes') or 0} failed={aggregates.get('failed_classes') or 0} "
        f"statuses={status_text}"
    )
    slice_line = Text(
        f"{row_label}: showing all"
        if hidden_count == 0
        else f"{row_label}: showing {len(shown_classes)} of {class_count}; hidden={hidden_count}; use --detail to show all"
    )

    table = Table(title=target_section_label)
    table.add_column("ID", justify="right")
    table.add_column("Priority", justify="right")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column(target_label)
    table.add_column("Cov")
    table.add_column("Mut")
    table.add_column("Tests")
    table.add_column("Tokens", justify="right")
    if show_sessions:
        table.add_column("Sessions")

    def _fmt_tok_short(n) -> str:
        n = int(n or 0)
        if n == 0:
            return "0"
        return f"{n//1000}K" if n < 1_000_000 else f"{n/1_000_000:.1f}M"

    for row in shown_classes:
        inp = int(row.get("input_tokens") or row.get("actual_input_tokens") or 0)
        cr = int(row.get("cache_read_tokens") or row.get("actual_cache_read_tokens") or 0)
        out = int(row.get("output_tokens") or row.get("actual_output_tokens") or 0)
        rsn = int(row.get("reasoning_tokens") or 0)
        if inp or cr or out or rsn:
            parts = [f"in={_fmt_tok_short(inp)}", f"cr={_fmt_tok_short(cr)}", f"out={_fmt_tok_short(out)}"]
            if rsn:
                parts.append(f"rsn={_fmt_tok_short(rsn)}")
            tok_str = " ".join(parts)
        else:
            tok_str = ""
        cells = [
            str(row["id"]),
            str(row["priority"]),
            row.get("status") or "",
            row.get("current_stage") or "",
            row.get("target_display_name") or row.get("class_fqn") or "",
            "" if row.get("coverage_line") is None else f"{float(row['coverage_line']):.1f}",
            "" if row.get("mutation_score") is None else f"{float(row['mutation_score']):.1f}",
            "" if row.get("test_count") is None else str(row["test_count"]),
            tok_str,
        ]
        if show_sessions:
            cells.append(", ".join(row.get("session_ids") or []))
        table.add_row(*cells)
    return Group(header, cost_line, tok_line, summary_line, slice_line, table)


def render_task_table(console: Console, payload: Dict[str, Any], *, show_sessions: bool = False, detail: bool = False) -> None:
    console.print(build_task_renderables(payload, show_sessions=show_sessions, detail=detail))
