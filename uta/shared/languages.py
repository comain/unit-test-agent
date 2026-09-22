"""Which language a repository is, and what a loose target selection means.

`LanguageAdapter` used to be one Protocol that answered everything a backend
could be asked: detection, target normalization, feature flags, generated-test
roots and prompt names -- and, on the concrete adapters that implemented it,
batch generation, workspace policy, and the testgen cycle binding as well. A
caller that needed nothing but `is_production_source_path` still depended on a
type that promised agent turns, which is why the umbrella shows up as
type-level back-edges rather than as a dependency-light neutral contract.

It is replaced here by ports named for the question each one answers:

* `LanguageDetector` -- does this repository, or this path, belong to me?
* `TargetNormalizer` -- what strict `TargetRef` does this loose selection mean?
* `PromptBundleProvider` -- which prompt templates does this backend use?

Two of the umbrella's methods are gone rather than split. `capabilities()` and
`generated_test_policy()` had no reader anywhere in `uta/`, `tools/` or
`scripts/`: the only callers were this registry's own passthroughs, and the
only callers of *those* were two tests. Where generated tests may be written is
decided by each backend's `WorkspacePolicy` and its test-artifact writer, not
by the declaration that used to sit here, so deleting it moves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Protocol, Sequence, Tuple, runtime_checkable

from uta.shared.targets import RawTargetSelection, TargetRef
from uta.shared.backends import make_all


@dataclass(frozen=True)
class DetectionSignal:
    """Evidence that a repository or changed path set belongs to a language."""

    language: str
    score: int
    reasons: Sequence[str]


@dataclass(frozen=True)
class LanguageDecision:
    """Resolved language choice and the source of that decision."""

    language: str
    source: str
    reason: str
    candidates: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "language": self.language,
            "source": self.source,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }




@dataclass(frozen=True)
class PromptBundle:
    """Prompt template names used by a language backend."""

    language: str
    generate: str
    # Optional plan-stage prompt. ``None`` means the backend has no plan stage
    # (Python generates directly); Java names its plan prompt here.
    plan: Optional[str] = None
    fix_compile: Optional[str] = None
    fix_coverage: Optional[str] = None
    fix_mutations: Optional[str] = None


@runtime_checkable
class LanguageDetector(Protocol):
    """Does this repository, or this path, belong to one language?

    Everything it needs is a path and a list of changed paths, so it can be
    consumed by the deterministic lane -- `uta.enforcement.diff` and
    `uta.shared.source_selection` both want only this port.
    """

    language: str

    def detect(self, repo_path: Path, changed_paths: Optional[Sequence[str]] = None) -> DetectionSignal:
        ...

    def is_production_source_path(self, path: str) -> bool:
        """True when a repo-relative path is production source for this language."""
        ...


@runtime_checkable
class TargetNormalizer(Protocol):
    """What strict `TargetRef` does one loose target selection mean?

    The permissive shapes CLI, CI, manifests, and stored rows use are only
    interpretable by the language that owns the target vocabulary.
    """

    language: str

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        ...


@runtime_checkable
class PromptBundleProvider(Protocol):
    """Which prompt templates does one language backend use?

    Kept as its own port rather than folded into either of the two above: the
    bundle is data with a real consumer (`uta.testgen.repair` and the Java
    phase ports), and each language publishes it from a module of its own.
    """

    language: str

    def prompt_bundle(self) -> PromptBundle:
        ...


class LanguageBackend(LanguageDetector, TargetNormalizer, PromptBundleProvider, Protocol):
    """The set of narrow ports one registered backend has to satisfy.

    Deliberately nothing but the union of the three named ports above. It is
    what `BackendRegistry` stores, not a place to add a fourth responsibility:
    a consumer that needs one of the ports should ask for that port, through
    `detector_for` or `normalizer_for`, and depend on nothing else.
    """


class UnsupportedLanguageError(ValueError):
    """Raised when no registered backend supports the requested language."""

    pass


class AmbiguousLanguageError(ValueError):
    """Raised when auto-detection finds multiple plausible languages."""

    def __init__(self, candidates: Iterable[str], reasons: Iterable[str]):
        self.candidates = tuple(sorted(candidates))
        details = ", ".join(reasons)
        super().__init__(
            "Ambiguous project language; provide --language. "
            f"Candidates: {', '.join(self.candidates)}. Reasons: {details}"
        )


class BackendRegistry:
    """Registry of language backends available to the current UTA runtime."""

    def __init__(self) -> None:
        self._languages: Dict[str, LanguageBackend] = {}

    def register_language(self, backend: LanguageBackend) -> None:
        language = _normalize_language_name(backend.language)
        if language in self._languages:
            raise ValueError(f"Language already registered: {language}")
        self._languages[language] = backend

    def adapter_for(self, language: str) -> LanguageBackend:
        normalized = _normalize_language_name(language)
        try:
            return self._languages[normalized]
        except KeyError:
            raise UnsupportedLanguageError(f"Unsupported language: {language}") from None

    def detector_for(self, language: str) -> LanguageDetector:
        """The detection port only -- for callers that classify paths or repos."""
        return self.adapter_for(language)

    def normalizer_for(self, language: str) -> TargetNormalizer:
        """The normalization port only -- for callers that read target input."""
        return self.adapter_for(language)

    def prompt_bundle(self, language: str) -> PromptBundle:
        return self.adapter_for(language).prompt_bundle()

    @property
    def languages(self) -> Tuple[str, ...]:
        return tuple(sorted(self._languages))


def _normalize_language_name(language: Optional[str]) -> str:
    return str(language or "").strip().lower()


def _target_implies_python(target: str) -> bool:
    value = str(target or "")
    return value.startswith(("pyfile:", "pysymbol:")) or ".py" in value.split("::", 1)[0]


def _target_implies_java(target: str) -> bool:
    value = str(target or "")
    return "." in value and not _target_implies_python(value)


def resolve_language(
    registry: BackendRegistry,
    repo_path: Path,
    *,
    rdc_language: Optional[str] = None,
    explicit_language: Optional[str] = None,
    class_fqns: Optional[Sequence[str]] = None,
    targets: Optional[Sequence[str]] = None,
    changed_paths: Optional[Sequence[str]] = None,
) -> LanguageDecision:
    repo_path = Path(repo_path)
    if rdc_language and _normalize_language_name(rdc_language) != "auto":
        language = _normalize_language_name(rdc_language)
        registry.detector_for(language)
        return LanguageDecision(language=language, source="rdc", reason="rdc_language_parameter", candidates=(language,))

    if explicit_language and _normalize_language_name(explicit_language) != "auto":
        language = _normalize_language_name(explicit_language)
        registry.detector_for(language)
        return LanguageDecision(language=language, source="cli", reason="explicit_language", candidates=(language,))

    if class_fqns:
        return LanguageDecision(language="java", source="target", reason="class_fqn", candidates=("java",))

    target_values = [str(target) for target in targets or [] if target]
    if target_values:
        target_languages = set()
        for target in target_values:
            if _target_implies_python(target):
                target_languages.add("python")
            elif _target_implies_java(target):
                target_languages.add("java")
        if len(target_languages) == 1:
            language = next(iter(target_languages))
            registry.detector_for(language)
            return LanguageDecision(language=language, source="target", reason="target_shape", candidates=(language,))
        if len(target_languages) > 1:
            raise AmbiguousLanguageError(target_languages, target_values)

    changed_values = [str(path) for path in changed_paths or [] if path]
    if changed_values:
        changed_value_set = set(changed_values)
        changed_signals = []
        for language in registry.languages:
            signal = registry.detector_for(language).detect(repo_path, changed_paths=changed_values)
            changed_reasons = [reason for reason in signal.reasons if reason in changed_value_set]
            if changed_reasons:
                changed_signals.append(DetectionSignal(signal.language, len(changed_reasons), changed_reasons))
        if len(changed_signals) == 1:
            signal = changed_signals[0]
            return LanguageDecision(language=signal.language, source="changed_files", reason=";".join(signal.reasons), candidates=(signal.language,))
        if len(changed_signals) > 1:
            raise AmbiguousLanguageError(
                [signal.language for signal in changed_signals],
                [reason for signal in changed_signals for reason in signal.reasons],
            )

    signals = [registry.detector_for(language).detect(repo_path, changed_paths=()) for language in registry.languages]
    marker_signals = [signal for signal in signals if signal.score > 0]
    if len(marker_signals) == 1:
        signal = marker_signals[0]
        return LanguageDecision(language=signal.language, source="repo_marker", reason=";".join(signal.reasons), candidates=(signal.language,))
    if len(marker_signals) > 1:
        raise AmbiguousLanguageError(
            [signal.language for signal in marker_signals],
            [reason for signal in marker_signals for reason in signal.reasons],
        )

    raise UnsupportedLanguageError(f"No supported language detected in {repo_path}")


def default_registry() -> BackendRegistry:
    registry = BackendRegistry()
    for backend in make_all("adapter"):
        registry.register_language(backend)
    return registry


__all__ = [
    "AmbiguousLanguageError",
    "BackendRegistry",
    "DetectionSignal",
    "LanguageBackend",
    "LanguageDecision",
    "LanguageDetector",
    "PromptBundle",
    "PromptBundleProvider",
    "RawTargetSelection",
    "TargetNormalizer",
    "UnsupportedLanguageError",
    "default_registry",
    "resolve_language",
]
