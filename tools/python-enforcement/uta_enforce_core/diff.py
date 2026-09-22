"""Neutral git diff helpers for lightweight enforcement."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Sequence


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git_output(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(["git", *args], cwd=str(repo), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def changed_paths(repo: Path, base_ref: str) -> list[str]:
    output = git_output(repo, "diff", "--name-only", f"{base_ref}...HEAD")
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def changed_production_python_files(repo: Path, base_ref: str) -> list[str]:
    return [path for path in changed_paths(repo, base_ref) if is_production_python_path(path)]


def changed_lines_by_file(repo: Path, base_ref: str, paths: Sequence[str]) -> dict[str, list[int]]:
    return {path: sorted(parse_added_diff_lines(git_output(repo, "diff", "--unified=0", f"{base_ref}...HEAD", "--", path))) for path in paths}


def parse_added_diff_lines(diff_text: str) -> set[int]:
    lines: set[int] = set()
    current_line = None
    for raw_line in str(diff_text or "").splitlines():
        hunk = HUNK_RE.match(raw_line)
        if hunk:
            current_line = int(hunk.group(1))
            continue
        if current_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.add(current_line)
            current_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            current_line += 1
    return lines


def is_production_python_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    parts = normalized.split("/")
    if not normalized.endswith(".py"):
        return False
    # Documentation generators are not production targets, including hidden
    # scripts such as docs/.create_porridge_merge_prd.py (repair task 197).
    if any(part in {"doc", "docs", "tests", "test", ".venv", "venv", ".tox", ".nox", "site-packages", "__pycache__"} for part in parts):
        return False
    name = Path(normalized).name
    return name != "__init__.py" and not name.startswith("test_")
