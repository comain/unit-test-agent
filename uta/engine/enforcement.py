from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ValidationVerdict:
    """Result of validating an enforcement evidence payload."""

    passed: bool
    reason_code: str
    message: str = ""


@runtime_checkable
class EnforcementCore(Protocol):
    """Language enforcement contract for local and CI quality gates.

    A concrete core produces one evidence payload and validates that payload
    against the expected commit and language-specific gate semantics.
    """

    language: str

    def run(self, *, repo_path: Path, **kwargs: Any) -> Dict[str, Any]:
        ...

    def validate(self, evidence: Mapping[str, Any], *, expected_head: Optional[str] = None) -> ValidationVerdict:
        ...
