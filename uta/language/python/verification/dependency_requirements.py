"""Backward-compatible facade for dependency requirements; delegates to uta_py_enforce."""

from __future__ import annotations

from uta_py_enforce.dependency_overlay import (
    flatten_manifest,
    install_manifest_requirements,
    manifest_digest,
    manifest_requirement_lines,
    nearest_requirements_manifest,
    prepare_dependency_overlay,
)

__all__ = [
    "flatten_manifest",
    "install_manifest_requirements",
    "manifest_digest",
    "manifest_requirement_lines",
    "nearest_requirements_manifest",
    "prepare_dependency_overlay",
]
