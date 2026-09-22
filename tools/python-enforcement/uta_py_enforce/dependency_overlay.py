"""Nested Python dependency overlay resolution, caching, and isolation.

Pip owns dependency resolution here, not UTA. The overlay first installs the
target's nearest manifest as a whole -- ``pip install -r``. If a stale broad
manifest cannot build for the selected runtime, a deterministic compatibility
fallback installs only unpinned distributions imported by the target and its
selected tests. This fallback is intentionally secondary: a static import scan
cannot replace pip's package-graph resolution.

The one thing UTA still decides is what to do with a distribution pip reports
as unavailable from the configured index. A single missing package must not
cost the whole environment, so that line is dropped and the install retried,
with the omission recorded in the evidence.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping, Optional, Sequence


_NO_MATCHING_DISTRIBUTION = re.compile(
    r"No matching distribution found for\s+([^\s]+)",
    re.IGNORECASE,
)

#: Requirement-file options whose argument is a path relative to the file that
#: declares it. Flattening moves lines out of that file, so they are rewritten.
_INCLUDE_OPTION = re.compile(r"^\s*(?:-r|--requirement)(?:[=\s]+)(.+?)\s*$")
_PATH_OPTIONS = (
    (re.compile(r"^\s*(?:-c|--constraint)(?:[=\s]+)(.+?)\s*$"), "-c"),
    (re.compile(r"^\s*(?:-f|--find-links)(?:[=\s]+)(.+?)\s*$"), "-f"),
)


def nearest_requirements_manifest(repo: Path, source_path: str) -> Optional[Path]:
    """Nearest requirements.txt from the target's directory up to the repo root.

    Nearest still wins, so a repository that nests a manifest per service keeps
    installing that service's manifest rather than a root-level superset. The
    root is only consulted when nothing nearer exists.

    The root used to be excluded, which left single-manifest repositories with
    no dependency overlay at all: one production run failed all 39 targets on
    ``ModuleNotFoundError: No module named 'django'`` while ``Django==3.2.25``
    sat on line 1 of the repository-root manifest.

    A candidate counts only if it declares something installable. "Not blank"
    is the wrong test now that the root is reachable: a nearer manifest holding
    only comments would shadow it, callers would find nothing to install, and
    the repository would be back to having no overlay for a subtler reason.
    Includes are followed, so a manifest that is one ``-r`` line still counts.
    """
    source = (repo / source_path).resolve()
    repo_resolved = repo.resolve()
    try:
        source.relative_to(repo_resolved)
    except ValueError:
        return None
    for directory in (source.parent, *source.parents):
        candidate = directory / "requirements.txt"
        if candidate.is_file() and manifest_requirement_lines(flatten_manifest(candidate)):
            return candidate
        if directory == repo_resolved:
            break
    return None


def flatten_manifest(manifest: Path, _seen: Optional[set[Path]] = None) -> list[str]:
    """Inline nested ``-r`` includes so every requirement is a top-level line.

    Dropping an unavailable distribution means rewriting the manifest, and a
    rewritten copy no longer sits beside the files its relative paths were
    written against. Flattening resolves both at once: includes are expanded,
    and the remaining path-valued options are made absolute.
    """
    seen = set() if _seen is None else _seen
    resolved = manifest.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)
    lines: list[str] = []
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        include = _INCLUDE_OPTION.match(raw_line)
        if include:
            nested = (resolved.parent / include.group(1)).resolve()
            if nested.is_file():
                lines.extend(flatten_manifest(nested, seen))
            continue
        rewritten = None
        for pattern, flag in _PATH_OPTIONS:
            match = pattern.match(raw_line)
            if match:
                rewritten = f"{flag} {(resolved.parent / match.group(1)).resolve()}"
                break
        lines.append(rewritten if rewritten is not None else raw_line)
    return lines


def manifest_requirement_lines(lines: Sequence[str]) -> list[str]:
    """The installable requirement lines, ignoring comments, blanks, and options."""
    requirements = []
    for raw_line in lines:
        requirement = raw_line.split(" #", 1)[0].strip()
        if requirement.startswith("#"):
            continue
        if not requirement or requirement.startswith("-"):
            continue
        requirements.append(requirement)
    return requirements


def dependency_overlay_env(
    dependency_dir: Path | None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return the child environment that makes an isolated overlay importable."""
    if dependency_dir is None:
        return dict(base_env) if base_env is not None else None
    env = dict(base_env if base_env is not None else os.environ)
    existing = str(env.get("PYTHONPATH", "") or "")
    entries = [dependency_dir.as_posix()]
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _result_value(result: Any, *names: str, default: Any = None) -> Any:
    """Read either the canonical evidence mapping or a subprocess result."""
    if isinstance(result, Mapping):
        for name in names:
            if name in result:
                return result[name]
        return default
    for name in names:
        if hasattr(result, name):
            return getattr(result, name)
    return default


