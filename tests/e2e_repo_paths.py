"""Configurable paths for pipeline E2E tests."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple


def primary_repo_path() -> str:
    """Primary Java repo for the main E2E block (default: sample-service)."""
    return os.path.abspath(
        os.path.expanduser(os.environ.get("UTA_E2E_REPO", "~/src/sample-service"))
    )


def _modules_from_pom(repo: Path) -> List[str]:
    pom = repo / "pom.xml"
    if not pom.is_file():
        return []
    text = pom.read_text(encoding="utf-8", errors="replace")
    return re.findall(r"<module>\s*([^<]+?)\s*</module>", text)


def infer_java_module(
    repo_root: str,
    *,
    explicit_env: Optional[str] = None,
    use_global_module_env: bool = True,
) -> str:
    """Pick a Maven reactor module that contains ``src/main/java``.

    Prefers ``biz`` when present (matches many multi-module service layouts).

    :param explicit_env: if set, read this env var first (e.g. ``UTA_E2E_SERVICE_A_MODULE``).
    :param use_global_module_env: if True (and ``explicit_env`` unset), honor ``UTA_E2E_MODULE``.
    """
    if explicit_env:
        ex = os.environ.get(explicit_env, "").strip()
        if ex:
            return ex
    if use_global_module_env:
        explicit = os.environ.get("UTA_E2E_MODULE", "").strip()
        if explicit:
            return explicit
    repo = Path(repo_root)
    mods = _modules_from_pom(repo)
    if "biz" in mods and (repo / "biz" / "src" / "main" / "java").is_dir():
        return "biz"
    for mod in mods:
        if (repo / mod / "src" / "main" / "java").is_dir():
            return mod
    if (repo / "src" / "main" / "java").is_dir():
        return ""
    return "biz"


def module_main_java(repo_root: str, module: str) -> Path:
    """``src/main/java`` root for a Maven module (or repo root if ``module`` is empty)."""
    r = Path(repo_root)
    if module:
        return r / module / "src" / "main" / "java"
    return r / "src" / "main" / "java"


def pick_service_like_class_for_context(graph) -> Optional[str]:
    """Pick a concrete class suitable for ``ContextBuilder.build_for_class``."""
    for fqn, node in graph.nodes.items():
        if node.kind != "class":
            continue
        name = fqn.split(".")[-1]
        ann = node.metadata.get("annotations", []) or []
        if "Service" in ann and (name.endswith("Impl") or "Service" in name):
            return fqn
    for fqn, node in graph.nodes.items():
        if node.kind == "class" and fqn.split(".")[-1].endswith("Impl"):
            return fqn
    for fqn, node in graph.nodes.items():
        if node.kind == "class":
            return fqn
    return None


def cross_repo_matrix() -> List[Tuple[str, str, str, Optional[str]]]:
    """(label, resolved_repo_path, repo_env, module_env) for extra org repos.

    Defaults: ``~/src/service-a``, ``~/src/service-b``.
    Override paths with ``UTA_E2E_SERVICE_A_REPO`` / ``UTA_E2E_SERVICE_B_REPO``; modules with
    ``UTA_E2E_SERVICE_A_MODULE`` / ``UTA_E2E_SERVICE_B_MODULE``.
    """
    rows = [
        (
            "service_a",
            os.environ.get(
                "UTA_E2E_SERVICE_A_REPO",
                os.path.expanduser("~/src/service-a"),
            ),
            "UTA_E2E_SERVICE_A_REPO",
            "UTA_E2E_SERVICE_A_MODULE",
        ),
        (
            "service_b",
            os.environ.get(
                "UTA_E2E_SERVICE_B_REPO",
                os.path.expanduser("~/src/service-b"),
            ),
            "UTA_E2E_SERVICE_B_REPO",
            "UTA_E2E_SERVICE_B_MODULE",
        ),
    ]
    return [
        (lab, os.path.abspath(os.path.expanduser(p)), renv, menv)
        for lab, p, renv, menv in rows
    ]


def _fixture_python_repo(name: str) -> str:
    return str((Path(__file__).resolve().parent / "fixtures" / "python_projects" / name).resolve())


def python_repo_matrix() -> List[dict]:
    """Python repo configs for staged E2E tests.

    Defaults point at tiny checked-in fixture repos so Phase 0 tests never need
    external repos or OpenCode. Real repos can be supplied with the documented
    UTA_E2E_PY3_* and UTA_E2E_PY2_* environment variables.
    """
    py3_repo = os.environ.get("UTA_E2E_PY3_REPO", _fixture_python_repo("py3_flat_project"))
    py2_repo = os.environ.get("UTA_E2E_PY2_REPO", _fixture_python_repo("py2_legacy_project"))
    return [
        {
            "language": "python3",
            "repo_path": os.path.abspath(os.path.expanduser(py3_repo)),
            "repo_env": "UTA_E2E_PY3_REPO",
            "target": os.environ.get("UTA_E2E_PY3_TARGET", "jobs/forecast.py::forecast_for_store"),
            "target_env": "UTA_E2E_PY3_TARGET",
            "test_command": os.environ.get("UTA_E2E_PY3_TEST_COMMAND", "python3 -m unittest discover -s tests"),
            "test_command_env": "UTA_E2E_PY3_TEST_COMMAND",
            "python_bin": os.environ.get("UTA_E2E_PY3_BIN", "python3"),
            "python_bin_env": "UTA_E2E_PY3_BIN",
            "mutmut_bin": os.environ.get("UTA_E2E_PY3_MUTMUT_BIN", "mutmut"),
            "mutmut_bin_env": "UTA_E2E_PY3_MUTMUT_BIN",
        },
        {
            "language": "python2",
            "repo_path": os.path.abspath(os.path.expanduser(py2_repo)),
            "repo_env": "UTA_E2E_PY2_REPO",
            "target": os.environ.get("UTA_E2E_PY2_TARGET", "legacy_job.py::legacy_total"),
            "target_env": "UTA_E2E_PY2_TARGET",
            "test_command": os.environ.get("UTA_E2E_PY2_TEST_COMMAND", "python2 -m unittest discover -s tests"),
            "test_command_env": "UTA_E2E_PY2_TEST_COMMAND",
            "python_bin": os.environ.get("UTA_E2E_PY2_BIN", "python2"),
            "python_bin_env": "UTA_E2E_PY2_BIN",
            "mutmut_bin": os.environ.get("UTA_E2E_PY2_MUTMUT_BIN", "mutmut"),
            "mutmut_bin_env": "UTA_E2E_PY2_MUTMUT_BIN",
        },
    ]


def find_largest_main_java(repo_root: str, module: str) -> Optional[Tuple[Path, int]]:
    """Return ``(path, line_count)`` for the largest ``src/main/java`` file under module."""
    root = Path(repo_root)
    base = root / module / "src" / "main" / "java" if module else root / "src" / "main" / "java"
    if not base.is_dir():
        return None
    best: Optional[Tuple[Path, int]] = None
    for p in base.rglob("*.java"):
        try:
            n = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if best is None or n > best[1]:
            best = (p, n)
    return best
