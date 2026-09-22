from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from uta.shared import backends
from uta.shared.backends import BackendConstructionRequest
from uta.shared.ci_models import CiTriggerRequest
from uta.testgen.context import make_context_provider
from uta.testgen.project_summary import (
    ProjectSummaryArtifacts,
    make_project_summary_provider,
)


@dataclass
class _SyntheticContextProvider:
    repo_path: Path
    language: str = "synthetic"


class _SyntheticContextFactory:
    required_inputs = frozenset({"syntax_index"})

    def create(self, request: BackendConstructionRequest) -> _SyntheticContextProvider:
        assert request.require("syntax_index") == "ready"
        return _SyntheticContextProvider(request.repo_path)


@dataclass
class _SyntheticSummaryProvider:
    marker: str
    language: str = "synthetic"

    def sync(self) -> ProjectSummaryArtifacts:
        return ProjectSummaryArtifacts(
            self.marker, self.marker, self.marker, self.marker
        )


class _SyntheticSummaryFactory:
    required_inputs = frozenset({"summary_marker"})

    def create(self, request: BackendConstructionRequest) -> _SyntheticSummaryProvider:
        return _SyntheticSummaryProvider(str(request.require("summary_marker")))


class _SyntheticGenerationBackend:
    language = "synthetic"


class _SyntheticAdapter:
    language = "synthetic"


def _register_synthetic_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    module = __name__
    registrations = {
        "adapter": f"{module}:_SyntheticAdapter",
        "context_factory": f"{module}:_SyntheticContextFactory",
        "project_summary_factory": f"{module}:_SyntheticSummaryFactory",
        "generation_backend": f"{module}:_SyntheticGenerationBackend",
    }
    for role, target in registrations.items():
        monkeypatch.setitem(
            backends._PROVIDERS,
            ("synthetic", role),
            target,
        )


def test_ci_request_accepts_a_registered_language_without_a_model_change(
    monkeypatch: pytest.MonkeyPatch,
):
    _register_synthetic_backends(monkeypatch)

    request = CiTriggerRequest(
        app_name="synthetic-app",
        git_url="git@example.test:group/repo.git",
        branch="feature/test",
        language="synthetic",
    )

    assert request.language == "synthetic"


def test_ci_request_rejects_a_well_formed_but_unregistered_language():
    with pytest.raises(ValueError, match="registered language"):
        CiTriggerRequest(
            app_name="unsupported-app",
            git_url="git@example.test:group/repo.git",
            branch="feature/test",
            language="notregistered",
        )


def test_backend_factories_own_asymmetric_construction_inputs(tmp_path, monkeypatch):
    _register_synthetic_backends(monkeypatch)

    context = make_context_provider(
        "synthetic",
        tmp_path,
        backend_inputs={"syntax_index": "ready"},
    )
    summary = make_project_summary_provider(
        "synthetic",
        tmp_path,
        backend_inputs={"summary_marker": "summary"},
    )

    assert context == _SyntheticContextProvider(tmp_path.resolve())
    assert summary.sync().repo_summary_abs == "summary"


def test_backend_factory_reports_its_missing_construction_input(tmp_path, monkeypatch):
    _register_synthetic_backends(monkeypatch)

    with pytest.raises(ValueError, match="syntax_index"):
        make_context_provider("synthetic", tmp_path)


def test_generation_backend_uses_the_same_registry_as_other_roles(monkeypatch):
    from uta.shared.backends import make_backend

    _register_synthetic_backends(monkeypatch)

    assert isinstance(
        make_backend("synthetic", "generation_backend"), _SyntheticGenerationBackend
    )
