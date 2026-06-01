from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, runtime_checkable


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
) -> ProjectSummaryProvider:
    normalized = str(language or "").strip().lower()
    if normalized == "java":
        if graph is None:
            raise ValueError("Java project summary provider requires a parsed CodeGraph")
        from uta.language.java.project_summary import JavaProjectSummaryProvider

        return JavaProjectSummaryProvider(str(repo_path), graph, module)
    if normalized == "python":
        from uta.language.python.project_summary import PythonProjectSummaryProvider

        return PythonProjectSummaryProvider(repo_path, max_files=max_files)
    raise ValueError(f"Unsupported project summary language: {language}")
