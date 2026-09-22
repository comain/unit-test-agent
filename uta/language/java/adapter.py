"""Java's implementations of the narrow language ports.

Detection, target normalization, and the prompt bundle -- one method per port
declared in `uta.shared.languages`, plus the generation cycle binding that
`uta.testgen.graph.durable_cycle` still reaches through the registry.

What used to also live here -- `capabilities()`, `generated_test_policy()`,
`workspace_policy()`, `test_generation_backend()`, `batch_generator()` -- is
gone. The first two had no reader at all; the last three duplicated entries
that `uta.shared.backends` already resolves by string, and were reachable only
through fallbacks that the backend table never lets run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from uta.language.java.prompt_bundle import java_prompt_bundle
from uta.shared.languages import DetectionSignal, PromptBundle, RawTargetSelection
from uta.shared.targets import TargetIdentity, TargetRef


class JavaLanguageAdapter:
    language = "java"

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

    def is_production_source_path(self, path: str) -> bool:
        normalized = str(path or "").replace("\\", "/")
        if not normalized.endswith(".java"):
            return False
        parts = [part for part in normalized.split("/") if part]
        return any(parts[index:index + 3] == ["src", "main", "java"] for index in range(max(0, len(parts) - 3)))

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        class_fqn = raw.class_fqn or raw.target or raw.target_id or raw.symbol
        if not class_fqn:
            raise ValueError("Java target requires class_fqn or target")
        return TargetIdentity.java_class(str(class_fqn))

    def prompt_bundle(self) -> PromptBundle:
        return java_prompt_bundle()

    def generation_cycle_binding(self, state: Mapping[str, object]):
        from uta.language.java.cycle_inputs import prepare_java_cycle_state
        from uta.language.java.generation_backend import JavaGenerationCycleBackend
        from uta.testgen.harness import create_agent_harness
        from uta.testgen.backend import GenerationCycleBinding

        context = dict(state.get("backend_context") or {})
        harness_factory = context.get("harness_factory")
        return GenerationCycleBinding(
            initial_state=prepare_java_cycle_state(state),
            backend=JavaGenerationCycleBackend(),
            runner=(
                harness_factory(Path(str(state["repo_path"])))
                if callable(harness_factory)
                else create_agent_harness()
            ),
        )
