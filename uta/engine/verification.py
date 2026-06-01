from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Protocol

from uta.engine.targets import TargetRef


class VerificationResult(Protocol):
    """Minimum result shape returned by language verification runners."""

    status: str
    reason_code: str

    def as_result_fields(self) -> dict:
        ...


class VerificationRunner(Protocol):
    """Runs tests, coverage, and mutation verification for one target."""

    language: str

    def verify(self, repo_path: Path, target: TargetRef, **kwargs: Any) -> VerificationResult:
        ...


class VerificationRunnerRegistry:
    """Registry that selects the verification runner for a language."""

    def __init__(self, runners: Iterable[VerificationRunner]) -> None:
        self._runners = {runner.language: runner for runner in runners}

    def runner_for(self, language: str) -> VerificationRunner:
        normalized = str(language or "").strip().lower()
        try:
            return self._runners[normalized]
        except KeyError:
            raise ValueError(f"Unsupported verification language: {language}") from None

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))


def default_verification_registry() -> VerificationRunnerRegistry:
    from uta.language.java.verification import JavaVerificationRunner
    from uta.language.python.verification import PythonVerificationRunner

    return VerificationRunnerRegistry((JavaVerificationRunner(), PythonVerificationRunner()))
