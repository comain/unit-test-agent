import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

_PYTHON_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "__pycache__",
    "site-packages",
    "dist",
    "build",
}


def get_changed_java_files(repo_path: str, days: int = 30, module: Optional[str] = None) -> List[Tuple[str, int]]:
    """
    Scans the git log for the last N days and returns a list of .java files
    ranked by change frequency.

    Returns:
        List of (relative_file_path, change_count)
    """
    cmd = [
        "git",
        "-C", repo_path,
        "log",
        f"--since={days} days ago",
        "--name-only",
        "--pretty=format:",
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running git log: {e.stderr}")
        return []
        
    lines = result.stdout.splitlines()
    
    # Filter for .java files in src/main/java (exclude tests)
    # Also filter by module if provided
    java_files = []
    for line in lines:
        line = line.strip()
        if not line or not line.endswith(".java"):
            continue
        
        # We only want production code, usually in src/main/java
        if "src/main/java" not in line:
            continue
            
        # Module filter: if module is 'biz', path should contain 'biz/'
        if module and f"{module}/" not in line:
            continue
            
        java_files.append(line)
        
    # Count frequencies
    counts = Counter(java_files)
    
    # Return ranked list: (path, count)
    return counts.most_common()


def get_changed_python_files(repo_path: str, days: int = 30, module: Optional[str] = None) -> List[Tuple[str, int]]:
    """
    Scans the git log for the last N days and returns production .py files
    ranked by change frequency.

    This intentionally mirrors the Java scan contract while applying Python
    source filters: exclude tests, virtualenv/vendor/build dirs, __init__.py,
    and test_*.py helpers.
    """
    cmd = [
        "git",
        "-C", repo_path,
        "log",
        f"--since={days} days ago",
        "--name-only",
        "--pretty=format:",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running git log: {e.stderr}")
        return []

    python_files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not _is_production_python_relpath(line):
            continue
        if module and not _path_is_under_module(line, module):
            continue
        python_files.append(line)

    return Counter(python_files).most_common()


def filter_files(files: List[Tuple[str, int]], max_files: int = 10) -> List[str]:
    """Take top N files."""
    return [path for path, count in files[:max_files]]


def get_all_java_files(repo_path: str, module: Optional[str] = None) -> List[Tuple[str, int]]:
    """
    Return all production .java files under the repo/module, in stable path order.

    The count is always 1 so callers can reuse the same (path, count) shape as
    git-history ranked files.
    """
    root = Path(repo_path)
    search_root = root / module if module else root
    files: List[Tuple[str, int]] = []
    for path in sorted(search_root.glob("**/src/main/java/**/*.java")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        files.append((rel.as_posix(), 1))
    return files


def get_all_python_files(repo_path: str, module: Optional[str] = None) -> List[Tuple[str, int]]:
    """
    Return all production .py files under the repo/module, in stable path order.

    The count is always 1 so callers can reuse the same (path, count) shape as
    git-history ranked files.
    """
    root = Path(repo_path)
    search_root = root / module if module else root
    files: List[Tuple[str, int]] = []
    for current_root, dirnames, filenames in os.walk(search_root):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in _PYTHON_EXCLUDED_PARTS)
        current = Path(current_root)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = current / filename
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            if _is_production_python_relpath(rel):
                files.append((rel, 1))
    return files


def _path_is_under_module(path: str, module: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    module_prefix = str(module or "").strip("/").replace("\\", "/")
    return not module_prefix or normalized == module_prefix or normalized.startswith(f"{module_prefix}/")


def _is_production_python_relpath(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized.endswith(".py") or normalized.startswith("/"):
        return False
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return False
    if parts[0] in {"tests", "test"}:
        return False
    if set(parts) & _PYTHON_EXCLUDED_PARTS:
        return False
    name = parts[-1]
    if name == "__init__.py" or name.startswith("test_"):
        return False
    return True