def install_manifest_requirements(
    repo: Path,
    dependency_dir: Path,
    manifest_lines: Sequence[str],
    *,
    python_bin: str,
    timeout_seconds: int,
    runner: Callable[..., Any],
    requirements_path: Path,
) -> dict[str, Any]:
    """Install the whole manifest, dropping only distributions pip cannot find.

    ``requirements_path`` is where the manifest UTA hands pip is written; it is
    the flattened copy, minus any line pip has identified as unavailable from
    the configured index.
    """
    lines = list(manifest_lines)
    skipped: list[str] = []
    last_stdout = ""
    last_stderr = ""
    last_command: list[str] = []
    # With no UTA override, inherit pip's configured index. This keeps local
    # PyPI behavior and lets managed environments use their approved mirror.
    dependency_index_url = os.environ.get("UTA_PYTHON_DEPENDENCY_INDEX_URL", "").strip()
    index_args = ["--index-url", dependency_index_url] if dependency_index_url else []

    while True:
        requirements_path.parent.mkdir(parents=True, exist_ok=True)
        requirements_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        last_command = [
            python_bin,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *index_args,
            "--target",
            str(dependency_dir),
            "-r",
            str(requirements_path),
        ]
        completed = runner(last_command, cwd=repo, timeout=timeout_seconds)
        exit_code = int(
            _result_value(completed, "returncode", "exit_code", "exitCode", default=0)
        )
        last_stdout = str(_result_value(completed, "stdout", default="") or "")
        last_stderr = str(_result_value(completed, "stderr", default="") or "")
        if exit_code == 0:
            break

        unavailable = _unavailable_line(last_stdout + "\n" + last_stderr, lines)
        if unavailable is None:
            return _dependency_install_evidence(
                last_command,
                exit_code,
                last_stdout,
                last_stderr,
                skipped,
            )
        skipped.append(unavailable.strip())
        lines.remove(unavailable)
        if not manifest_requirement_lines(lines):
            # Dropping one unavailable distribution keeps the environment
            # usable. Dropping every one of them does not: pip would then be
            # asked to install nothing, exit 0, and UTA would cache an empty
            # overlay as a success and measure the gates against it. An index
            # that can serve none of the manifest is a broken environment, and
            # it has to fail loudly here rather than as a wall of
            # ModuleNotFoundError inside the target's own tests.
            return _dependency_install_evidence(
                last_command,
                exit_code,
                last_stdout,
                (
                    "UTA: no requirement in the manifest could be installed from the "
                    "configured index; refusing to cache an empty dependency overlay.\n"
                    + last_stderr
                ),
                skipped,
            )

    return _dependency_install_evidence(
        last_command,
        exit_code,
        last_stdout,
        last_stderr,
        skipped,
    )


def _unavailable_line(output: str, lines: Sequence[str]) -> Optional[str]:
    match = _NO_MATCHING_DISTRIBUTION.search(output)
    if match is None:
        return None
    missing_name = _distribution_name(match.group(1))
    for raw_line in lines:
        requirement = raw_line.split(" #", 1)[0].strip()
        if not requirement or requirement.startswith(("#", "-")):
            continue
        if _distribution_name(requirement) == missing_name:
            return raw_line
    return None


