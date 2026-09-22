"""Dependency-light public models for Python verification.

The request/config/result value objects that the verification orchestrator,
its focused execution modules, the UTA mutation-context adapter and tests all
speak. They live here rather than in ``runner`` because ``mutation_context``
needs them: with the models in a module that depends on neither side, the
``runner`` <-> ``mutation_context`` logical import cycle disappears instead of
being hidden behind a function-local import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

from uta.enforcement.verification import verification_task_status
from uta.language.python.verification.evidence import CoverageSummary, MutationSummary


RunCommand = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class CommandEvidence:
    name: str
    command: List[str]
    exit_code: int
    elapsed_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class PythonRuntimeConfig:
    python_bin: str = "python3"
    python2_bin: Optional[str] = None
    mutmut_bin: str = "mutmut"
    python2_mutmut_bin: Optional[str] = None
    setup_command: Sequence[str] = ()
    dependency_overlay_enabled: bool = True
    environment_profile: str = "default"
    timeout_seconds: int = 1800
    artifact_dir: str = ".uta_cache/python"
    dependency_fingerprints: Dict[str, str] = field(default_factory=dict)
    cache_key: str = "python-env:default"
    config_sources: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PythonVerificationResult:
    status: str
    reason_code: str
    tests_pass: bool = False
    coverage: Optional[CoverageSummary] = None
    mutation: Optional[MutationSummary] = None
    commands: List[CommandEvidence] = field(default_factory=list)
    message: str = ""
    setup_status: str = "skipped"
    environment_profile: str = "default"
    dependency_fingerprints: Dict[str, str] = field(default_factory=dict)
    cache_key: str = ""

    def as_result_fields(self) -> Dict[str, Any]:
        mutation_score = self.mutation.rate if self.mutation else None
        coverage_rate = self.coverage.rate if self.coverage else 0.0
        failure_message = (self.message or self.reason_code) if self.status != "passed" else None
        return {
            "status": _task_status_for_verification(self),
            "coverage": coverage_rate,
            "tests_pass": self.tests_pass,
            "mutation_score": mutation_score,
            "surviving_mutants": self.mutation.survived if self.mutation else None,
            "total_mutants": self.mutation.generated if self.mutation else None,
            "killed_mutants": self.mutation.killed if self.mutation else None,
            "no_coverage_mutants": self.mutation.no_coverage if self.mutation else None,
            "no_tests_mutants": self.mutation.no_tests if self.mutation else None,
            "timeout_mutants": self.mutation.timeout if self.mutation else None,
            "suspicious_mutants": self.mutation.suspicious if self.mutation else None,
            "verification_status": self.status,
            "verification_reason": self.reason_code,
            "verification_message": self.message,
            # Task persistence reads these neutral fields. Keeping the detailed
            # verifier message only under verification_message made failed
            # Python rows render with a blank Error column.
            "error": failure_message,
            "last_error": failure_message,
            "verification_setup": {
                "setup_status": self.setup_status,
                "environment_profile": self.environment_profile,
                "dependency_fingerprints": dict(self.dependency_fingerprints),
                "cache_key": self.cache_key,
            },
            "dependency_fingerprints": dict(self.dependency_fingerprints),
            "verification_cache_key": self.cache_key,
            "verification_commands": [command.as_dict() for command in self.commands],
            "coverage_summary": self.coverage.as_dict() if self.coverage else None,
            "mutation_summary": self.mutation.as_dict() if self.mutation else None,
        }



def _task_status_for_verification(result: PythonVerificationResult) -> str:
    return verification_task_status(result.status, result.reason_code)


__all__ = [
    "CommandEvidence",
    "CoverageSummary",
    "MutationSummary",
    "PythonRuntimeConfig",
    "PythonVerificationResult",
    "RunCommand",
]
