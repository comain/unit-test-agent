import logging
import os
import datetime
import time
import tempfile
import json
import subprocess
import zipfile
import hashlib
import xml.etree.ElementTree as ET
import sys
from typing import Optional, Dict, Any, List, Tuple, Iterable
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.live import Live

from uta.config import settings
from uta.assessment import (
    DEFAULT_OPENCODE_DB,
    assess_sessions,
    compare_sessions,
    iso_local,
    top_tools_rows,
)

console = Console()
logger = logging.getLogger("uta")

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


def _provider_from_model(model_id: str) -> Optional[str]:
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0]


def _load_graph_and_flows(repo: str, module: Optional[str]) -> Tuple[Any, List[Any]]:
    from uta.engine.parse import ParseProjectRequest, make_parse_provider

    parsed = make_parse_provider("java").parse_project(
        ParseProjectRequest(repo_path=Path(os.path.abspath(repo)), module=module)
    )
    return parsed.graph, parsed.flows


def _iter_module_roots(base: Path, *, max_depth: int = 3) -> Iterable[Path]:
    seen = set()
    if (base / "src" / "main" / "java").exists():
        resolved = base.resolve()
        seen.add(str(resolved))
        yield resolved
    for src_root in sorted(base.rglob("src/main/java")):
        try:
            depth = len(src_root.relative_to(base).parts)
        except ValueError:
            continue
        if depth > max_depth + 3:
            continue
        module_root = src_root.parent.parent.parent.resolve()
        key = str(module_root)
        if key in seen:
            continue
        seen.add(key)
        yield module_root


def _looks_like_shared_source_repo(path: Path) -> bool:
    name = path.name.lower()
    return (
        "api" in name
        or "util" in name
        or "common" in name
    )


def _configured_source_bases() -> List[Path]:
    raw = (settings.index_source_dirs or "").strip()
    if not raw:
        return []
    normalized = raw.replace("\n", ",")
    if os.pathsep in normalized:
        normalized = normalized.replace(os.pathsep, ",")
    bases: List[Path] = []
    seen = set()
    for item in normalized.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        bases.append(path)
    return bases


def _discover_configured_module_roots() -> List[Path]:
    roots: List[Path] = []
    seen = set()
    for base in _configured_source_bases():
        for root in _iter_module_roots(base, max_depth=5):
            key = str(root.resolve())
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
    return roots


def _discover_sibling_module_roots(repo: str) -> List[Path]:
    repo_root = Path(os.path.abspath(repo)).resolve()
    parent = repo_root.parent
    roots: List[Path] = []
    seen = set()
    for child in sorted(parent.iterdir()):
        if child.resolve() == repo_root or not child.is_dir():
            continue
        if not _looks_like_shared_source_repo(child):
            continue
        for root in _iter_module_roots(child, max_depth=5):
            key = str(root.resolve())
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
    return roots


def _maven_settings_path() -> Optional[Path]:
    configured = (settings.maven_settings_path or "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        return path if path.exists() else None
    default = Path("~/.m2/settings.xml").expanduser()
    return default.resolve() if default.exists() else None


def _maven_local_repository() -> Path:
    settings_path = _maven_settings_path()
    default_repo = Path("~/.m2/repository").expanduser().resolve()
    if not settings_path:
        return default_repo
    try:
        tree = ET.parse(settings_path)
        root = tree.getroot()
    except ET.ParseError:
        return default_repo
    local_repo = None
    for node in root.iter():
        if node.tag.endswith("localRepository") and (node.text or "").strip():
            local_repo = (node.text or "").strip()
            break
    if not local_repo:
        return default_repo
    expanded = os.path.expanduser(os.path.expandvars(local_repo))
    return Path(expanded).resolve()


def _class_source_relpath(class_fqn: str) -> Optional[Path]:
    parts = class_fqn.split(".")
    if len(parts) < 2:
        return None
    return Path(*parts[:-1]) / f"{parts[-1]}.java"


def _source_jar_cache_root(repo: str) -> Path:
    root = Path(repo) / ".uta_cache" / "index_sources"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_source_jar_to_cache(repo: str, jar_path: Path) -> Path:
    fingerprint = hashlib.sha256()
    fingerprint.update(str(jar_path.resolve()).encode("utf-8"))
    stat = jar_path.stat()
    fingerprint.update(str(stat.st_mtime_ns).encode("utf-8"))
    fingerprint.update(str(stat.st_size).encode("utf-8"))
    extract_root = _source_jar_cache_root(repo) / fingerprint.hexdigest()
    src_root = extract_root / "src" / "main" / "java"
    marker = extract_root / ".complete"
    if marker.exists() and src_root.exists():
        return extract_root
    src_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar_path) as archive:
        for member in archive.namelist():
            if member.endswith("/") or not member.endswith(".java"):
                continue
            target = src_root / member
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as dest:
                dest.write(source.read())
    marker.write_text(str(jar_path), encoding="utf-8")
    return extract_root


