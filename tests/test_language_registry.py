from pathlib import Path

import pytest

from uta.shared.languages import (
    AmbiguousLanguageError,
    BackendRegistry,
    DetectionSignal,
    LanguageBackend,
    LanguageDetector,
    PromptBundle,
    RawTargetSelection,
    TargetNormalizer,
    UnsupportedLanguageError,
    default_registry,
    resolve_language,
)


class ToyBackend(LanguageBackend):
    language = "toy"

    def detect(self, repo_path: Path, changed_paths=None):
        return DetectionSignal(self.language, 1, ["toy.marker"]) if (repo_path / "toy.marker").exists() else DetectionSignal(self.language, 0, [])

    def is_production_source_path(self, path: str) -> bool:
        return str(path or "").endswith(".toy")

    def normalize_target(self, raw):
        target = raw.target or raw.target_id or raw.symbol or "toy:all"
        return raw.to_target_ref(
            language=self.language,
            target_id=f"toy:{target}",
            display_name=str(target),
            granularity="symbol",
            symbol=str(target),
        )

    def prompt_bundle(self) -> PromptBundle:
        return PromptBundle(language=self.language, generate="toy_generate_test")


def test_default_registry_contains_java_and_python():
    registry = default_registry()

    assert registry.adapter_for("java").language == "java"
    assert registry.adapter_for("python").language == "python"
    assert registry.prompt_bundle("python").generate == "python_generate_test"
    assert registry.prompt_bundle("java").generate == "generate_test"


def test_registry_rejects_duplicate_and_unknown_language():
    registry = BackendRegistry()
    registry.register_language(ToyBackend())

    with pytest.raises(ValueError, match="already registered"):
        registry.register_language(ToyBackend())
    with pytest.raises(UnsupportedLanguageError):
        registry.adapter_for("python")


def test_fake_third_language_can_register_and_normalize_target(tmp_path):
    registry = BackendRegistry()
    registry.register_language(ToyBackend())

    target = registry.normalizer_for("toy").normalize_target(RawTargetSelection(target="widget"))

    assert target.language == "toy"
    assert target.target_id == "toy:widget"
    assert target.granularity == "symbol"


def test_language_detection_uses_markers_targets_and_explicit_override(tmp_path):
    registry = default_registry()
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='mixed'\n")

    decision = resolve_language(registry, tmp_path, explicit_language="python")
    assert decision.language == "python"
    assert decision.source == "cli"

    decision = resolve_language(registry, tmp_path, class_fqns=["com.example.Foo"])
    assert decision.language == "java"
    assert decision.source == "target"

    decision = resolve_language(registry, tmp_path, targets=["jobs/a.py::run"])
    assert decision.language == "python"
    assert decision.source == "target"


def test_language_detection_reports_ambiguous_mixed_repo(tmp_path):
    registry = default_registry()
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "requirements.txt").write_text("pytest\n")

    with pytest.raises(AmbiguousLanguageError) as exc:
        resolve_language(registry, tmp_path)

    assert set(exc.value.candidates) == {"java", "python"}
    assert "--language" in str(exc.value)


def test_language_detection_uses_changed_paths_to_disambiguate_mixed_repo(tmp_path):
    registry = default_registry()
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='mixed'\n")

    decision = resolve_language(registry, tmp_path, changed_paths=["jobs/forecast.py"])

    assert decision.language == "python"
    assert decision.source == "changed_files"


def test_language_detection_can_use_changed_file_suffixes(tmp_path):
    registry = default_registry()

    decision = resolve_language(registry, tmp_path, changed_paths=["jobs/forecast.py"])

    assert decision.language == "python"
    assert decision.source == "changed_files"


def test_registry_hands_out_one_narrow_port_at_a_time():
    """A caller that classifies paths gets the detector, not a batch generator."""
    registry = BackendRegistry()
    registry.register_language(ToyBackend())

    detector = registry.detector_for("toy")
    normalizer = registry.normalizer_for("toy")

    assert isinstance(detector, LanguageDetector)
    assert isinstance(normalizer, TargetNormalizer)
    assert detector.is_production_source_path("widget.toy") is True
    assert detector.is_production_source_path("widget.java") is False


def test_narrow_accessors_reject_an_unregistered_language():
    registry = BackendRegistry()
    registry.register_language(ToyBackend())

    with pytest.raises(UnsupportedLanguageError):
        registry.detector_for("java")
    with pytest.raises(UnsupportedLanguageError):
        registry.normalizer_for("java")


def test_the_umbrella_adapter_protocol_is_retired():
    """`LanguageAdapter` mixed detection with policy, prompts, and batch generation.

    The registry no longer publishes the feature-flag and generated-test-root
    passthroughs either: nothing under `uta/` ever read them, so they were
    surface without a consumer rather than a capability.
    """
    import uta.shared.languages as languages

    assert not hasattr(languages, "LanguageAdapter")
    assert not hasattr(languages, "LanguageCapabilities")
    assert not hasattr(languages, "GeneratedTestPolicy")
    assert not hasattr(BackendRegistry, "capabilities_for")
    assert not hasattr(BackendRegistry, "generated_test_policy")


def test_language_backends_expose_no_generation_or_workspace_methods():
    """Detection and normalization do not drag batch generation in with them."""
    registry = default_registry()

    for language in ("java", "python"):
        backend = registry.adapter_for(language)
        assert not hasattr(backend, "capabilities")
        assert not hasattr(backend, "generated_test_policy")
        assert not hasattr(backend, "workspace_policy")
        assert not hasattr(backend, "batch_generator")
        assert not hasattr(backend, "test_generation_backend")
