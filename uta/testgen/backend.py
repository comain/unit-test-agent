"""Language backend contract consumed by the test-generation graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol


@dataclass(frozen=True)
class GenerationCycleBinding:
    """Language-owned preparation bound to the neutral durable runner."""

    initial_state: Dict[str, Any]
    backend: Any
    runner: Any


class TestGenerationBackend(Protocol):
    """Operations a language supplies to the neutral generation graph."""

    def prepare_workspace(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def baseline_validate(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def select_targets(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def prepare_context(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def select_next_target(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def deliver_target(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def finalize(self, state: Dict[str, Any]) -> Dict[str, Any]:
        ...


__all__ = ["GenerationCycleBinding", "TestGenerationBackend"]
