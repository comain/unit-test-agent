from pathlib import Path

import pytest

from uta.engine.languages import (
    AmbiguousLanguageError,
    BackendRegistry,
    DetectionSignal,
    GeneratedTestPolicy,
    LanguageAdapter,
    LanguageCapabilities,
    RawTargetSelection,
    UnsupportedLanguageError,
    default_registry,
    resolve_language,
)


class ToyAdapter(LanguageAdapter):
    language = "toy"

    def capabilities(self):
        return LanguageCapabilities(
            supports_function_targets=True,
            supports_branch_coverage=False,
            supports_mutation=False,
            supports_incremental_diff_enforcement=False,
            supports_import_safety_hints=False,
            generated_tests_are_autopushable=False,
        )

    def detect(self, repo_path: Path, changed_paths=None):
        return DetectionSignal(self.language, 1, ["toy.marker"]) if (repo_path / "toy.marker").exists() else DetectionSignal(self.language, 0, [])

    def normalize_target(self, raw):
        target = raw.target or raw.target_id or raw.symbol or "toy:all"
        return raw.to_target_ref(
            language=self.language,
            target_id=f"toy:{target}",
            display_name=str(target),
            granularity="symbol",
            symbol=str(target),
        )

    def generated_test_policy(self, repo_path: Path, target):
        return GeneratedTestPolicy(language=self.language, allowed_test_roots=("tests/toy_generated",))


def test_default_registry_contains_java_and_python():
    registry = default_registry()

    assert registry.adapter_for("java").language == "java"
    assert registry.adapter_for("python").language == "python"
    assert registry.capabilities_for("python").supports_function_targets is True
    assert registry.generated_test_policy("python", Path("/repo"), None).allowed_test_roots


def test_registry_rejects_duplicate_and_unknown_language():
    registry = BackendRegistry()
    registry.register_language(ToyAdapter())

    with pytest.raises(ValueError, match="already registered"):
        registry.register_language(ToyAdapter())
    with pytest.raises(UnsupportedLanguageError):
        registry.adapter_for("python")


def test_fake_third_language_can_register_and_normalize_target(tmp_path):
    registry = BackendRegistry()
    registry.register_language(ToyAdapter())

    target = registry.adapter_for("toy").normalize_target(RawTargetSelection(target="widget"))

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
