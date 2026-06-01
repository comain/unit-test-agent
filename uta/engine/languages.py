from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from uta.engine.targets import TargetRef


@dataclass(frozen=True)
class LanguageCapabilities:
    """Feature flags advertised by a language backend.

    Core workflow code uses these capabilities to decide which generation,
    coverage, mutation, and incremental paths are available.
    """

    supports_function_targets: bool
    supports_branch_coverage: bool
    supports_mutation: bool
    supports_incremental_diff_enforcement: bool
    supports_import_safety_hints: bool
    generated_tests_are_autopushable: bool


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
class RawTargetSelection:
    """Loose target input accepted from CLI, CI, manifests, and tests.

    Language adapters normalize this permissive shape into a strict TargetRef.
    """

    target: Optional[str] = None
    target_id: Optional[str] = None
    class_fqn: Optional[str] = None
    source_path: Optional[str] = None
    symbol: Optional[str] = None
    granularity: Optional[str] = None
    display_name: Optional[str] = None

    @classmethod
    def from_value(cls, value) -> "RawTargetSelection":
        if isinstance(value, RawTargetSelection):
            return value
        if isinstance(value, str):
            return cls(target=value)
        if isinstance(value, Mapping):
            return cls(
                target=value.get("target"),
                target_id=value.get("target_id") or value.get("targetId"),
                class_fqn=value.get("class_fqn") or value.get("classFqn"),
                source_path=value.get("source_path") or value.get("sourcePath"),
                symbol=value.get("symbol"),
                granularity=value.get("granularity") or value.get("target_granularity") or value.get("targetGranularity"),
                display_name=value.get("display_name") or value.get("displayName"),
            )
        raise TypeError(f"Unsupported target selection: {value!r}")

    def to_target_ref(
        self,
        *,
        language: str,
        target_id: str,
        display_name: str,
        granularity: str,
        source_path: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> TargetRef:
        return TargetRef(
            language=language,
            target_id=target_id,
            display_name=display_name,
            source_path=source_path,
            symbol=symbol,
            granularity=granularity,
        )


@dataclass(frozen=True)
class GeneratedTestPolicy:
    """Rules for where generated tests may be written for one language."""

    language: str
    allowed_test_roots: Sequence[str]
    autopushable: bool = True


@dataclass(frozen=True)
class PromptBundle:
    """Prompt template names used by a language backend."""

    language: str
    plan: str
    generate: str
    fix_compile: Optional[str] = None
    fix_coverage: Optional[str] = None
    fix_mutations: Optional[str] = None


class LanguageAdapter(Protocol):
    """Primary extension point for a backend language.

    Adapters expose detection, target normalization, generated-test policy, and
    prompt selection so the rest of UTA can operate on normalized targets.
    """

    language: str

    def capabilities(self) -> LanguageCapabilities:
        ...

    def detect(self, repo_path: Path, changed_paths: Optional[Sequence[str]] = None) -> DetectionSignal:
        ...

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        ...

    def generated_test_policy(self, repo_path: Path, target: Optional[TargetRef]) -> GeneratedTestPolicy:
        ...

    def prompt_bundle(self) -> PromptBundle:
        ...


class UnsupportedLanguageError(ValueError):
    """Raised when no registered adapter supports the requested language."""

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
    """Registry of language adapters available to the current UTA runtime."""

    def __init__(self) -> None:
        self._languages = {}

    def register_language(self, adapter: LanguageAdapter) -> None:
        language = _normalize_language_name(adapter.language)
        if language in self._languages:
            raise ValueError(f"Language already registered: {language}")
        self._languages[language] = adapter

    def adapter_for(self, language: str) -> LanguageAdapter:
        normalized = _normalize_language_name(language)
        try:
            return self._languages[normalized]
        except KeyError:
            raise UnsupportedLanguageError(f"Unsupported language: {language}") from None

    def capabilities_for(self, language: str) -> LanguageCapabilities:
        return self.adapter_for(language).capabilities()

    def generated_test_policy(self, language: str, repo_path: Path, target: Optional[TargetRef]) -> GeneratedTestPolicy:
        return self.adapter_for(language).generated_test_policy(repo_path, target)

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
    ci_language: Optional[str] = None,
    explicit_language: Optional[str] = None,
    class_fqns: Optional[Sequence[str]] = None,
    targets: Optional[Sequence[str]] = None,
    changed_paths: Optional[Sequence[str]] = None,
) -> LanguageDecision:
    repo_path = Path(repo_path)
    if ci_language and _normalize_language_name(ci_language) != "auto":
        language = _normalize_language_name(ci_language)
        registry.adapter_for(language)
        return LanguageDecision(language=language, source="ci", reason="ci_language_parameter", candidates=(language,))

    if explicit_language and _normalize_language_name(explicit_language) != "auto":
        language = _normalize_language_name(explicit_language)
        registry.adapter_for(language)
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
            registry.adapter_for(language)
            return LanguageDecision(language=language, source="target", reason="target_shape", candidates=(language,))
        if len(target_languages) > 1:
            raise AmbiguousLanguageError(target_languages, target_values)

    changed_values = [str(path) for path in changed_paths or [] if path]
    if changed_values:
        changed_value_set = set(changed_values)
        changed_signals = []
        for language in registry.languages:
            signal = registry.adapter_for(language).detect(repo_path, changed_paths=changed_values)
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

    signals = [registry.adapter_for(language).detect(repo_path, changed_paths=()) for language in registry.languages]
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


def normalize_targets(adapter: LanguageAdapter, values: Iterable) -> List[TargetRef]:
    return [adapter.normalize_target(RawTargetSelection.from_value(value)) for value in values or []]


def default_registry() -> BackendRegistry:
    from uta.language.java.adapter import JavaLanguageAdapter
    from uta.language.python.adapter import PythonLanguageAdapter

    registry = BackendRegistry()
    registry.register_language(JavaLanguageAdapter())
    registry.register_language(PythonLanguageAdapter())
    return registry
