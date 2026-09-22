"""Neutral target helpers for lightweight enforcement."""

from __future__ import annotations

from pathlib import Path
import re


def target_source_path(value: str) -> str:
    return str(value or "").split("::", 1)[0].strip().replace("\\", "/")


def target_payload(source_path: str) -> dict[str, str]:
    return {
        "language": "python",
        "target_id": f"pyfile:{source_path}",
        "display_name": source_path,
        "source_path": source_path,
        "granularity": "file",
    }


def safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path).strip("_") or "target"


def source_stem(source_path: str) -> str:
    return Path(source_path).stem.lower()
