from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable
from uta.shared.backends import BackendConstructionRequest, UnknownBackendError, construct_backend


@dataclass(frozen=True)
class ProjectSummaryArtifacts:
    """Paths produced by syncing cached project summary artifacts."""

    repo_summary_abs: str
    context_summary_abs: str
    test_guidance_abs: str
    compile_facts_abs: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "repo_summary_abs": self.repo_summary_abs,
            "context_summary_abs": self.context_summary_abs,
            "test_guidance_abs": self.test_guidance_abs,
            "compile_facts_abs": self.compile_facts_abs,
        }


@runtime_checkable
class ProjectSummaryProvider(Protocol):
    """Creates or refreshes project-level context artifacts for one language."""

    language: str

    def sync(self) -> ProjectSummaryArtifacts:
        ...


def make_project_summary_provider(
    language: str,
    repo_path: str | Path,
    *,
    graph: Optional[Any] = None,
    module: Optional[str] = None,
    max_files: int = 500,
    backend_inputs: Optional[Mapping[str, Any]] = None,
) -> ProjectSummaryProvider:
    inputs = {"graph": graph, "module": module, "max_files": max_files}
    inputs.update(dict(backend_inputs or {}))
    try:
        return construct_backend(
            language,
            "project_summary_factory",
            BackendConstructionRequest(Path(repo_path).resolve(), inputs),
        )
    except UnknownBackendError as exc:
        raise ValueError(f"Unsupported project summary language: {language}") from exc


__all__ = [
    "ProjectSummaryArtifacts",
    "ProjectSummaryProvider",
    "make_project_summary_provider",
]
