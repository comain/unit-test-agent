"""Repository discovery and parsed-index CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from rich.table import Table

from uta.shared.config import settings


_QUERY_SECTIONS = (
    "summary",
    "plan_summary",
    "generation_summary",
    "generation_lookup",
    "fix_summary",
    "class",
    "imports",
    "fields",
    "methods",
    "dependencies",
    "flows",
    "nearby_tests",
    "symbols",
    "callers",
)


def register_index_commands(
    main,
    *,
    console,
    load_index_payload,
    normalize_cli_targets,
    python_query_index_payload,
    resolve_cli_language,
) -> None:
    """Attach source-discovery and index-query commands to main."""
    _load_index_payload = load_index_payload
    _normalize_cli_targets = normalize_cli_targets
    _python_query_index_payload = python_query_index_payload
    _resolve_cli_language = resolve_cli_language

    @main.command()
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--days", default=settings.default_days, help="Scan git log for last N days")
    @click.option("--module", default=None, help="Target Maven module name")
    @click.option("--all", "select_all_files", is_flag=True, help="List all production files instead of git-history ranking")
    def scan(repo, language, days, module, select_all_files):
        """Scan and list candidate files for test generation."""
        from uta.testgen.source_selection import get_all_source_files, get_changed_source_files

        decision = _resolve_cli_language(repo, language)
        if select_all_files:
            console.print(f"[bold blue]Scanning all production {decision.language} files in {repo}...[/bold blue]")
            files = get_all_source_files(decision.language, repo, module)
        else:
            console.print(f"[bold blue]Scanning {decision.language} files in {repo} for the last {days} days...[/bold blue]")
            files = get_changed_source_files(decision.language, repo, days, module)

        table = Table(title=f"Candidates in {repo}")
        table.add_column("Target", style="cyan")
        table.add_column("Changes", justify="right")

        for path, count in files[:20]:
            table.add_row(path, str(count))

        console.print(table)
        console.print(f"\nTotal: {len(files)} candidate files")


    @main.command()
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--module", default=None, help="Target Maven module name")
    @click.option("--max-files", default=500, show_default=True, type=int, help="Maximum Python production files to parse")
    def parse(repo, language, module, max_files):
        """Deep parse the module and cache code graph/flows."""
        repo = os.path.abspath(repo)
        decision = _resolve_cli_language(repo, language)
        from uta.shared.parse import ParseProjectRequest, make_parse_provider

        if decision.language == "python":
            parsed = make_parse_provider("python").parse_project(ParseProjectRequest(repo_path=Path(repo), max_files=max_files))
            index_path = parsed.write_project_index()

            console.print(f"[bold yellow]Parsing Python sources in {repo}...[/bold yellow]")
            console.print(f"Found {len(parsed.source_files)} Python files")
            skipped_count = int((parsed.selection or {}).get("skipped_count") or 0)
            if skipped_count:
                console.print(f"Skipped: {skipped_count} Python files (max_files_exceeded)")
            console.print(f"Symbols: {len(parsed.callables)}")
            console.print(f"Index: {index_path}")
            return

        console.print(f"[bold yellow]Parsing {module or 'all'} in {repo}...[/bold yellow]")

        cache_dir = Path(repo) / ".uta_cache"
        cache_files = list(cache_dir.glob("*.json")) if cache_dir.exists() else []
        parsed = make_parse_provider("java").parse_project(ParseProjectRequest(repo_path=Path(repo), module=module))
        graph = parsed.graph
        flows = parsed.flows
        console.print(f"Found {len(parsed.source_files)} Java files")
        console.print(f"Parsed cache available: {len(cache_files)} artifact(s)")
        console.print(f"Graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")
        console.print(f"Flows detected: {len(flows)}")

        # Show top flows
        if flows:
            table = Table(title="Top Process Flows")
            table.add_column("Entry Point", style="cyan")
            table.add_column("Steps", justify="right")
            table.add_column("External Deps")
            for flow in flows[:10]:
                ext_deps = ", ".join(s.kind for s in flow.steps if s.kind != "internal_call")
                table.add_row(flow.name, str(len(flow.steps)), ext_deps or "-")
            console.print(table)

        # Show class stats
        classes = [n for n in graph.nodes.values() if n.kind == "class"]
        table = Table(title="Classes Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        table.add_row("Total classes", str(len(classes)))
        table.add_row("Total methods", str(len([n for n in graph.nodes.values() if n.kind == "method"])))
        table.add_row("Total edges", str(len(graph.edges)))
        console.print(table)


    @main.command("query-index")
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--module", default=None, help="Target Maven module name")
    @click.option("--class-fqn", required=False, help="Class FQN to inspect")
    @click.option("--target", default=None, help="Language-neutral target. For Python use path.py or path.py::symbol.")
    @click.option(
        "--section",
        "sections",
        multiple=True,
        type=click.Choice(_QUERY_SECTIONS, case_sensitive=False),
        help="Section(s) to return. Repeat as needed. Defaults to summary.",
    )
    @click.option("--method", "method_name", default=None, help="Optional exact method name filter")
    @click.option("--symbol", default=None, help="Optional exact symbol name filter")
    @click.option("--limit", default=20, show_default=True, type=int, help="Per-section item cap")
    @click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
    def query_index(repo, language, module, class_fqn, target, sections, method_name, symbol, limit, json_output):
        """Query the tree-sitter-derived class index without opening source files first."""
        repo = os.path.abspath(repo)
        module = (module or "").strip() or None
        decision = _resolve_cli_language(
            repo,
            language,
            class_fqns=[class_fqn] if class_fqn else [],
            targets=[target] if target else [],
        )
        if decision.language == "python":
            if not target:
                raise click.ClickException("--target is required for Python query-index")
            target_ref = _normalize_cli_targets("python", [target])[0]
            payload = _python_query_index_payload(repo, target_ref, decision)
            if json_output:
                click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return
            if not payload["found"]:
                raise click.ClickException(payload.get("error") or f"Python target not found: {target_ref.display_name}")
            console.print(f"[bold cyan]{target_ref.display_name}[/bold cyan]")
            console.print("Language: python")
            console.print(f"Source: {target_ref.source_path}")
            if target_ref.symbol:
                console.print(f"Symbol: {target_ref.symbol}")
            return

        class_fqn = class_fqn or target
        if not class_fqn:
            raise click.ClickException("--class-fqn is required for Java query-index")
        payload, resolved_module = _load_index_payload(
            repo,
            module,
            class_fqn,
            sections=[section.lower() for section in sections],
            limit=limit,
            method_name=method_name,
            symbol=symbol,
        )
        if resolved_module:
            payload.setdefault("class", {})
            payload["class"]["module"] = resolved_module

        if json_output:
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return

        if not payload.get("found"):
            raise click.ClickException(f"Class not found in parsed graph: {class_fqn}")

        console.print(f"[bold cyan]{class_fqn}[/bold cyan]")
        class_info = payload.get("class") or {}
        if class_info:
            console.print(f"Source: {class_info.get('source_path')}")
            if class_info.get("module"):
                console.print(f"Module: {class_info['module']}")

        for key in ("imports", "fields", "methods", "dependencies", "flows", "nearby_tests", "symbols", "callers"):
            if key not in payload:
                continue
            console.print(f"\n[bold]{key}[/bold]")
            click.echo(json.dumps(payload[key], ensure_ascii=False, sort_keys=True))




__all__ = ["register_index_commands"]
