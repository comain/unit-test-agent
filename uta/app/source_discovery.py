"""Finding the Java source for a class that is not in the repository.

`uta query-index` is asked about a class by name. Often that class is not in
the checkout at all: it is in a sibling module of a multi-repo project, under
a configured shared-source directory, or only in a `-sources.jar` in the local
Maven repository. So the answer is produced by an escalating ladder, and the
ladder is this module -- module roots, configured bases, sibling repositories,
external roots, then source jars, fetched from Maven if the local repository
has none.

It was 400 lines of `cli.py`, which made the CLI's largest responsibility
something the CLI does not do: it parses Maven `settings.xml`, unpacks jars
into a fingerprinted cache and shells out to `mvn`. None of that is command
plumbing, none of it is reused by any other command, and all of it is
testable without a `CliRunner`.

`load_index_payload` is the ladder itself, and the only entry point the
command layer needs; everything else is one rung of it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from uta.shared.config import settings

# The CLI's logger, not this module's: `query-index` failing to fetch sources
# is a CLI event, and a deployment reading "uta" logs should keep seeing it.
logger = logging.getLogger("uta")


def _load_graph_and_flows(repo: str, module: Optional[str]) -> Tuple[Any, List[Any]]:
    from uta.shared.parse import ParseProjectRequest, make_parse_provider

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

    from uta.shared.parse import ParseProjectRequest, make_parse_provider

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


def load_index_payload(
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


__all__ = ["load_index_payload"]