def _distribution_name(requirement: str) -> str:
    distribution = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", distribution).lower()


_DISTRIBUTION_IMPORT_ALIASES = {
    "beautifulsoup4": "bs4",
    "opencv-python": "cv2",
    "pillow": "pil",
    "pyjwt": "jwt",
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
}


def _target_import_roots(repo: Path, source_path: str, test_paths: Sequence[str]) -> set[str]:
    """Collect imports reachable from the target and selected tests.

    This is deliberately a fallback signal, not UTA's primary dependency
    resolver. Pip still receives the complete nearest manifest first. Static
    discovery is used only after that manifest proves incompatible with the
    selected runtime, when running no test at all would be strictly worse.
    """
    roots: set[str] = set()
    pending = [repo / path for path in (source_path, *test_paths)]
    visited: set[Path] = set()
    while pending:
        path = pending.pop(0).resolve()
        if path in visited or not path.is_file() or path.suffix != ".py":
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0].lower()
                local = repo / f"{root}.py"
                package = repo / root / "__init__.py"
                if local.is_file():
                    pending.append(local)
                elif package.is_file():
                    pending.append(package)
                else:
                    roots.add(root)
    return roots


def _compatible_target_requirements(
    repo: Path,
    source_path: str,
    test_paths: Sequence[str],
    manifest_lines: Sequence[str],
) -> list[str]:
    """Return deterministic, unpinned requirements needed by target tests."""
    imports = _target_import_roots(repo, source_path, test_paths)
    selected: list[str] = []
    for raw_line in manifest_lines:
        requirement = raw_line.split(" #", 1)[0].strip()
        if not requirement or requirement.startswith(("#", "-")):
            continue
        distribution = _distribution_name(requirement)
        import_name = _DISTRIBUTION_IMPORT_ALIASES.get(
            distribution, distribution.replace("-", "_")
        )
        meaningful_tokens = {
            token
            for token in distribution.split("-")
            if len(token) >= 3 and token not in {"python", "client", "library", "sdk"}
        }
        if import_name not in imports and not (meaningful_tokens & imports):
            continue
        # Keep extras but discard pins and environment markers. The complete
        # manifest already failed for this runtime; repeating an incompatible
        # legacy pin cannot produce a usable target-specific environment.
        spec = re.split(r"[<>=!~;]", requirement, maxsplit=1)[0].strip()
        if spec and spec not in selected:
            selected.append(spec)
    return selected


