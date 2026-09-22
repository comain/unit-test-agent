"""Java workspace path rules for the engine LLM guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from uta.shared.workspace_policy import task_fqns_for_guard


def is_maven_project_file(path: str) -> bool:
    return path == "pom.xml" or path.endswith("/pom.xml")


def expected_test_file_rel(module: Optional[str], class_fqn: str) -> str:
    test_class_name = f"{class_fqn.split('.')[-1]}Test"
    package_path = class_fqn.rsplit(".", 1)[0].replace(".", "/")
    module_prefix = f"{module}/" if module else ""
    return f"{module_prefix}src/test/java/{package_path}/{test_class_name}.java"


def module_from_source_path(repo_path: str, source_path: str) -> Optional[str]:
    try:
        rel = Path(source_path).resolve().relative_to(Path(repo_path).resolve())
    except Exception:
        rel = Path(source_path)
    parts = rel.parts
    for index in range(0, max(0, len(parts) - 3)):
        if parts[index:index + 3] == ("src", "main", "java"):
            module_parts = parts[:index]
            return "/".join(module_parts) if module_parts else None
    return None


def maven_module_for_path(repo_path: str, path_text: str) -> Optional[str]:
    repo = Path(repo_path).resolve()
    path = Path(path_text)
    current = path if path.is_absolute() else repo / path
    current = current.parent if current.suffix else current
    try:
        current = current.resolve()
    except Exception:
        pass
    while current != repo:
        try:
            current.relative_to(repo)
        except ValueError:
            break
        if (current / "pom.xml").is_file():
            rel = current.relative_to(repo).as_posix()
            return rel or None
        current = current.parent
    return None


def java_fqn_from_test_path(path_text: str) -> Optional[str]:
    marker = "src/test/java/"
    if marker not in path_text or not path_text.endswith(".java"):
        return None
    package_path = path_text.split(marker, 1)[1][:-5]
    return package_path.replace("/", ".").strip(".")


def test_path_matches_target_module(repo_path: str, path_text: str, module: Optional[str]) -> bool:
    test_module = maven_module_for_path(repo_path, path_text)
    if module:
        return test_module == module
    return test_module is None


def candidate_source_path(state: Dict[str, Any], class_fqn: str) -> str:
    graph = state.get("graph")
    node = None
    if graph is not None and hasattr(graph, "nodes"):
        node = graph.nodes.get(class_fqn)
    if node is not None and getattr(node, "file_path", None):
        return str(node.file_path)
    repo_path = Path(str(state.get("repo_path") or ""))
    module = state.get("module")
    base = repo_path / module if module else repo_path
    expected = base / "src" / "main" / "java" / Path(*class_fqn.split(".")).with_suffix(".java")
    if expected.is_file():
        return str(expected)
    suffix = "/".join(class_fqn.split(".")) + ".java"
    for path in repo_path.glob(f"**/src/main/java/{suffix}"):
        return str(path)
    return str(expected)


def class_module(state: Dict[str, Any], class_fqn: str) -> Optional[str]:
    configured_module = state.get("module_filter") if "module_filter" in state else state.get("module")
    if configured_module:
        return configured_module
    source_path = candidate_source_path(state, class_fqn)
    repo_path = str(state.get("repo_path") or "")
    return module_from_source_path(repo_path, source_path)


def is_ci_incremental_java_repair(state: Dict[str, Any]) -> bool:
    return (
        state.get("quality_mode") == "ci_incremental"
        and state.get("quality_gate_backend") == "maven_enforcer"
    )


def expected_ci_incremental_java_test_paths(state: Dict[str, Any], batch: List[str]) -> List[str]:
    paths: List[str] = []
    for class_fqn in task_fqns_for_guard(state, batch):
        module = class_module(state, class_fqn)
        paths.extend(discover_ci_incremental_java_test_files(state, str(state.get("repo_path") or ""), class_fqn, module))
        paths.append(expected_test_file_rel(module, class_fqn).replace("\\", "/"))
        if not module:
            paths.append(expected_test_file_rel(None, class_fqn).replace("\\", "/"))

    context = state.get("rdc_context") if isinstance(state.get("rdc_context"), dict) else {}
    enforcement = context.get("enforcement") if isinstance(context.get("enforcement"), dict) else {}
    evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
    batch_simple_names = {class_fqn.rsplit(".", 1)[-1] for class_fqn in task_fqns_for_guard(state, batch)}
    for target_test in evidence.get("targetTests") or []:
        target_test_fqn = str(target_test or "").strip()
        if not target_test_fqn:
            continue
        target_simple = target_test_fqn.rsplit(".", 1)[-1]
        if not any(simple in target_simple for simple in batch_simple_names):
            continue
        paths.append(f"src/test/java/{target_test_fqn.replace('.', '/')}.java")
    return list(dict.fromkeys(paths))


def discover_ci_incremental_java_test_files(
    state: Dict[str, Any],
    repo_path: str,
    class_fqn: str,
    module: Optional[str],
) -> List[str]:
    """Find real existing Java test files for a CI incremental repair target."""
    repo = Path(repo_path or state.get("repo_path") or ".")
    simple_name = class_fqn.rsplit(".", 1)[-1]
    candidates: List[str] = []

    def add_existing(path_value: str) -> None:
        normalized = path_value.replace("\\", "/").lstrip("/")
        if (
            normalized
            and (repo / normalized).is_file()
            and test_path_matches_target_module(str(repo), normalized, module)
        ):
            candidates.append(normalized)

    add_existing(expected_test_file_rel(module, class_fqn))
    if module:
        add_existing(expected_test_file_rel(None, class_fqn))

    context = state.get("rdc_context") if isinstance(state.get("rdc_context"), dict) else {}
    enforcement = context.get("enforcement") if isinstance(context.get("enforcement"), dict) else {}
    evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
    for target_test in evidence.get("targetTests") or []:
        target_test_fqn = str(target_test or "").strip()
        if not target_test_fqn:
            continue
        target_simple = target_test_fqn.rsplit(".", 1)[-1]
        if simple_name not in target_simple:
            continue
        suffix = f"src/test/java/{target_test_fqn.replace('.', '/')}.java"
        for test_path in repo.glob(f"**/{suffix}"):
            try:
                rel = test_path.relative_to(repo).as_posix()
            except ValueError:
                continue
            if test_path_matches_target_module(str(repo), rel, module):
                candidates.append(rel)
        add_existing(suffix)

    for test_path in repo.glob(f"**/src/test/java/**/*{simple_name}*Test.java"):
        try:
            rel = test_path.relative_to(repo).as_posix()
        except ValueError:
            continue
        if test_path_matches_target_module(str(repo), rel, module):
            candidates.append(rel)

    def rank(path: str) -> tuple[int, int, int, str]:
        parts = path.split("/")
        module_match = 0 if module and path.startswith(f"{module}/") else 1
        exact_name = 0 if Path(path).name == f"{simple_name}Test.java" else 1
        return (module_match, exact_name, len(parts), path)

    return sorted(dict.fromkeys(candidates), key=rank)


class JavaWorkspacePolicy:
    language = "java"

    def allowed_llm_path(self, path: str, state: Dict[str, Any], batch: List[str]) -> bool:
        if is_maven_project_file(path):
            return True
        if self.is_test_artifact_path(path):
            return True
        if is_ci_incremental_java_repair(state):
            return False
        module = state.get("module")
        for class_fqn in task_fqns_for_guard(state, batch):
            expected = expected_test_file_rel(module, class_fqn).replace("\\", "/")
            if path == expected:
                return True
            if not module:
                unqualified = expected_test_file_rel(None, class_fqn).replace("\\", "/")
                if path.endswith(f"/{unqualified}"):
                    return True
        return False

    def is_test_artifact_path(self, path: str) -> bool:
        normalized = path.lstrip("/")
        if not normalized or normalized.startswith("."):
            return False
        return normalized.startswith("src/test/") or "/src/test/" in normalized

    def is_repair_config_path(self, path: str) -> bool:
        return is_maven_project_file(path)

    def related_test_artifact_paths(self, state: Dict[str, Any], target_id: str) -> List[str]:
        repo_path = str(state.get("repo_path") or "")
        return discover_ci_incremental_java_test_files(
            state,
            repo_path,
            target_id,
            class_module(state, target_id),
        )

    def cleanup_runtime_residue(self, repo_path: str) -> None:
        return None

    def cleanup_llm_generated_residue(self, repo_path: str, before: Mapping[str, str]) -> None:
        return None
