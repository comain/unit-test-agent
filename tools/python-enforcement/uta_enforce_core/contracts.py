"""Language-neutral immutable enforcement contracts and data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


class EnforcementStatus(str, Enum):
    """Normalized enforcement outcome status."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    UNSUPPORTED = "unsupported"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class QualityGates:
    """Quality gate thresholds for coverage and mutation."""

    diff_coverage_min: float | None = None
    diff_mutation_min: float | None = None


@dataclass(frozen=True)
class RuntimeSelection:
    """Language runtime configuration for enforcement execution."""

    python_executable: str | None = None
    dependency_overlay_enabled: bool = True
    timeout_seconds: int = 1800
    total_timeout_seconds: int = 7200
    syntax_version: str | None = None


@dataclass(frozen=True)
class EnforcementTarget:
    """Target specification for language-neutral enforcement."""

    language: str
    target_id: str
    source_path: str
    test_paths: tuple[str, ...] = ()
    display_name: str = ""
    granularity: str = "file"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.test_paths, list):
            object.__setattr__(self, "test_paths", tuple(self.test_paths))
        if isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class EnforcementRequest:
    """Sole neutral request carrying parameters for an enforcement run."""

    repo_path: Path
    language: str
    targets: tuple[EnforcementTarget, ...]
    test_paths: tuple[str, ...] = ()
    base_ref: str = "origin/master"
    quality_gates: QualityGates = field(default_factory=QualityGates)
    runtime: RuntimeSelection = field(default_factory=RuntimeSelection)

    def __post_init__(self) -> None:
        if isinstance(self.repo_path, str):
            object.__setattr__(self, "repo_path", Path(self.repo_path))
        if isinstance(self.targets, list):
            object.__setattr__(self, "targets", tuple(self.targets))
        if isinstance(self.test_paths, list):
            object.__setattr__(self, "test_paths", tuple(self.test_paths))


@dataclass(frozen=True)
class EnforcementCapabilities:
    """Capabilities declared by a language enforcement binding."""

    language: str
    supports_coverage: bool = True
    supports_mutation: bool = True
    supported_runtime_versions: tuple[str, ...] = ()
    accepts_sampling_policy: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.supported_runtime_versions, list):
            object.__setattr__(
                self,
                "supported_runtime_versions",
                tuple(self.supported_runtime_versions),
            )


@dataclass(frozen=True)
class ProgressEvent:
    """Progress update emitted during enforcement."""

    stage: str
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetEnforcementResult:
    """Enforcement result for a single target."""

    target: EnforcementTarget
    status: EnforcementStatus
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnforcementResult:
    """Overall enforcement execution result."""

    status: EnforcementStatus
    evidence: Mapping[str, Any] = field(default_factory=dict)
    target_results: tuple[TargetEnforcementResult, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.target_results, list):
            object.__setattr__(
                self, "target_results", tuple(self.target_results)
            )


@runtime_checkable
class MutationSelectionPolicy(Protocol):
    """Optional mutation selection policy, e.g. for CI sampling."""

    def select_mutants(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        target_context: Mapping[str, Any] | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class CommandRunner(Protocol):
    """Protocol for executing sub-process commands safely."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class EnforcementInvocationContext:
    """Context injected into an enforcement run."""

    run_command: CommandRunner
    is_cancelled: Callable[[], bool] = lambda: False
    on_progress: Callable[[ProgressEvent], None] = lambda event: None
    sampling_policy: MutationSelectionPolicy | None = None


@runtime_checkable
class EnforcementLanguageBinding(Protocol):
    """Protocol implemented by language-specific enforcement bindings."""

    language: str

    def capabilities(self) -> EnforcementCapabilities: ...

    def enforce(
        self,
        request: EnforcementRequest,
        context: EnforcementInvocationContext,
    ) -> EnforcementResult: ...