def _find_source_jars_for_class(class_fqn: str) -> List[Path]:
    relative = _class_source_relpath(class_fqn)
    if relative is None:
        return []
    rel_posix = relative.as_posix()
    local_repo = _maven_local_repository()
    search_roots: List[Path] = []
    package_parts = class_fqn.split(".")[:-1]
    for depth in range(min(len(package_parts), 4), 1, -1):
        candidate = local_repo.joinpath(*package_parts[:depth])
        if candidate.exists():
            search_roots.append(candidate)
            break
    search_roots.append(local_repo)
    seen_roots = set()
    matches: List[Path] = []
    for root in search_roots:
        root_key = str(root.resolve())
        if root_key in seen_roots or not root.exists():
            continue
        seen_roots.add(root_key)
        for jar_path in sorted(root.rglob("*-sources.jar")):
            try:
                with zipfile.ZipFile(jar_path) as archive:
                    if rel_posix in archive.namelist():
                        matches.append(jar_path.resolve())
            except zipfile.BadZipFile:
                continue
    deduped: List[Path] = []
    seen = set()
    for match in matches:
        key = str(match)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _fetch_dependency_sources(repo: str) -> None:
    if not settings.index_fetch_sources:
        return
    repo_root = Path(os.path.abspath(repo))
    pom_path = repo_root / "pom.xml"
    if not pom_path.exists():
        return
    cmd = [settings.maven_bin, "-q"]
    settings_path = _maven_settings_path()
    if settings_path:
        cmd.extend(["-s", str(settings_path)])
    cmd.extend(["dependency:sources", "-DexcludeTransitive=false"])
    try:
        subprocess.run(
            cmd,
            cwd=str(repo_root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("query-index failed to fetch dependency sources via Maven", exc_info=True)


def _load_graph_from_source_jars(repo: str, source_jars: List[Path]) -> Tuple[Any, List[Any]]:
    module_roots = [_extract_source_jar_to_cache(repo, jar_path) for jar_path in source_jars]
    return _load_graph_and_flows_for_roots(repo, module_roots)


def _discover_external_module_roots(repo: str) -> List[Path]:
    repo_root = Path(os.path.abspath(repo)).resolve()
    roots: List[Path] = []
    seen = set()
    for root in _discover_configured_module_roots() + _discover_sibling_module_roots(repo):
        resolved = root.resolve()
        if resolved == repo_root:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _load_graph_and_flows_for_roots(repo: str, module_roots: List[Path]) -> Tuple[Any, List[Any]]:
    java_files: List[Path] = []
    seen = set()
    for module_root in module_roots:
        src_root = module_root / "src" / "main" / "java"
        if not src_root.exists():
            continue
        for java_file in src_root.rglob("*.java"):
            key = str(java_file.resolve())
            if key in seen:
                continue
            seen.add(key)
            java_files.append(java_file)

    from uta.engine.parse import ParseProjectRequest, make_parse_provider

    parsed = make_parse_provider("java").parse_project(
        ParseProjectRequest(repo_path=Path(os.path.abspath(repo)), source_files=java_files)
    )
    return parsed.graph, parsed.flows


def _find_class_source(repo: str, class_fqn: str, module: Optional[str]) -> Optional[Path]:
    repo_root = Path(os.path.abspath(repo))
    search_root = repo_root / module if module else repo_root
    relative = _class_source_relpath(class_fqn)
    if relative is None:
        return None
    matches = sorted(search_root.glob(f"**/src/main/java/{relative.as_posix()}"))
    if matches:
        return matches[0]
    fallback = sorted(repo_root.glob(f"**/src/main/java/{relative.as_posix()}"))
    if fallback:
        return fallback[0]
    for external_root in _discover_external_module_roots(repo):
        candidate = external_root / "src" / "main" / "java" / relative
        if candidate.exists():
            return candidate
    return None


def _module_for_source(repo: str, source_path: Path) -> Optional[str]:
    repo_root = Path(os.path.abspath(repo)).resolve()
    resolved = source_path.resolve()
    try:
        rel = resolved.relative_to(repo_root)
    except ValueError:
        return None
    parts = rel.parts
    try:
        src_index = parts.index("src")
    except ValueError:
        return None
    if src_index == 0:
        return None
    return str(Path(*parts[:src_index]))


def _load_index_payload(
    repo: str,
    module: Optional[str],
    class_fqn: str,
    *,
    sections: List[str],
    limit: int,
    method_name: Optional[str],
    symbol: Optional[str],
) -> Tuple[Dict[str, Any], Optional[str]]:
    from uta.language.java.context_builder import ContextBuilder

    graph, flows = _load_graph_and_flows(repo, module)
    builder = ContextBuilder(repo, graph, flows)
    payload = builder.build_index_payload(
        class_fqn,
        module=module,
        sections=sections,
        limit=limit,
        method_name=method_name,
        symbol=symbol,
    )
    if payload.get("found"):
        return payload, module

    source_path = _find_class_source(repo, class_fqn, module)
    fallback_module = _module_for_source(repo, source_path) if source_path else None
    if fallback_module and fallback_module != module:
        graph, flows = _load_graph_and_flows(repo, fallback_module)
        builder = ContextBuilder(repo, graph, flows)
        payload = builder.build_index_payload(
            class_fqn,
            module=fallback_module,
            sections=sections,
            limit=limit,
            method_name=method_name,
            symbol=symbol,
        )
        if payload.get("found"):
            return payload, fallback_module

    if module is not None:
        graph, flows = _load_graph_and_flows(repo, None)
        builder = ContextBuilder(repo, graph, flows)
        payload = builder.build_index_payload(
            class_fqn,
            module=None,
            sections=sections,
            limit=limit,
            method_name=method_name,
            symbol=symbol,
        )
        if payload.get("found"):
            resolved_source = payload.get("class", {}).get("source_path")
            resolved_module = _module_for_source(repo, Path(resolved_source)) if resolved_source else None
            if resolved_module:
                payload.setdefault("class", {})["module"] = resolved_module
            return payload, resolved_module

    external_module_roots = _discover_external_module_roots(repo)
    if external_module_roots:
        primary_root = Path(os.path.abspath(repo)) / module if module else Path(os.path.abspath(repo))
        all_roots = [primary_root.resolve()] + [
            root for root in external_module_roots if root.resolve() != primary_root.resolve()
        ]
        graph, flows = _load_graph_and_flows_for_roots(repo, all_roots)
        builder = ContextBuilder(repo, graph, flows)
        payload = builder.build_index_payload(
            class_fqn,
            module=module,
            sections=sections,
            limit=limit,
            method_name=method_name,
            symbol=symbol,
        )
        if payload.get("found"):
            resolved_source = payload.get("class", {}).get("source_path")
            resolved_module = _module_for_source(repo, Path(resolved_source)) if resolved_source else None
            if resolved_module:
                payload.setdefault("class", {})["module"] = resolved_module
            return payload, resolved_module

    source_jars = _find_source_jars_for_class(class_fqn)
    if not source_jars:
        _fetch_dependency_sources(repo)
        source_jars = _find_source_jars_for_class(class_fqn)
    if source_jars:
        graph, flows = _load_graph_from_source_jars(repo, source_jars)
        builder = ContextBuilder(repo, graph, flows)
        payload = builder.build_index_payload(
            class_fqn,
            module=module,
            sections=sections,
            limit=limit,
            method_name=method_name,
            symbol=symbol,
        )
        if payload.get("found"):
            payload.setdefault("class", {})["module"] = module
            payload.setdefault("class", {})["source_kind"] = "source_jar"
            return payload, module

    return payload, module


def _pick_openai_oauth_method(methods) -> Optional[int]:
    oauth_methods = [
        (idx, method)
        for idx, method in enumerate(methods)
        if method.get("type") == "oauth"
    ]
    for idx, method in oauth_methods:
        if "headless" in (method.get("label") or "").lower():
            return idx
    if oauth_methods:
        return oauth_methods[0][0]
    return None


def _probe_openai_auth_ready(client, repo: str) -> bool:
    from uta.opencode.process import OpenCodeProcess

    process = OpenCodeProcess()
    result = process.run_turn(
        "Reply with only: OK",
        model_id=settings.opencode_model,
        repo_path=repo,
        timeout=120,
    )
    event = {"type": result.type, "result": result.result, "error": result.error, "rate_limit": result.error}

    if event.get("type") == "completed" and "OK" in event.get("result", ""):
        return True
    if event.get("type") == "rate_limited":
        rate_limit = event.get("rate_limit") or {}
        retry_after = rate_limit.get("retry_after_seconds")
        detail = f" retry after {retry_after}s." if retry_after else "."
        raise RuntimeError(f"OpenAI readiness probe was rate limited by the provider/model.{detail}")

    error = event.get("error") or {}
    error_name = error.get("name")
    error_message = ((error.get("data") or {}).get("message") or "").lower()
    if error_name == "ProviderAuthError":
        return False
    if error_name == "APIError" and (
        "invalid_api_key" in error_message
        or "incorrect api key" in error_message
        or "api key is missing" in error_message
    ):
        return False
    if event.get("type") == "timeout":
        raise RuntimeError(
            "OpenAI readiness probe timed out before the model replied. The ChatGPT auth may be fine, "
            "but the confirmation turn was too slow."
        )
    raise RuntimeError(f"OpenAI auth probe failed unexpectedly: {event}")


def _probe_openai_auth_ready_with_retry(client, repo: Optional[str] = None, attempts: int = 3) -> bool:
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return _probe_openai_auth_ready(client, repo or os.getcwd())
        except RuntimeError as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(3 * attempt)
    if last_exc:
        raise last_exc
    return False


def _ensure_model_auth(repo: str) -> None:
    """Check provider auth using a process-based probe. No HTTP server required."""
    provider_id = _provider_from_model(settings.opencode_model) or settings.opencode_provider
    if provider_id != "openai":
        return
    if _probe_openai_auth_ready_with_retry(None, repo):
        return
    raise RuntimeError(
        "OpenCode OpenAI authentication is required for openai/* models. "
        "Run `opencode /connect` to authenticate, then rerun UTA."
    )


@click.group()
def main():
    """Unit Test Agent (uta) - Generate unit tests for legacy Java code."""
    pass


def _task_config_snapshot() -> Dict[str, Any]:
    from uta.opencode.tiered_router import (
        available_provider_candidates,
        parse_provider_chain,
        parse_provider_tokens,
        provider_candidates,
        provider_token_statuses,
    )

    chain = parse_provider_chain(settings.opencode_provider_chain)
    selected = available_provider_candidates(fallback_enabled=False)
    if not selected:
        selected = provider_candidates(fallback_enabled=False)
    selected_candidate = selected[0] if selected else None
    return {
        "opencode_model": settings.opencode_model,
        "opencode_provider": settings.opencode_provider,
        "opencode_provider_chain": [
            {"provider": candidate.provider, "model": candidate.model, "index": candidate.index}
            for candidate in chain
        ],
        "opencode_selected_provider": selected_candidate.provider if selected_candidate else "",
        "opencode_selected_model": selected_candidate.model if selected_candidate else settings.opencode_model,
        "opencode_candidate_index": selected_candidate.index if selected_candidate else None,
        "opencode_provider_tokens": provider_token_statuses(
            chain,
            parse_provider_tokens(settings.opencode_provider_tokens),
        ),
        "coverage_gate": settings.coverage_gate,
        "mutation_gate": settings.mutation_gate,
        "classes_per_agent_run": settings.classes_per_agent_run,
        "planning_timeout_seconds": getattr(settings, "planning_timeout_seconds", None),
        "generation_timeout_seconds": getattr(settings, "generation_timeout_seconds", None),
    }


def _apply_task_opencode_selection(config_snapshot: Dict[str, Any]) -> None:
    selected_model = config_snapshot.get("opencode_selected_model") or ""
    selected_provider = config_snapshot.get("opencode_selected_provider") or _provider_from_model(selected_model)
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
    from uta.engine.languages import default_registry

    return default_registry()


def _resolve_cli_language(repo: str, language: str = "auto", *, class_fqns: Iterable[str] = (), targets: Iterable[str] = ()):
    from uta.engine.languages import AmbiguousLanguageError, UnsupportedLanguageError, resolve_language

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
    from uta.engine.languages import RawTargetSelection, UnsupportedLanguageError

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
    from uta.engine.context import make_context_provider

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
    from uta.engine.source_selection import filter_files, get_all_python_files, get_changed_python_files

    if select_all_files:
        files = [path for path, _count in get_all_python_files(repo, module)]
    else:
        files = filter_files(get_changed_python_files(repo, days, module), max_files=max_files)
    return _normalize_cli_targets("python", files)


def _queue_provider_fallback_resume(task_manager: Any, effective_task_id: int, exc: Exception) -> None:
    from uta.tasks.models import json_loads

    task = task_manager.get_task(effective_task_id)
    snapshot = json_loads(task["config_snapshot_json"]) if task else {}
    provider = (
        getattr(exc, "rate_limit", {}).get("provider_id")
        or snapshot.get("opencode_selected_provider")
        or "provider"
    )
    raw_model = (
        getattr(exc, "rate_limit", {}).get("model_id")
        or snapshot.get("opencode_selected_model")
        or "model"
    )
    model = raw_model if "/" in raw_model else f"{provider}/{raw_model}"
    candidate_index = snapshot.get("opencode_candidate_index")
    if candidate_index is None:
        candidate_index = 0
    task_manager.stop_and_resume_for_provider_fallback(
        effective_task_id,
        provider=provider,
        model=model,
        candidate_index=int(candidate_index),
        reason=getattr(exc, "reason", None) or "rate_limit",
        phase=getattr(exc, "phase", "unknown"),
        retry_after_seconds=getattr(exc, "rate_limit", {}).get("retry_after_seconds"),
    )


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
    task_manager: Any = None,
    effective_task_id: Optional[int] = None,
    task_db: Optional[str] = None,
    ci_context: Optional[Dict[str, Any]] = None,
    verbose: bool = False,
) -> None:
    from uta.output.reporter import Reporter
    from uta.language.python.batch import run_python_batch_generation

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
    try:
        result = run_python_batch_generation(
            repo_path=Path(repo),
            targets=target_refs,
            task_id=effective_task_id,
            task_db_path=Path(task_manager.db_path) if task_manager else (Path(task_db) if task_db and effective_task_id else None),
            model_id=settings.opencode_model,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
        )
    except Exception as exc:
        if task_manager and effective_task_id:
            if exc.__class__.__name__ == "ProviderRateLimitError":
                _queue_provider_fallback_resume(task_manager, effective_task_id, exc)
                console.print(f"[yellow]Provider fallback queued after provider/model error: {exc}[/yellow]")
                return
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
        from uta.ci_plugin.auto_push import AutoPushConflictError, AutoPushPolicyError
        from uta.tasks.autopush import commit_ci_repair_results, passed_result_targets_and_paths, record_existing_repair_commit

        target_ids, commit_paths = passed_result_targets_and_paths(repo, result.results)
        task = task_manager.get_task(effective_task_id)
        branch_name = str(task.get("branch_name") or "")
        try:
            commit_ci_repair_results(
                repo=repo,
                results=result.results,
                manager=task_manager,
                task_id=effective_task_id,
                branch_name=branch_name,
                target_ids=target_ids,
                commit_paths=commit_paths,
                ci_context=ci_context,
            )
        except AutoPushPolicyError as exc:
            if str(exc) == "CI repair auto-push found no test changes to commit":
                if not record_existing_repair_commit(
                    manager=task_manager,
                    task_id=effective_task_id,
                    target_ids=target_ids,
                    results=result.results,
                ):
                    task_manager.record_commit(
                        effective_task_id,
                        class_fqns=target_ids,
                        commit_sha=None,
                        remote_ref=None,
                    )
            else:
                task_manager.record_push_failed(
                    effective_task_id,
                    branch_name=branch_name,
                    message=str(exc),
                    class_fqns=target_ids,
                )
                raise click.ClickException(str(exc)) from exc
        except AutoPushConflictError as exc:
            task_manager.record_push_failed(
                effective_task_id,
                branch_name=branch_name,
                message=str(exc),
                class_fqns=target_ids,
            )
            raise click.ClickException(str(exc)) from exc
        live_paths = task_manager.write_live_status(effective_task_id)
        console.print(f"[dim]Task status: {live_paths['html']}[/dim]")


@main.group("tasks")
def tasks_group():
    """Manage production UTA repo/class tasks."""
    pass


@tasks_group.command("create")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
@click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
@click.option("--module", default=None, help="Target Maven module name")
@click.option("--class-fqn", "class_fqns", multiple=True, help="Class FQN to include. Repeat for multiple classes.")
@click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
@click.option("--all", "select_all", is_flag=True, help="Select all production classes during execution")
@click.option("--priority", default=100, show_default=True, type=int, help="Lower number runs first")
@click.option("--branch-name", default=None, help="Explicit generation branch. Defaults to same-repo active branch reuse.")
@click.option("--new-branch", is_flag=True, help="Force a new branch instead of reusing the active repo branch")
@click.option("--base-ref", default="origin/master", show_default=True, help="Base ref recorded for branch reuse")
@click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
@click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_create(repo, language, module, class_fqns, targets, select_all, priority, branch_name, new_branch, base_ref, coverage_gate, mutation_gate, task_db):
    from uta.tasks.manager import TaskManager

    manager = TaskManager(task_db)
    decision = _resolve_cli_language(repo, language, class_fqns=class_fqns, targets=targets)
    if decision.language == "java" and not targets:
        task_id = manager.create_task(
            repo_path=repo,
            module=module,
            class_fqns=class_fqns,
            select_all=select_all,
            priority=priority,
            branch_name=branch_name,
            new_branch=new_branch,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            config_snapshot=_task_config_snapshot(),
            budget_snapshot=_task_budget_snapshot(),
        )
    else:
        if class_fqns and decision.language != "java":
            raise click.ClickException("--class-fqn can only be used with Java targets")
        raw_targets = list(targets or ())
        if decision.language == "java":
            raw_targets.extend({"class_fqn": class_fqn} for class_fqn in class_fqns)
        target_refs = _normalize_cli_targets(decision.language, raw_targets)
        task_id = manager.create_task_targets(
            repo_path=repo,
            module=module,
            targets=target_refs,
            select_all=select_all,
            priority=priority,
            branch_name=branch_name,
            new_branch=new_branch,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            config_snapshot=_task_config_snapshot(),
            budget_snapshot=_task_budget_snapshot(),
            language=decision.language,
        )
    task = manager.get_task(task_id)
    manager.write_live_status(task_id)
    console.print(f"[green]Created task {task_id}[/green] branch={task['branch_name']} db={manager.db_path}")


@tasks_group.command("create-manifest")
@click.option("--manifest", "manifest_path", required=True, type=click.Path(exists=True, dir_okay=False), help="JSON manifest with repo task definitions")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_create_manifest(manifest_path, task_db):
    """Create tasks from a JSON manifest.

    Accepted shapes:
    {"tasks": [{"repo": "/repo", "module": "biz", "class_fqns": [...], "all": false, "priority": 100}]}
    or a top-level list of the same task objects.
    """
    from uta.tasks.manager import TaskManager

    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    items = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise click.ClickException("Manifest must be a JSON list or an object with a 'tasks' list")
    manager = TaskManager(task_db)
    created = []
    for item in items:
        if not isinstance(item, dict) or not item.get("repo"):
            raise click.ClickException("Each manifest task must include a repo path")
        class_fqns = item.get("class_fqns") or item.get("classes") or []
        targets = _manifest_targets(item)
        decision = _resolve_cli_language(
            item["repo"],
            item.get("language", "auto"),
            class_fqns=class_fqns,
            targets=[target if isinstance(target, str) else (target.get("target") or target.get("target_id") or target.get("sourcePath") or target.get("source_path") or "") for target in targets],
        )
        if decision.language == "java" and not targets:
            task_id = manager.create_task(
                repo_path=item["repo"],
                module=item.get("module"),
                class_fqns=class_fqns,
                select_all=bool(item.get("all") or item.get("select_all")),
                priority=int(item.get("priority", 100)),
                branch_name=item.get("branch_name"),
                new_branch=bool(item.get("new_branch")),
                base_ref=item.get("base_ref", "origin/master"),
                coverage_gate=item.get("coverage_gate", settings.coverage_gate),
                mutation_gate=item.get("mutation_gate", settings.mutation_gate),
                config_snapshot=_task_config_snapshot(),
                budget_snapshot=_task_budget_snapshot(),
                estimate_snapshot=item.get("estimate"),
            )
        else:
            if class_fqns and decision.language != "java":
                raise click.ClickException("Manifest class_fqns can only be used with Java targets")
            raw_targets = list(targets)
            if decision.language == "java":
                raw_targets.extend({"class_fqn": class_fqn} for class_fqn in class_fqns)
            task_id = manager.create_task_targets(
                repo_path=item["repo"],
                module=item.get("module"),
                targets=_normalize_cli_targets(decision.language, raw_targets),
                select_all=bool(item.get("all") or item.get("select_all")),
                priority=int(item.get("priority", 100)),
                branch_name=item.get("branch_name"),
                new_branch=bool(item.get("new_branch")),
                base_ref=item.get("base_ref", "origin/master"),
                coverage_gate=item.get("coverage_gate", settings.coverage_gate),
                mutation_gate=item.get("mutation_gate", settings.mutation_gate),
                config_snapshot=_task_config_snapshot(),
                budget_snapshot=_task_budget_snapshot(),
                estimate_snapshot=item.get("estimate"),
                language=decision.language,
            )
        created.append(task_id)
        manager.write_live_status(task_id)
    console.print(f"[green]Created {len(created)} task(s): {', '.join(map(str, created))}[/green]")


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
@click.option("--opencode-db", default=None, type=click.Path(dir_okay=False), help="OpenCode SQLite DB path for token verification")
@click.option("--json", "as_json", is_flag=True, help="Emit the task summary as JSON")
@click.option("--detail", is_flag=True, help="Show per-class token verification rows")
def task_summary(task_id, task_db, opencode_db, as_json, detail):
    from uta.tasks.manager import TaskManager

    manager = TaskManager(task_db)
    summary = manager.build_task_summary(task_id, opencode_db_path=opencode_db, recalc_project_coverage=True)
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

    token_table = Table(title="Token And Cost Verification")
    token_table.add_column("Source", style="cyan")
    token_table.add_column("Input", justify="right")
    token_table.add_column("Cache Read", justify="right")
    token_table.add_column("Output", justify="right")
    token_table.add_column("Reasoning", justify="right")
    token_table.add_column("Total", justify="right")
    token_table.add_column("Cost USD", justify="right")
    for label, row in (("Task DB", tokens["task_db"]), ("Verified", tokens["verified"])):
        token_table.add_row(
            label,
            _fmt_int(row.get("input")),
            _fmt_int(row.get("cache_read")),
            _fmt_int(row.get("output")),
            _fmt_int(row.get("reasoning")),
            _fmt_int(row.get("total")),
            _fmt_optional_float(row.get("cost_usd"), decimals=4),
        )
    delta = tokens["comparison"]["delta"]
    token_table.add_row(
        "Delta",
        _fmt_int(delta.get("input")),
        _fmt_int(delta.get("cache_read")),
        _fmt_int(delta.get("output")),
        _fmt_int(delta.get("reasoning")),
        _fmt_int(delta.get("total")),
        _fmt_optional_float(
            float(tokens["verified"].get("cost_usd") or 0.0) - float(tokens["task_db"].get("cost_usd") or 0.0),
            decimals=4,
        ),
    )
    console.print(token_table)

    verification = Table(title="Verification Health")
    verification.add_column("Metric", style="cyan")
    verification.add_column("Value", justify="right")
    verification.add_row("OpenCode DB", str(tokens["comparison"]["opencode_db_path"]))
    verification.add_row("Classes From Sessions", _fmt_int(tokens["comparison"]["classes_from_sessions"]))
    verification.add_row("Classes From Recovery", _fmt_int(tokens["comparison"]["classes_from_recovery"]))
    verification.add_row("Classes Missing", _fmt_int(tokens["comparison"]["classes_missing"]))
    verification.add_row("Classes Mismatched", _fmt_int(tokens["comparison"]["classes_mismatched"]))
    console.print(verification)

    if detail:
        per_class = Table(title="Per-Class Token Verification")
        per_class.add_column("Class")
        per_class.add_column("Status")
        per_class.add_column("Source")
        per_class.add_column("DB Total", justify="right")
        per_class.add_column("Verified Total", justify="right")
        per_class.add_column("Sessions")
        for row in tokens["per_class_verification"]:
            per_class.add_row(
                str(row["class_fqn"]),
                str(row["status"]),
                str(row["source"]),
                _fmt_int(row["db_total_tokens"]),
                _fmt_int(row["verified_total_tokens"]),
                ", ".join(row["session_ids"]),
            )
        console.print(per_class)


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


@tasks_group.command("start")
@click.argument("task_id", required=False, type=int)
@click.option("--next", "start_next", is_flag=True, help="Acquire and execute the next queued task")
@click.option("--include-failed", is_flag=True, help="Allow --next to reacquire FAILED repo tasks")
@click.option("--execute", is_flag=True, help="Run the selected task immediately in this process")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_start(task_id, start_next, include_failed, execute, task_db):
    from uta.tasks.manager import TaskManager
    from uta.tasks.scheduler import TaskScheduler

    manager = TaskManager(task_db)
    selected_id = task_id
    if start_next:
        execute = True
        task = TaskScheduler(str(manager.db_path)).acquire_next(include_failed=include_failed)
        if not task:
            console.print("[yellow]No queued task available[/yellow]")
            return
        selected_id = int(task["id"])
    if selected_id is None:
        raise click.ClickException("Provide TASK_ID or --next")
    if not start_next:
        manager.start_task(selected_id)
    console.print(f"[green]Task {selected_id} {'acquired' if start_next else 'queued'}[/green]")
    if execute:
        cmd = [sys.executable, "-m", "uta.cli", "run", "--production", "--task-id", str(selected_id), "--task-db", str(manager.db_path)]
        raise SystemExit(subprocess.call(cmd))


@tasks_group.command("daemon")
@click.option("--interval", "--poll-interval", default=10.0, show_default=True, type=float, help="Polling interval in seconds")
@click.option("--heartbeat-interval", default=15.0, show_default=True, type=float, help="Heartbeat interval in seconds")
@click.option("--once", is_flag=True, help="Poll once and exit")
@click.option("--allow-same-repo-concurrency", is_flag=True, help="Allow multiple active tasks for one repo")
@click.option("--include-failed", is_flag=True, help="Allow daemon to retry FAILED repo tasks")
@click.option("--max-parallel", default=None, type=int, help="Max concurrent repo tasks (default: UTA_MAX_PARALLEL_REPOS or 1)")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_daemon(interval, heartbeat_interval, once, allow_same_repo_concurrency, include_failed, max_parallel, task_db):
    from uta.tasks.scheduler import TaskScheduler
    from uta.tasks.manager import TaskManager
    from uta.config import settings

    from uta.tasks.run_error_classifier import classify_run_error

    scheduler = TaskScheduler(task_db)
    manager = TaskManager(str(scheduler.db.path))
    max_slots = max_parallel if max_parallel is not None else int(settings.max_parallel_repos)
    console.print(f"[green]UTA task daemon using {scheduler.db.path} (max-parallel={max_slots})[/green]")

    # pool: task_id → {"proc": Popen, "last_heartbeat": float, "retry_count": int}
    pool: dict = {}
    # pending_retries: task_id → {"retry_after": monotonic, "retry_count": int}
    # Tasks here are QUEUED in the DB but held back until the backoff elapses.
    pending_retries: dict = {}
    _last_idle_heartbeat_at: float = 0.0

    _TRANSIENT_BACKOFF_SECONDS = [30, 60, 120]  # backoff per attempt index
    _TRANSIENT_MAX_RETRIES = 3

    def _launch(task_id: int, retry_count: int = 0) -> None:
        cmd = [sys.executable, "-m", "uta.cli", "run", "--production", "--task-id", str(task_id), "--task-db", str(scheduler.db.path)]
        proc = subprocess.Popen(cmd)
        pool[task_id] = {"proc": proc, "last_heartbeat": 0.0, "retry_count": retry_count}

    def _queue_retry(tid: int, retry_count: int, *, backoff_seconds: float = 0.0) -> None:
        """Re-queue a task in the DB and park it in pending_retries until backoff elapses."""
        manager.db.update_repo_task(
            tid,
            status="QUEUED",
            current_stage="queued_retry",
            current_detail=f"retry {retry_count} after {backoff_seconds:.0f}s backoff",
        )
        manager.db.add_event(tid, None, "task_retry_queued", f"Retry {retry_count} scheduled after {backoff_seconds:.0f}s", stage="queued_retry")
        pending_retries[tid] = {"retry_after": time.monotonic() + backoff_seconds, "retry_count": retry_count}

    def _reap_finished() -> None:
        for tid in list(pool.keys()):
            proc = pool[tid]["proc"]
            rc = proc.poll()
            if rc is None:
                continue
            return_code = int(rc)
            retry_count = pool[tid].get("retry_count", 0)
            del pool[tid]

            if return_code != 0:
                task_row = manager.db.get_repo_task(tid)
                error_msg = (task_row["last_error"] or "") if task_row else ""
                if not error_msg:
                    error_msg = f"uta run exited with status {return_code}"
                policy = classify_run_error(error_msg)
                logger.info("[%s] Task %d rc=%d retries=%d: %s", policy, tid, return_code, retry_count, error_msg[:120])

                if policy == "budget_abort":
                    manager.mark_budget_exceeded(tid, error_msg)
                    console.print(f"[red]Task {tid} BUDGET_EXCEEDED — not retrying[/red]")

                elif policy == "transient" and retry_count < _TRANSIENT_MAX_RETRIES:
                    backoff = _TRANSIENT_BACKOFF_SECONDS[min(retry_count, len(_TRANSIENT_BACKOFF_SECONDS) - 1)]
                    _queue_retry(tid, retry_count + 1, backoff_seconds=backoff)
                    console.print(f"[yellow]Task {tid} transient error — retry {retry_count + 1}/{_TRANSIENT_MAX_RETRIES} after {backoff}s[/yellow]")

                elif policy == "internal_retry_once" and retry_count < 1:
                    _queue_retry(tid, 1, backoff_seconds=0)
                    console.print(f"[yellow]Task {tid} internal error — retry 1/1 (immediate)[/yellow]")

                else:
                    manager.mark_failed(tid, error_msg, stage="runner_exit")
                    console.print(f"[dim]Task {tid} failed (policy={policy}, retries={retry_count})[/dim]")
            else:
                console.print(f"[dim]Task {tid} finished ok[/dim]")

            scheduler.heartbeat(repo_task_id=tid, status="IDLE", message=f"task exited {return_code}", config_snapshot=_task_config_snapshot())

    def _promote_ready_retries() -> None:
        """Move pending retries whose backoff has elapsed back into the pool."""
        now = time.monotonic()
        for tid in list(pending_retries.keys()):
            entry = pending_retries[tid]
            if now >= entry["retry_after"] and len(pool) < max_slots:
                del pending_retries[tid]
                console.print(f"[yellow]Launching retry for task {tid} (attempt {entry['retry_count']})[/yellow]")
                _launch(tid, retry_count=entry["retry_count"])

    def _batch_cap_reached() -> bool:
        cap = settings.batch_cap_usd
        if cap is None:
            return False
        total = scheduler.db.total_provider_cost_usd()
        if total >= cap:
            logger.warning("[BATCH CAP REACHED] spent $%.4f >= cap $%.4f — pausing dequeue", total, cap)
            return True
        return False

    while True:
        _reap_finished()
        _promote_ready_retries()

        # Fill empty slots from the scheduler queue (one cap check per tick, not per slot)
        if not _batch_cap_reached():
            while len(pool) < max_slots:
                task = scheduler.acquire_next(
                    allow_same_repo_concurrency=allow_same_repo_concurrency,
                    include_failed=include_failed,
                )
                if not task:
                    break
                task_id = int(task["id"])
                console.print(f"[blue]Starting task {task_id} (slot {len(pool)+1}/{max_slots})[/blue]")
                _launch(task_id)

        # Heartbeat all running slots
        now = time.monotonic()
        for tid, slot in pool.items():
            if now - slot["last_heartbeat"] >= max(heartbeat_interval, 1.0):
                scheduler.heartbeat(repo_task_id=tid, status="RUNNING", message="task subprocess running", config_snapshot=_task_config_snapshot())
                slot["last_heartbeat"] = now

        if not pool and not pending_retries:
            if once:
                console.print("[yellow]No queued task available[/yellow]")
                return
            if now - _last_idle_heartbeat_at >= max(heartbeat_interval, 1.0):
                scheduler.heartbeat(repo_task_id=None, status="IDLE", message="waiting for task", config_snapshot=_task_config_snapshot())
                _last_idle_heartbeat_at = now

        time.sleep(min(max(interval, 1.0), max(heartbeat_interval, 1.0)))

        if once and not pool and not pending_retries:
            return


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
                msg = (evt["message"] or "")[:60] if evt else ""
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


@tasks_group.command("stop")
@click.argument("task_id", type=int)
@click.option("--reason", default=None)
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_stop(task_id, reason, task_db):
    from uta.tasks.manager import TaskManager

    TaskManager(task_db).stop_task(task_id, reason=reason)
    console.print(f"[yellow]Stop requested for task {task_id}[/yellow]")


@tasks_group.command("resume")
@click.argument("task_id", type=int)
@click.option("--force-rerun-failed", is_flag=True, help="Requeue failed child rows as well as unfinished rows")
@click.option("--force-rerun-all", is_flag=True, help="Requeue every child row, including passing rows")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_resume(task_id, force_rerun_failed, force_rerun_all, task_db):
    from uta.tasks.manager import TaskManager

    TaskManager(task_db).resume_task(
        task_id,
        force_rerun_failed=force_rerun_failed,
        force_rerun_all=force_rerun_all,
    )
    console.print(f"[green]Task {task_id} queued for resume[/green]")


@tasks_group.command("cancel")
@click.argument("task_id", type=int)
@click.option("--reason", default=None)
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_cancel(task_id, reason, task_db):
    from uta.tasks.manager import TaskManager

    TaskManager(task_db).cancel_task(task_id, reason=reason)
    console.print(f"[yellow]Task {task_id} cancelled[/yellow]")


@tasks_group.command("unblock")
@click.argument("task_id", type=int)
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_unblock(task_id, task_db):
    """Reset a POISONED or BUDGET_EXCEEDED task to QUEUED."""
    from uta.tasks.manager import TaskManager

    TaskManager(task_db).unblock(task_id)
    console.print(f"[green]Task {task_id} unblocked and queued[/green]")


@tasks_group.command("enqueue")
@click.argument("git_url")
@click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
@click.option("--module", default=None, help="Target Maven module")
@click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
@click.option("--all", "select_all", is_flag=True, help="Select all targets during execution")
@click.option("--branch", default=None, help="Git branch to check out")
@click.option("--priority", default=100, type=int, show_default=True, help="Task priority (lower = sooner)")
@click.option("--hard-cap-usd", default=None, type=float, help="Abort task when spend exceeds this USD amount")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_enqueue(git_url, language, module, targets, select_all, branch, priority, hard_cap_usd, task_db):
    """Clone (or update) a repo and add it to the task queue."""
    import subprocess as _sp
    import re as _re
    from uta.tasks.manager import TaskManager
    from uta.config import settings

    slug = _re.sub(r"[^A-Za-z0-9_.-]+", "-", git_url.rstrip("/").split("/")[-1]).strip("-") or "repo"
    clone_root = settings.clone_root
    dest = Path(clone_root).expanduser() / slug

    if dest.exists():
        console.print(f"[dim]Fetching updates: {dest}[/dim]")
        _sp.run(["git", "-C", str(dest), "fetch", "--all"], check=True)
    else:
        console.print(f"[dim]Cloning {git_url} → {dest}[/dim]")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _sp.run(["git", "clone", git_url, str(dest)], check=True)

    if branch:
        _sp.run(["git", "-C", str(dest), "checkout", branch], check=True)

    manager = TaskManager(task_db)
    # Skip if a RUNNING task for this path already exists
    with manager.db.connect() as _conn:
        running = _conn.execute(
            "SELECT id FROM repo_tasks WHERE repo_path=? AND status='RUNNING' LIMIT 1",
            (str(dest),),
        ).fetchone()
    if running:
        console.print(f"[yellow]Task already RUNNING for {dest} (id={running['id']}); skipping enqueue[/yellow]")
        return

    decision = _resolve_cli_language(str(dest), language, targets=targets)
    if decision.language == "java" and not targets:
        task_id = manager.create_task(
            repo_path=str(dest),
            module=module,
            select_all=select_all,
            priority=priority,
            branch_name=branch,
            hard_cap_usd=hard_cap_usd,
        )
    else:
        task_id = manager.create_task_targets(
            repo_path=str(dest),
            module=module,
            targets=_normalize_cli_targets(decision.language, targets),
            select_all=select_all,
            priority=priority,
            branch_name=branch,
            hard_cap_usd=hard_cap_usd,
            language=decision.language,
        )
    manager.start_task(task_id)
    console.print(f"[green]Enqueued task {task_id} for {dest}[/green]")


@tasks_group.command("reprioritize")
@click.argument("task_id", type=int)
@click.argument("priority_arg", required=False, type=int)
@click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_reprioritize(task_id, priority_arg, priority_opt, task_db):
    from uta.tasks.manager import TaskManager

    priority = priority_opt if priority_opt is not None else priority_arg
    if priority is None:
        raise click.ClickException("Provide PRIORITY or --priority")
    TaskManager(task_db).reprioritize_task(task_id, priority)
    console.print(f"[green]Task {task_id} priority={priority}[/green]")


@tasks_group.command("reprioritize-class")
@click.argument("class_task_id", type=int)
@click.argument("priority_arg", required=False, type=int)
@click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_reprioritize_class(class_task_id, priority_arg, priority_opt, task_db):
    from uta.tasks.manager import TaskManager

    priority = priority_opt if priority_opt is not None else priority_arg
    if priority is None:
        raise click.ClickException("Provide PRIORITY or --priority")
    TaskManager(task_db).reprioritize_class(class_task_id, priority)
    console.print(f"[green]Class task {class_task_id} priority={priority}[/green]")


@tasks_group.command("reprioritize-target")
@click.argument("target_task_id", type=int)
@click.argument("priority_arg", required=False, type=int)
@click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
@click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
def task_reprioritize_target(target_task_id, priority_arg, priority_opt, task_db):
    from uta.tasks.manager import TaskManager

    priority = priority_opt if priority_opt is not None else priority_arg
    if priority is None:
        raise click.ClickException("Provide PRIORITY or --priority")
    TaskManager(task_db).reprioritize_class(target_task_id, priority)
    console.print(f"[green]Target task {target_task_id} priority={priority}[/green]")


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
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def run(repo, language, module, days, max_files, select_all_files, explicit_class_fqns, explicit_targets, coverage_gate, mutation_gate, classes_per_run, fix_code, stop_after_stage, resume, branch_name, existing_branch, preserve_branch, production, task_id, resume_task, task_db, verbose):
    """Run the full test generation pipeline."""
    from uta.language.java.batch import JavaBatchGenerationRequest, run_java_batch_generation
    from uta.opencode.config import generate_opencode_config
    from uta.opencode.client import OpenCodeClient
    from uta.output.reporter import Reporter

    if classes_per_run < 1:
        raise click.BadParameter("--classes-per-run must be at least 1")

    task_manager = None
    effective_task_id = task_id or resume_task
    quality_mode = "class_batch"
    quality_gate_backend = "builtin"
    quality_gate_command = ""
    ci_context = {}
    if production or effective_task_id:
        from uta.tasks.manager import TaskManager
        from uta.tasks.models import json_loads

        task_manager = TaskManager(task_db)
        if effective_task_id:
            task = task_manager.get_task(effective_task_id)
            task_config_snapshot = json_loads(task.get("config_snapshot_json") or "{}")
            _apply_task_opencode_selection(task_config_snapshot)
            selection = json_loads(task.get("selection_json"))
            ci_context = json_loads(task.get("ci_context_json") or "{}")
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
            quality_mode, coverage_gate, mutation_gate = _task_quality_options(
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
            create_decision = _resolve_cli_language(repo, language, class_fqns=explicit_class_fqns, targets=explicit_targets)
            if create_decision.language == "python":
                python_select_all = select_all_files
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
                    config_snapshot=_task_config_snapshot(),
                    budget_snapshot=_task_budget_snapshot(),
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
                    config_snapshot=_task_config_snapshot(),
                    budget_snapshot=_task_budget_snapshot(),
                )
            task_manager.mark_running(effective_task_id, stage="startup", detail="uta run started")
            task = task_manager.get_task(effective_task_id)
            existing_branch = existing_branch or task.get("branch_name")
            branch_name = task.get("branch_name") or branch_name
            preserve_branch = True

    if not repo:
        raise click.ClickException("--repo is required unless --task-id/--resume-task is provided")

    repo = os.path.abspath(repo)
    decision = _resolve_cli_language(repo, language, class_fqns=explicit_class_fqns, targets=explicit_targets)
    if decision.language == "python":
        return _run_python_batch_cli(
            repo=repo,
            explicit_targets=explicit_targets,
            max_files=max_files,
            days=days,
            module=module,
            select_all_files=select_all_files,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            task_manager=task_manager,
            effective_task_id=effective_task_id,
            task_db=task_db,
            ci_context=ci_context,
            verbose=verbose,
        )
    if explicit_targets and not explicit_class_fqns:
        explicit_class_fqns = tuple(explicit_targets)
    effective_branch_name = existing_branch or branch_name
    preserve_branch = preserve_branch or bool(existing_branch)
    run_log_path = _configure_run_logging(repo, verbose)
    run_started_at = time.time()
    console.print(f"[bold green]Starting UTA on {repo}[/bold green]")
    console.print(f"[dim]Run log: {run_log_path}[/dim]")

    # 1. Setup OpenCode
    generate_opencode_config(repo)
    client = OpenCodeClient(repo_path=repo)
    auth_started = time.perf_counter()
    try:
        _ensure_model_auth(repo)
    except Exception as e:
        logger.exception("OpenCode auth check failed during run startup")
        console.print(f"[bold red]OpenCode auth error: {e}[/bold red]")
    auth_seconds = time.perf_counter() - auth_started
    session_id = client.create_session(
        model_id=settings.opencode_model,
        provider_id=_provider_from_model(settings.opencode_model) or settings.opencode_provider
    )

    console.print("[blue]Executing pipeline...[/blue]")
    final_error = None
    task_stopped = False
    try:
        java_result = run_java_batch_generation(
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
                ci_context=ci_context,
                session_id=session_id,
                session_ids=[session_id] if session_id else [],
                run_log_path=run_log_path,
                production=bool(production or effective_task_id),
                language_decision=decision.as_dict(),
                phase_timings={"auth_probe_seconds": auth_seconds},
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
        elif e.__class__.__name__ == "ProviderRateLimitError" and task_manager and effective_task_id:
            _queue_provider_fallback_resume(task_manager, effective_task_id, e)
            console.print(f"[yellow]Provider fallback queued after provider/model error: {e}[/yellow]")
            results = {}
            final_error = None
            task_stopped = True
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
        report_path = _latest_report_or_none(repo)
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


@main.command("resume-gates")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the Java repository")
@click.option("--report", "report_path", default=None, type=click.Path(exists=True, dir_okay=False), help="Existing UTA summary report to continue from. Defaults to the latest summary_*.json in .uta_reports.")
@click.option("--class-fqn", default=None, help="Class FQN to continue from the saved report")
@click.option("--module", default=None, help="Target Maven module name. Defaults to the report metadata.")
@click.option("--model", "resume_model", default=None, help="OpenCode model to use for the resumed gates. Defaults to the model recorded in the report.")
@click.option("--provider", "resume_provider", default=None, help="OpenCode provider to use for the resumed gates. Defaults to the provider inferred from --model or the report model.")
@click.option("--coverage-gate", default=None, type=int, help="Target coverage percentage. Defaults to the current UTA setting.")
@click.option("--mutation-gate", default=None, type=int, help="Target mutation score. Defaults to the current UTA setting.")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def resume_gates(repo, report_path, class_fqn, module, resume_model, resume_provider, coverage_gate, mutation_gate, verbose):
    """Continue a failed run from fresh coverage evaluation, then proceed into mutation."""
    from uta.language.java.context_builder import ContextBuilder
    from uta.graph.nodes import (
        _capture_session_retrospect,
        _capture_session_token_usage,
        _run_focused_mutation_fix_round,
        _run_mutation_test_fix_loop,
        _run_generation_test_gate,
        _mutation_enhancement_attempts,
        _should_run_mutation,
        _status_with_mutation_gate,
        parse_context,
        run_compile_fix_loop,
        run_coverage_fix_loop,
    )
    from uta.opencode.config import generate_opencode_config
    from uta.opencode.client import OpenCodeClient
    from uta.output.reporter import Reporter
    from uta.maven.jacoco import run_test_with_jacoco, find_jacoco_report, parse_jacoco_report
    from uta.maven.pitest import (
        run_pitest,
        find_latest_pitest_report,
        compute_mutation_stats,
        parse_pitest_report,
        parse_pitest_green_suite_failure,
    )

    repo = os.path.abspath(repo)
    run_log_path = _configure_run_logging(repo, verbose)
    resumed_started_at = time.time()
    console.print(f"[bold green]Resuming UTA gates on {repo}[/bold green]")
    console.print(f"[dim]Run log: {run_log_path}[/dim]")

    report_file = Path(report_path) if report_path else _latest_summary_report(repo)
    report = _load_report(report_file)
    class_fqn, original_result = _pick_report_result(report, class_fqn)
    report_module = module or (report.get("metadata") or {}).get("module")
    report_model = _report_primary_model(report)
    effective_model = resume_model or report_model or settings.opencode_model
    effective_provider = resume_provider or _provider_from_model(effective_model) or settings.opencode_provider
    coverage_gate = int(coverage_gate if coverage_gate is not None else settings.coverage_gate)
    mutation_gate = int(mutation_gate if mutation_gate is not None else settings.mutation_gate)

    previous_model = settings.opencode_model
    previous_provider = settings.opencode_provider
    settings.opencode_model = effective_model
    settings.opencode_provider = effective_provider

    test_file_rel = original_result.get("test_file_path")
    if not test_file_rel:
        raise click.ClickException("Saved report does not contain a test_file_path for the target class")
    test_file_abs = Path(repo) / test_file_rel
    if not test_file_abs.exists():
        raise click.ClickException(f"Saved test file is missing: {test_file_abs}")

    generate_opencode_config(repo)
    client = OpenCodeClient(repo_path=repo)
    auth_seconds = 0.0
    auth_started = time.perf_counter()
    try:
        _ensure_model_auth(repo)
    except Exception as e:
        logger.exception("OpenCode auth check failed during resume-gates startup")
        console.print(f"[bold red]OpenCode auth error: {e}[/bold red]")
    auth_seconds = time.perf_counter() - auth_started
    try:

        parse_state = {
            "repo_path": repo,
            "module": report_module,
            "days": 0,
            "max_files": 1,
            "explicit_class_fqns": [class_fqn],
            "candidates": [class_fqn],
            "phase_timings": {},
        }
        parsed = parse_context(parse_state)
        graph = parsed["graph"]
        flows = parsed["flows"]
        parse_seconds = float((parsed.get("phase_timings") or {}).get("parse_context_seconds", 0.0) or 0.0)
        ctx_builder = ContextBuilder(repo, graph, flows)
        target_paths = ctx_builder.export_target_context_files(
            class_fqn,
            module=report_module,
            test_file_rel=test_file_rel,
        )
        source_rel = ctx_builder.get_class_source_path(class_fqn)
        source_file_abs = Path(source_rel)
        if not source_file_abs.is_absolute():
            source_file_abs = Path(repo) / source_rel

        session_ids = _dedupe_session_ids(
            list((report.get("token_usage") or {}).get("session_ids") or [])
            + list(original_result.get("session_ids") or [])
        )

        test_class_name = f"{class_fqn.split('.')[-1]}Test"
        line_cov = float(original_result.get("coverage", 0.0) or 0.0)
        test_output = ""
        compile_seconds = 0.0

        compile_started = time.perf_counter()
        compile_ok, compile_fix_session_id = run_compile_fix_loop(
            repo_path=repo,
            module=report_module,
            batch=[class_fqn],
            generation_session_id=session_ids[-1] if session_ids else "",
            client=client,
            maven_module_flag=f" -pl {report_module} -am" if report_module else "",
            target_context_paths={class_fqn: target_paths},
            max_fix_attempts=3,
        )
        compile_seconds = time.perf_counter() - compile_started
        if compile_fix_session_id:
            session_ids = _dedupe_session_ids(session_ids + [compile_fix_session_id])

        if not compile_ok:
            raise click.ClickException("Resume preflight compile-fix failed; current test file still does not compile")

        generation_test_ok, generation_test_seconds, generation_test_session_id = _run_generation_test_gate(
            state={},
            repo_path=repo,
            module=report_module,
            batch=[class_fqn],
            generation_session_id=session_ids[-1] if session_ids else "",
            client=client,
            maven_module_flag=f" -pl {report_module} -am" if report_module else "",
            max_fix_attempts=3,
        )
        if generation_test_session_id:
            session_ids = _dedupe_session_ids(session_ids + [generation_test_session_id])
        if not generation_test_ok:
            raise click.ClickException("Resume preflight test-fix failed; targeted test gate still does not pass")

        test_started = time.perf_counter()
        test_ok, test_output = run_test_with_jacoco(repo, test_class_name, report_module)
        jacoco_path = find_jacoco_report(repo, report_module)
        if jacoco_path:
            line_cov = parse_jacoco_report(jacoco_path, class_fqn).get("line", line_cov)
        coverage_fix_seconds = 0.0
        if test_ok and line_cov < coverage_gate:
            coverage_started = time.perf_counter()
            coverage_ok, line_cov, coverage_output, coverage_session_ids = run_coverage_fix_loop(
                repo_path=repo,
                module=report_module,
                class_fqn=class_fqn,
                session_id=session_ids[-1] if session_ids else "",
                client=client,
                test_class_name=test_class_name,
                test_file_abs=test_file_abs,
                source_file_abs=source_file_abs,
                coverage_gate=coverage_gate,
                current_coverage=line_cov,
                maven_module_flag=f" -pl {report_module} -am" if report_module else "",
                target_context_abs=target_paths.get("context_abs", ""),
                target_symbols_abs=target_paths.get("symbols_abs", ""),
            )
            coverage_fix_seconds = time.perf_counter() - coverage_started
            if coverage_output:
                test_output = coverage_output
            session_ids = _dedupe_session_ids(session_ids + coverage_session_ids)
        test_seconds = time.perf_counter() - test_started

        mutation_score = 0.0
        surviving_mutants = 0
        mutation_stats: Dict[str, Any] = {}
        mutation_seconds = 0.0
        if _should_run_mutation(test_ok, mutation_gate):
            mutation_started = time.perf_counter()
            test_class_fqn = f"{'.'.join(class_fqn.split('.')[:-1])}.{test_class_name}"
            max_mutation_attempts = _mutation_enhancement_attempts()
            for attempt in range(1, max_mutation_attempts + 1):
                pitest_ok, pitest_output = run_pitest(repo, class_fqn, test_class_fqn, report_module)
                if not pitest_ok:
                    green_suite_failure = parse_pitest_green_suite_failure(pitest_output)
                    if green_suite_failure:
                        mutation_test_fix_ok, mutation_test_output, mutation_test_fix_session_id = _run_mutation_test_fix_loop(
                            repo_path=repo,
                            module=report_module,
                            class_fqn=class_fqn,
                            test_class_name=test_class_name,
                            current_output=pitest_output,
                            client=client,
                            maven_module_flag=f" -pl {report_module} -am" if report_module else "",
                        )
                        if mutation_test_fix_session_id:
                            session_ids = _dedupe_session_ids(session_ids + [mutation_test_fix_session_id])
                        if mutation_test_fix_ok:
                            if mutation_test_output:
                                test_output = mutation_test_output
                            continue
                        if mutation_test_output:
                            test_output = mutation_test_output
                            pitest_output = mutation_test_output
                    test_output = pitest_output
                    break
                pit_report = find_latest_pitest_report(repo, report_module)
                if not pit_report:
                    break
                mutation_stats = compute_mutation_stats(pit_report, class_fqn)
                mutation_score = float(mutation_stats.get("score", 0.0) or 0.0)
                surviving_mutants = int(mutation_stats.get("survived", 0) or 0)
                survivors = parse_pitest_report(pit_report, class_fqn)
                if mutation_score >= mutation_gate or not survivors:
                    break
                if attempt < max_mutation_attempts:
                    for followup_round in range(1, 3):
                        mutation_fix_result = _run_focused_mutation_fix_round(
                            repo_path=repo,
                            module=report_module,
                            class_fqn=class_fqn,
                            session_client=client,
                            source_file_abs=source_file_abs,
                            test_file_abs=test_file_abs,
                            target_context_abs=target_paths.get("context_abs", ""),
                            target_symbols_abs=target_paths.get("symbols_abs", ""),
                            current_coverage=line_cov,
                            mutation_gate_score=mutation_gate,
                            attempt=attempt,
                            mutation_score=mutation_score,
                            mutation_stats=mutation_stats,
                            report_path=pit_report,
                        )
                        mutation_fix_session_id = mutation_fix_result.get("session_id")
                        session_ids = _dedupe_session_ids(session_ids + [mutation_fix_session_id])
                        if mutation_fix_result.get("patched", False):
                            break
                        logger.warning(
                            "[%s] Resume mutation round %d follow-up %d produced diagnosis only (no patch); "
                            "escalating to the next ranked family",
                            class_fqn,
                            attempt,
                            followup_round,
                        )
                        if followup_round >= 2:
                            logger.warning(
                                "[%s] Resume mutation round %d exhausted diagnosis-only escalations for this PIT snapshot",
                                class_fqn,
                                attempt,
                            )
            mutation_seconds = time.perf_counter() - mutation_started

        status = _status_with_mutation_gate(
            test_ok=test_ok,
            line_cov=line_cov,
            coverage_gate=coverage_gate,
            mutation_gate_score=mutation_gate,
            mutation_score=mutation_score,
        )

        resumed_elapsed = time.time() - resumed_started_at
        test_file_content = test_file_abs.read_text(encoding="utf-8", errors="replace") if test_file_abs.exists() else ""
        updated_result = dict(original_result)
        updated_result.update(
            {
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
                "output": test_output[:2000],
                "test_file_content": test_file_content,
                "test_file_path": test_file_rel,
                "elapsed_seconds": float(original_result.get("elapsed_seconds", 0.0) or 0.0) + resumed_elapsed,
                "test_seconds": float(original_result.get("test_seconds", 0.0) or 0.0) + test_seconds,
                "mutation_seconds": float(original_result.get("mutation_seconds", 0.0) or 0.0) + mutation_seconds,
                "session_id": session_ids[-1] if session_ids else None,
                "session_ids": session_ids,
            }
        )

        merged_timings = _merge_timing_details(
            report.get("timing_details") or {},
            {
                "auth_probe_seconds": auth_seconds,
                "parse_context_seconds": parse_seconds,
                "compile_verification_seconds": compile_seconds,
                "test_execution_seconds": generation_test_seconds + test_seconds,
                "mutation_seconds": mutation_seconds,
            },
        )

        token_usage = _capture_session_token_usage(
            state={"session_token_usage": {}},
            client=client,
            session_ids=session_ids,
        )
        retrospect = _capture_session_retrospect(
            state={"session_retrospect": {}},
            repo_path=repo,
            client=client,
            session_ids=session_ids,
        )

        reporter = Reporter(repo)
        resume_filename = f"resume_{report_file.name}"
        total_elapsed = float((report.get("project_summary") or {}).get("total_elapsed_seconds", 0.0) or 0.0) + resumed_elapsed
        metadata = {
            "repo_path": repo,
            "module": report_module,
            "branch_name": (report.get("metadata") or {}).get("branch_name", "unit-code-gen"),
            "total_candidates": 1,
            "session_retrospect": retrospect,
            "session_token_usage": token_usage,
            "phase_timings": merged_timings,
            "total_elapsed_seconds": total_elapsed,
            "resumed_from_report": str(report_file),
            "run_log_path": run_log_path,
        }
        reporter.save_report({class_fqn: updated_result}, resume_filename, metadata=metadata)
        reporter.display_summary({class_fqn: updated_result}, metadata=metadata)
    finally:
        settings.opencode_model = previous_model
        settings.opencode_provider = previous_provider


@main.command()
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
@click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
@click.option("--days", default=settings.default_days, help="Scan git log for last N days")
@click.option("--module", default=None, help="Target Maven module name")
@click.option("--all", "select_all_files", is_flag=True, help="List all production files instead of git-history ranking")
def scan(repo, language, days, module, select_all_files):
    """Scan and list candidate files for test generation."""
    from uta.engine.source_selection import (
        get_all_java_files,
        get_all_python_files,
        get_changed_java_files,
        get_changed_python_files,
    )

    decision = _resolve_cli_language(repo, language)
    if decision.language == "python":
        if select_all_files:
            console.print(f"[bold blue]Scanning all production Python files in {repo}...[/bold blue]")
            files = get_all_python_files(repo, module)
        else:
            console.print(f"[bold blue]Scanning Python files in {repo} for the last {days} days...[/bold blue]")
            files = get_changed_python_files(repo, days, module)
        table = Table(title=f"Candidates in {repo}")
        table.add_column("Target", style="cyan")
        table.add_column("Changes", justify="right")
        for path, count in files[:20]:
            table.add_row(path, str(count))
        console.print(table)
        console.print(f"\nTotal: {len(files)} candidate files")
        return

    if select_all_files:
        console.print(f"[bold blue]Scanning all production Java files in {repo}...[/bold blue]")
        files = get_all_java_files(repo, module)
    else:
        console.print(f"[bold blue]Scanning {repo} for the last {days} days...[/bold blue]")
        files = get_changed_java_files(repo, days, module)

    table = Table(title=f"Candidates in {repo}")
    table.add_column("File Path", style="cyan")
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
    from uta.engine.parse import ParseProjectRequest, make_parse_provider

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
            console.print_json(json.dumps(payload))
            return
        if not payload["found"]:
            raise click.ClickException(payload.get("error") or f"Python target not found: {target_ref.display_name}")
        console.print(f"[bold cyan]{target_ref.display_name}[/bold cyan]")
        console.print(f"Language: python")
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
        console.print_json(json.dumps(payload))
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
        console.print_json(json.dumps(payload[key]))


@main.command("enforce")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
@click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
@click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
@click.option("--backend", default=None, help="Enforcement backend name")
@click.option("--test-path", "test_paths", multiple=True, help="Test path to run for Python enforcement. Repeat for multiple paths.")
@click.option("--base-ref", default="origin/master", show_default=True, help="Git base ref for incremental enforcement")
@click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
@click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
@click.option("--syntax-version", default="python3", show_default=True, help="Python syntax/runtime lane: python3 or python2")
@click.option("--evidence-output", default=None, type=click.Path(dir_okay=False), help="Optional JSON evidence output file")
@click.option("--dev-skills-launcher-version", default=None, hidden=True)
@click.option("--dry-run", is_flag=True, help="Resolve language and targets without running the gate")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
def enforce(repo, language, targets, backend, test_paths, base_ref, coverage_gate, mutation_gate, syntax_version, evidence_output, dev_skills_launcher_version, dry_run, json_output):
    """Resolve a language-aware enforcement request."""
    decision = _resolve_cli_language(repo, language, targets=targets)
    normalized = _normalize_cli_targets(decision.language, targets)
    payload = {
        "language": decision.language,
        "backend": backend or ("maven_enforcer" if decision.language == "java" else f"{decision.language}_enforcer"),
        "targets": [target.as_selection() for target in normalized],
        "languageDecision": decision.as_dict(),
        "dryRun": bool(dry_run),
    }
    if dry_run:
        if json_output:
            console.print_json(json.dumps(payload))
        else:
            console.print(f"[green]Resolved {payload['backend']} for language={decision.language}[/green]")
        return
    if decision.language == "python" and payload["backend"] == "python_enforcer":
        from uta.language.python.enforcement import format_evidence_markers, run_python_enforcement

        evidence = run_python_enforcement(
            repo_path=Path(repo),
            target_values=list(targets),
            test_paths=list(test_paths),
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            syntax_version=syntax_version,
            dev_skills_launcher_version=dev_skills_launcher_version,
        )
        if evidence_output:
            Path(evidence_output).expanduser().write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        if json_output:
            click.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        else:
            click.echo(format_evidence_markers(evidence), nl=False)
        if not evidence.get("passed"):
            raise click.exceptions.Exit(1)
        return
    if decision.language == "java" and payload["backend"] == "maven_enforcer":
        from uta.language.java.enforcement import format_evidence_markers, run_java_enforcement

        evidence = run_java_enforcement(
            repo_path=Path(repo),
            command=settings.ci_enforcement_command,
            base_ref=base_ref,
            timeout_seconds=int(settings.ci_enforcement_timeout_seconds or 1800),
        )
        if evidence_output:
            Path(evidence_output).expanduser().write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        if json_output:
            click.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        else:
            click.echo(format_evidence_markers(evidence), nl=False)
        if not evidence.get("passed"):
            raise click.exceptions.Exit(1)
        return
    raise click.ClickException(
        f"Language-aware enforcement routing is registered for language={decision.language}; "
        "gate execution is implemented in the enforcement phase."
    )


@main.command("python-enforce")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the Python repository")
@click.option("--target", "targets", multiple=True, help="Python target path.py or path.py::symbol")
@click.option("--test-path", "test_paths", multiple=True, help="Test path to run. Repeat for multiple paths.")
@click.option("--base-ref", default="origin/master", show_default=True, help="Git base ref for incremental enforcement")
@click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
@click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
@click.option("--syntax-version", default="python3", show_default=True, help="Python syntax/runtime lane: python3 or python2")
@click.option("--evidence-output", default=None, type=click.Path(dir_okay=False), help="Optional JSON evidence output file")
@click.option("--dev-skills-launcher-version", default=None, hidden=True)
@click.option("--dry-run", is_flag=True, help="Resolve language and targets without running the gate")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
def python_enforce(repo, targets, test_paths, base_ref, coverage_gate, mutation_gate, syntax_version, evidence_output, dev_skills_launcher_version, dry_run, json_output):
    """Compatibility alias for Python enforcement routing."""
    return enforce.callback(
        repo=repo,
        language="python",
        targets=targets,
        backend="python_enforcer",
        test_paths=test_paths,
        base_ref=base_ref,
        coverage_gate=coverage_gate,
        mutation_gate=mutation_gate,
        syntax_version=syntax_version,
        evidence_output=evidence_output,
        dev_skills_launcher_version=dev_skills_launcher_version,
        dry_run=dry_run,
        json_output=json_output,
    )


@main.command("assess")
@click.option("--session-id", "session_ids", required=True, multiple=True, help="OpenCode session ID to assess. Repeat to aggregate multiple sessions.")
@click.option("--baseline-session-id", "baseline_session_ids", multiple=True, help="Optional baseline OpenCode session ID. Repeat to aggregate multiple baseline sessions.")
@click.option(
    "--db-path",
    default=str(DEFAULT_OPENCODE_DB),
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to opencode.db",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON instead of rich tables")
def assess(session_ids, baseline_session_ids, db_path, json_output):
    """Inspect OpenCode session token usage, verbosity, and top expensive steps."""
    current = assess_sessions(session_ids, Path(db_path))
    baseline = assess_sessions(baseline_session_ids, Path(db_path)) if baseline_session_ids else None

    if json_output:
        payload = {
            "current": {
                "session_id": current.session_id,
                "session_ids": current.session_ids,
                "first_part": iso_local(current.first_part_ms),
                "last_part": iso_local(current.last_part_ms),
                "duration_seconds": current.duration_seconds,
                "part_count": current.part_count,
                "step_count": current.step_count,
                "tool_call_count": current.tool_call_count,
                "text_chars": current.text_chars,
                "reasoning_chars": current.reasoning_chars,
                "tokens": {
                    "input": current.input_tokens,
                    "output": current.output_tokens,
                    "reasoning": current.reasoning_tokens,
                    "cache_read": current.cache_read_tokens,
                    "cache_write": current.cache_write_tokens,
                    "total": current.total_tokens,
                },
                "verbosity": {
                    "output_tokens_per_step": current.output_tokens_per_step,
                    "output_tokens_per_tool_call": current.output_tokens_per_tool_call,
                    "text_chars_per_step": current.text_chars_per_step,
                    "cache_share": current.cache_share,
                },
                "top_tools": top_tools_rows(current),
                "top_steps": [
                    {
                        "index": step.index,
                        "reason": step.reason,
                        "total_tokens": step.total_tokens,
                        "input_tokens": step.input_tokens,
                        "output_tokens": step.output_tokens,
                        "cache_read_tokens": step.cache_read_tokens,
                        "tool_calls": step.tool_calls,
                        "tools": dict(step.tools),
                    }
                    for step in current.top_steps()
                ],
            },
        }
        if baseline:
            payload["baseline"] = {
                "session_id": baseline.session_id,
                "session_ids": baseline.session_ids,
                "duration_seconds": baseline.duration_seconds,
                "step_count": baseline.step_count,
                "tool_call_count": baseline.tool_call_count,
                "text_chars": baseline.text_chars,
                "tokens": {
                    "input": baseline.input_tokens,
                    "output": baseline.output_tokens,
                    "reasoning": baseline.reasoning_tokens,
                    "cache_read": baseline.cache_read_tokens,
                    "cache_write": baseline.cache_write_tokens,
                    "total": baseline.total_tokens,
                },
            }
            payload["comparison"] = compare_sessions(current, baseline)
        console.print_json(json.dumps(payload))
        return

    summary = Table(title=f"UTA Session Assessment: {', '.join(current.session_ids)}")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("First Part", iso_local(current.first_part_ms))
    summary.add_row("Last Part", iso_local(current.last_part_ms))
    summary.add_row("Duration", f"{current.duration_seconds:.1f}s")
    summary.add_row("Parts", str(current.part_count))
    summary.add_row("Steps", str(current.step_count))
    summary.add_row("Stop Steps", str(current.stop_steps))
    summary.add_row("Tool Calls", str(current.tool_call_count))
    summary.add_row("Assistant Text Chars", str(current.text_chars))
    summary.add_row("Reasoning Chars", str(current.reasoning_chars))
    console.print(summary)

    tokens = Table(title="Tokens")
    tokens.add_column("Bucket", style="cyan")
    tokens.add_column("Value", justify="right")
    tokens.add_row("Input", str(current.input_tokens))
    tokens.add_row("Output", str(current.output_tokens))
    tokens.add_row("Reasoning", str(current.reasoning_tokens))
    tokens.add_row("Cache Read", str(current.cache_read_tokens))
    tokens.add_row("Cache Write", str(current.cache_write_tokens))
    tokens.add_row("Total", str(current.total_tokens))
    tokens.add_row("Non-cache Total", str(current.input_tokens + current.output_tokens + current.reasoning_tokens))
    console.print(tokens)

    verbosity = Table(title="Verbosity / Efficiency")
    verbosity.add_column("Metric", style="cyan")
    verbosity.add_column("Value", justify="right")
    verbosity.add_row("Output Tokens / Step", f"{current.output_tokens_per_step:.1f}")
    verbosity.add_row("Output Tokens / Tool Call", f"{current.output_tokens_per_tool_call:.1f}")
    verbosity.add_row("Text Chars / Step", f"{current.text_chars_per_step:.1f}")
    verbosity.add_row("Cache Share", f"{current.cache_share:.1%}")
    console.print(verbosity)

    tools = Table(title="Top Tools")
    tools.add_column("Tool", style="cyan")
    tools.add_column("Calls", justify="right")
    for tool_name, count in top_tools_rows(current):
        tools.add_row(tool_name, str(count))
    console.print(tools)

    expensive = Table(title="Top Expensive Steps")
    expensive.add_column("#", justify="right")
    expensive.add_column("Reason")
    expensive.add_column("Total", justify="right")
    expensive.add_column("In", justify="right")
    expensive.add_column("Out", justify="right")
    expensive.add_column("Cache", justify="right")
    expensive.add_column("Tools", justify="right")
    for step in current.top_steps():
        expensive.add_row(
            str(step.index),
            step.reason or "-",
            str(step.total_tokens),
            str(step.input_tokens),
            str(step.output_tokens),
            str(step.cache_read_tokens),
            str(step.tool_calls),
        )
    console.print(expensive)

    if baseline:
        comparison = compare_sessions(current, baseline)
        compare_table = Table(title=f"Comparison vs {baseline.session_id}")
        compare_table.add_column("Metric", style="cyan")
        compare_table.add_column("Current", justify="right")
        compare_table.add_column("Baseline", justify="right")
        compare_table.add_column("Delta", justify="right")
        compare_table.add_column("Delta %", justify="right")
        for metric, values in comparison.items():
            compare_table.add_row(
                metric,
                f"{values['current']:.1f}",
                f"{values['baseline']:.1f}",
                f"{values['delta']:.1f}",
                f"{values['pct_delta']:.1f}%",
            )
        console.print(compare_table)


if __name__ == "__main__":
    main()
