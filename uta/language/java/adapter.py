from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from uta.engine.languages import DetectionSignal, GeneratedTestPolicy, LanguageCapabilities, PromptBundle, RawTargetSelection
from uta.engine.targets import TargetIdentity, TargetRef


class JavaLanguageAdapter:
    language = "java"

    def capabilities(self) -> LanguageCapabilities:
        return LanguageCapabilities(
            supports_function_targets=False,
            supports_branch_coverage=True,
            supports_mutation=True,
            supports_incremental_diff_enforcement=True,
            supports_import_safety_hints=False,
            generated_tests_are_autopushable=True,
        )

    def detect(self, repo_path: Path, changed_paths: Optional[Sequence[str]] = None) -> DetectionSignal:
        reasons = []
        for marker in ("pom.xml", "build.gradle", "settings.gradle"):
            if (repo_path / marker).exists():
                reasons.append(marker)
        if (repo_path / "src" / "main" / "java").exists():
            reasons.append("src/main/java")
        if changed_paths:
            reasons.extend(path for path in changed_paths if str(path).endswith(".java"))
        return DetectionSignal(self.language, len(reasons), reasons)

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        class_fqn = raw.class_fqn or raw.target or raw.target_id or raw.symbol
        if not class_fqn:
            raise ValueError("Java target requires class_fqn or target")
        return TargetIdentity.java_class(str(class_fqn))

    def generated_test_policy(self, repo_path: Path, target: Optional[TargetRef]) -> GeneratedTestPolicy:
        return GeneratedTestPolicy(
            language=self.language,
            allowed_test_roots=("src/test/java", "src/test/resources"),
            autopushable=True,
        )

    def prompt_bundle(self) -> PromptBundle:
        return PromptBundle(
            language=self.language,
            plan="plan_tests",
            generate="generate_test",
            fix_compile="fix_compile",
            fix_coverage="fix_coverage",
            fix_mutations="fix_mutations",
        )
