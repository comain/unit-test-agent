"""Language-neutral candidate source-file selection.

Ranks production source files by git change frequency or enumerates them all.
The scan/walk skeleton is shared; each language adapter decides what counts as
a production source path via ``is_production_source_path``.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

# Directories never worth descending into, regardless of backend language.
_WALK_EXCLUDED_DIRS = {".git", ".hg", ".svn"}


#: Generous, because a wide `--since` window on a large history is legitimately
#: slow. It exists to catch a wedge, not to police a slow query.
_GIT_LOG_TIMEOUT_SECONDS = 120


def _adapter_for(language: str):
    from uta.shared.backends import make_backend

    return make_backend(language, "adapter")


def get_changed_source_files(
    language: str,
    repo_path: str,
    days: int = 30,
    module: Optional[str] = None,
) -> List[Tuple[str, int]]:
    """Production source files changed in the last N days, ranked by change count."""
    adapter = _adapter_for(language)
    cmd = [
        "git",
        "-C", repo_path,
        "log",
        f"--since={days} days ago",
        "--name-only",
        "--pretty=format:",
    ]
    try:
        # Bounded: the window is configurable, so on a large repository this is
        # slow rather than hung -- which is the harder failure to recognise.
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=_GIT_LOG_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as e:
        print(f"Error running git log: {e.stderr}")
        return []

    changed = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not adapter.is_production_source_path(line):
            continue
        if module and not _path_is_under_module(line, module):
            continue
        changed.append(line)

    return Counter(changed).most_common()


def get_all_source_files(
    language: str,
    repo_path: str,
    module: Optional[str] = None,
) -> List[Tuple[str, int]]:
    """All production source files under the repo/module, in stable path order.

    The count is always 1 so callers can reuse the same (path, count) shape as
    git-history ranked files.
    """
    return [(path, 1) for path in iter_all_source_files(_adapter_for(language), repo_path, module=module)]


def iter_all_source_files(adapter, repo_path: str, module: Optional[str] = None) -> List[str]:
    """Enumerate repo-relative production source paths for one language adapter."""
    root = Path(repo_path)
    search_root = root / module if module else root
    files: List[str] = []
    for current_root, dirnames, filenames in os.walk(search_root):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in _WALK_EXCLUDED_DIRS)
        current = Path(current_root)
        for filename in filenames:
            path = current / filename
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            if adapter.is_production_source_path(rel):
                files.append(rel)
    files.sort(key=lambda rel: tuple(rel.split("/")))
    return files


def filter_files(files: List[Tuple[str, int]], max_files: int = 10) -> List[str]:
    """Take top N files."""
    return [path for path, count in files[:max_files]]


def _path_is_under_module(path: str, module: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    module_prefix = str(module or "").strip("/").replace("\\", "/")
    return not module_prefix or normalized == module_prefix or normalized.startswith(f"{module_prefix}/")