def _dependency_install_evidence(
    command: Sequence[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    skipped: Sequence[str],
) -> dict[str, Any]:
    skipped_detail = "\n".join(
        f"UTA: Skipped unavailable dependency: {requirement}" for requirement in skipped
    )
    combined_stderr = "\n".join(part for part in (skipped_detail, stderr) if part)
    return {
        "name": "dependency_overlay_install",
        "command": list(command),
        "exitCode": exit_code,
        "stdout": stdout[-4000:] if stdout else "",
        "stderr": combined_stderr[-4000:] if combined_stderr else "",
    }


def manifest_digest(manifest_lines: Sequence[str]) -> str:
    """Digest the flattened manifest, so a nested include's edit invalidates too."""
    return hashlib.sha256("\n".join(manifest_lines).encode("utf-8")).hexdigest()


def prepare_dependency_overlay(
    repo: Path,
    source_path: str,
    test_paths: Sequence[str] = (),
    *,
    python_bin: str,
    timeout_seconds: int,
    artifact_dir: Path | str,
    runner: Callable[..., Any],
    runtime_cache_key: str = "",
) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """Discover, lock, install, and cache nested dependency overlay for a target.

    Returns (evidence_dict, dependency_overlay_dir, digest).
    """
    repo_resolved = Path(repo).resolve()
    manifest = nearest_requirements_manifest(repo_resolved, source_path)
    if manifest is None:
        return None, None, None

    manifest_lines = flatten_manifest(manifest)
    if not manifest_requirement_lines(manifest_lines):
        return None, None, None

    content_digest = manifest_digest(manifest_lines)
    digest = hashlib.sha256(
        json.dumps(
            {
                "manifest": content_digest,
                "pythonBin": python_bin,
                "runtimeCacheKey": runtime_cache_key,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    dep_root = repo_resolved / artifact_dir / "dependencies" / digest[:16]
    marker = dep_root / ".uta-installed"
    dep_root.mkdir(parents=True, exist_ok=True)

    lock_path = dep_root.parent / f"{digest[:16]}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    requirements_path = dep_root.parent / f"{digest[:16]}.requirements.txt"

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        marker_value = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        cache_valid = marker_value == digest or marker_value.startswith(f"{digest}:compat:")
        if cache_valid:
            cmd = [python_bin, "-m", "pip", "install", "--target", str(dep_root), "-r", str(requirements_path)]
            evidence = {
                "name": (
                    "dependency_overlay_compat_cached"
                    if ":compat:" in marker_value
                    else "dependency_overlay_cached"
                ),
                "command": cmd,
                "exitCode": 0,
                "stdout": "",
                "stderr": "",
            }
            return evidence, dep_root, content_digest

        # Never layer a new install over an interrupted cache or binary wheels
        # built for a different interpreter ABI.
        shutil.rmtree(dep_root, ignore_errors=True)
        dep_root.mkdir(parents=True, exist_ok=True)
        evidence = install_manifest_requirements(
            repo_resolved,
            dep_root,
            manifest_lines,
            python_bin=python_bin,
            timeout_seconds=timeout_seconds,
            runner=runner,
            requirements_path=requirements_path,
        )
        if evidence["exitCode"] == 0:
            marker.write_text(digest, encoding="utf-8")
            return evidence, dep_root, content_digest

        is_repository_root_manifest = manifest.resolve() == (repo_resolved / "requirements.txt")
        compatibility_requirements = (
            _compatible_target_requirements(
                repo_resolved, source_path, test_paths, manifest_lines
            )
            if is_repository_root_manifest
            else []
        )
        whole_manifest_error = str(evidence.get("stderr") or evidence.get("stdout") or "")
        if (
            not compatibility_requirements
            or "refusing to cache an empty dependency overlay" in whole_manifest_error
        ):
            shutil.rmtree(dep_root, ignore_errors=True)
            dep_root.mkdir(parents=True, exist_ok=True)
            return evidence, dep_root, content_digest

        shutil.rmtree(dep_root, ignore_errors=True)
        dep_root.mkdir(parents=True, exist_ok=True)
        dependency_index_url = os.environ.get("UTA_PYTHON_DEPENDENCY_INDEX_URL", "").strip()
        index_args = ["--index-url", dependency_index_url] if dependency_index_url else []
        command = [
            python_bin,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *index_args,
            "--target",
            str(dep_root),
            *compatibility_requirements,
        ]
        completed = runner(command, cwd=repo_resolved, timeout=timeout_seconds)
        exit_code = int(
            _result_value(completed, "returncode", "exit_code", "exitCode", default=0)
        )
        stdout = str(_result_value(completed, "stdout", default="") or "")
        stderr = str(_result_value(completed, "stderr", default="") or "")
        fallback_detail = (
            "UTA: whole manifest failed for the selected Python runtime; "
            "retried unpinned target-import requirements.\n"
            + whole_manifest_error[-2000:]
        )
        compatibility_evidence = {
            "name": "dependency_overlay_compat_install",
            "command": command,
            "exitCode": exit_code,
            "stdout": stdout[-4000:],
            "stderr": "\n".join(part for part in (fallback_detail, stderr[-2000:]) if part),
        }
        if exit_code == 0:
            compatibility_digest = hashlib.sha256(
                json.dumps(compatibility_requirements).encode("utf-8")
            ).hexdigest()[:16]
            marker.write_text(f"{digest}:compat:{compatibility_digest}", encoding="utf-8")
            return compatibility_evidence, dep_root, content_digest
        shutil.rmtree(dep_root, ignore_errors=True)
        dep_root.mkdir(parents=True, exist_ok=True)
        return compatibility_evidence, dep_root, content_digest
