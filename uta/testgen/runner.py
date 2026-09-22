from __future__ import annotations

from typing import Optional

from uta.testgen.batch import BatchGenerationRequest, BatchGenerationResult, BatchGenerator
from uta.shared.languages import BackendRegistry


class BatchGeneratorConfigurationError(RuntimeError):
    """Raised when a language adapter exposes an invalid batch generator."""


def batch_generator_for(
    language: str,
    *,
    registry: Optional[BackendRegistry] = None,
) -> BatchGenerator:
    """Resolve a language's batch generator through backends or registered adapter."""
    if registry is not None:
        adapter = registry.adapter_for(language)
        generator_factory = getattr(adapter, "batch_generator", None)
        if not callable(generator_factory):
            raise BatchGeneratorConfigurationError(
                f"Language adapter {adapter.language!r} does not provide a batch generator"
            )
        generator = generator_factory()
    else:
        from uta.shared.backends import UnknownBackendError, make_backend

        try:
            generator = make_backend(language, "batch_generator")
        except UnknownBackendError as exc:
            raise BatchGeneratorConfigurationError(
                f"No batch generator registered for language {language!r}"
            ) from exc

    requested_language = _normalize_language(language)
    generator_language = _normalize_language(getattr(generator, "language", ""))
    if generator_language != requested_language:
        prefix = f"Language adapter {getattr(adapter, 'language', requested_language)!r} " if registry is not None else ""
        raise BatchGeneratorConfigurationError(
            f"{prefix}returned a batch generator for {generator_language or '<missing>'!r}"
        )
    if not callable(getattr(generator, "run", None)):
        prefix = f"Language adapter {getattr(adapter, 'language', requested_language)!r} " if registry is not None else ""
        raise BatchGeneratorConfigurationError(
            f"{prefix}returned a batch generator without run()"
        )
    return generator


def run_batch_generation(
    request: BatchGenerationRequest,
    *,
    registry: Optional[BackendRegistry] = None,
) -> BatchGenerationResult:
    """Execute a batch request without exposing its language backend to callers."""
    if not isinstance(request, BatchGenerationRequest):
        raise TypeError("run_batch_generation requires BatchGenerationRequest")
    from uta.shared.workspace_rules import validate_workspace_rules

    validate_workspace_rules(request.repo_path)
    return batch_generator_for(request.language, registry=registry).run(request)


def _normalize_language(language: str) -> str:
    return str(language or "").strip().lower()
