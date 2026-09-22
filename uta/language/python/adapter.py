"""Python's implementations of the narrow language ports.

Detection, target normalization, and the prompt bundle -- one method per port
declared in `uta.shared.languages` -- plus two Python-only capabilities
(`scan_candidates` / `select_candidates`, which walk the tree for testable
files) and the generation cycle binding that `uta.testgen.graph.durable_cycle`
still reaches through the registry.

What used to also live here -- `capabilities()`, `generated_test_policy()`,
`workspace_policy()`, `test_generation_backend()`, `batch_generator()` -- is
gone. The first two had no reader at all; the last three duplicated entries
that `uta.shared.backends` already resolves by string, and were reachable only
through fallbacks that the backend table never lets run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from uta.language.python.prompt_bundle import python_prompt_bundle
from uta.shared.languages import DetectionSignal, PromptBundle, RawTargetSelection
from uta.shared.targets import TargetRef


_PYTHON_MARKERS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "tox.ini", "noxfile.py")
_EXCLUDED_PARTS = {".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "__pycache__", "site-packages", "dist", "build"}
DEFAULT_MAX_FILES = 500


@dataclass(frozen=True)
class PythonTargetSelection:
    targets: Sequence[TargetRef]
    skipped_targets: Sequence[Dict[str, Any]]
    max_files: int

    @property
    def selected_count(self) -> int:
        return len(self.targets)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_targets)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "selected_count": self.selected_count,
            "skipped_count": self.skipped_count,
            "max_files": self.max_files,
            "skipped_targets": list(self.skipped_targets),
        }


class PythonLanguageAdapter:
    language = "python"

    def detect(self, repo_path: Path, changed_paths: Optional[Sequence[str]] = None) -> DetectionSignal:
        from uta.shared.source_selection import iter_all_source_files

        reasons = []
        for marker in _PYTHON_MARKERS:
            if (repo_path / marker).exists():
                reasons.append(marker)
        if changed_paths:
            reasons.extend(path for path in changed_paths if str(path).endswith(".py"))
        elif not reasons:
            reasons.extend(iter_all_source_files(self, str(repo_path))[:1])
        return DetectionSignal(self.language, len(reasons), reasons)

    def is_production_source_path(self, path: str) -> bool:
        normalized = str(path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized.endswith(".py") or normalized.startswith("/"):
            return False
        parts = [part for part in normalized.split("/") if part]
        if not parts or any(part == ".." for part in parts):
            return False
        if parts[0] in {"tests", "test"}:
            return False
        if set(parts) & _EXCLUDED_PARTS:
            return False
        name = parts[-1]
        if name == "__init__.py" or name.startswith("test_"):
            return False
        return True

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        source_path, symbol, granularity = _parse_python_selection(raw)
        display = raw.display_name or (f"{source_path}::{symbol}" if symbol else source_path)
        target_id = f"pysymbol:{source_path}::{symbol}" if symbol else f"pyfile:{source_path}"
        return raw.to_target_ref(
            language=self.language,
            target_id=target_id,
            display_name=display,
            granularity=granularity,
            source_path=source_path,
            symbol=symbol,
        )

    def scan_candidates(self, repo_path: Path) -> Sequence[TargetRef]:
        return self.select_candidates(repo_path).targets

    def select_candidates(self, repo_path: Path, *, max_files: int = DEFAULT_MAX_FILES) -> PythonTargetSelection:
        from uta.shared.source_selection import iter_all_source_files

        targets = []
        skipped: List[Dict[str, Any]] = []
        max_files = max(1, int(max_files or DEFAULT_MAX_FILES))
        for relative in iter_all_source_files(self, str(repo_path)):
            target = self.normalize_target(RawTargetSelection(target=relative))
            if len(targets) < max_files:
                targets.append(target)
            else:
                skipped.append(
                    {
                        "target_id": target.target_id,
                        "source_path": target.source_path,
                        "reason": "max_files_exceeded",
                    }
                )
        return PythonTargetSelection(targets=targets, skipped_targets=skipped, max_files=max_files)

    def prompt_bundle(self) -> PromptBundle:
        return python_prompt_bundle()

    def generation_cycle_binding(self, state):
        from uta.language.python.cycle_inputs import prepare_python_cycle_state
        from uta.language.python.generation_backend import PythonGenerationCycleBackend
        from uta.testgen.harness import create_agent_harness
        from uta.testgen.backend import GenerationCycleBinding

        context = dict(state.get("backend_context") or {})
        harness_factory = context.get("harness_factory")
        if callable(harness_factory):
            runner = harness_factory(Path(str(state["repo_path"])))
        else:
            runner = create_agent_harness()
        return GenerationCycleBinding(
            initial_state=prepare_python_cycle_state(state),
            backend=PythonGenerationCycleBackend(
                enforcer=context.get("enforcer")
            ),
            runner=runner,
        )


def _normalize_source_path(source_path: str) -> str:
    path = str(source_path or "").strip()
    if not path:
        raise ValueError("Python target requires a source path")
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = [part for part in path.split("/") if part]
    if path.startswith("/") or any(part == ".." for part in parts):
        raise ValueError("Python target source path must be repo-relative and stay inside the repository")
    if not path.endswith(".py"):
        raise ValueError("Python target source path must point to a .py file")
    return "/".join(parts)


def _strip_prefixed_target(value: str) -> Tuple[str, Optional[str]]:
    if value.startswith("pyfile:"):
        return value[len("pyfile:") :], None
    if value.startswith("pysymbol:"):
        rest = value[len("pysymbol:") :]
        if "::" not in rest:
            raise ValueError("Python symbol target must use pysymbol:path.py::symbol")
        source_path, symbol = rest.split("::", 1)
        return source_path, symbol
    if "::" in value:
        source_path, symbol = value.split("::", 1)
        return source_path, symbol
    return value, None


def _parse_python_selection(raw: RawTargetSelection) -> Tuple[str, Optional[str], str]:
    value = raw.target or raw.target_id
    if value:
        source_path, symbol = _strip_prefixed_target(str(value))
    else:
        source_path, symbol = raw.source_path, raw.symbol
    source_path = _normalize_source_path(str(source_path or ""))
    symbol = str(symbol or raw.symbol or "").strip() or None
    granularity = raw.granularity or ("function" if symbol else "file")
    return source_path, symbol, str(granularity)
