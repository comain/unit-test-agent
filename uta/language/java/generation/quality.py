"""The delegated (Maven enforcer) quality gate for Java generation.

One responsibility: decide what enforcement command to run for the current
batch, run it, and turn its raw output into something a repair phase can act
on -- readable feedback, whether the failure even belongs to this batch, and
which repair stage owns it.

Projection of a gate result into per-class results lives in ``evidence``; the
process-level command leaves live in ``commands``.
"""

import logging
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.language.java.generation.commands import _write_context_artifact
from uta.language.java.workspace import candidate_source_path as _candidate_source_path
from uta.language.java.workspace import class_module as _class_module
from uta.language.java.workspace import (
    discover_ci_incremental_java_test_files as _discover_ci_incremental_java_test_files,
)
from uta.language.java.workspace import java_fqn_from_test_path as _java_fqn_from_test_path
from uta.shared.config import settings as uta_settings

logger = logging.getLogger("uta")


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PIT_SPINNER_TAIL_RE = re.compile(r"\s*(?:[|/\\-]{3,}|(?:\?/\?[-|\\]?)){3,}\s*$")
_LOW_SIGNAL_PIT_PREFIXES = (
    "[INFO] Found plugin :",
    "[INFO] Found shared classpath plugin :",
    "[INFO] Available mutators :",
    "[INFO] Adding org.pitest:",
    "Enhanced functionality available at",
)
_HIGH_SIGNAL_QUALITY_GATE_MARKERS = (
    "[test-enforcer]",
    "BUILD FAILURE",
    "BUILD SUCCESS",
    "Tests run:",
    "Failed to execute goal",
    "Line Coverage",
    ">> Generated",
    ">> Mutations with no coverage",
    ">> Ran",
    "Test strength",
    "PIT >> INFO : Created",
    "PIT >> INFO : Completed",
)



def _apply_terminal_backspaces(text: str) -> str:
    cleaned: List[str] = []
    for char in text:
        if char == "\b":
            if cleaned:
                cleaned.pop()
            continue
        cleaned.append(char)
    return "".join(cleaned)


def _sanitize_quality_gate_output(text: str) -> str:
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    sanitized_lines: List[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = _apply_terminal_backspaces(line)
        line = _CONTROL_CHAR_RE.sub("", line)
        line = _PIT_SPINNER_TAIL_RE.sub("", line).rstrip()
        if any(line.lstrip().startswith(prefix) for prefix in _LOW_SIGNAL_PIT_PREFIXES):
            continue
        sanitized_lines.append(line)
    signal_lines = [
        line
        for line in sanitized_lines
        if any(marker in line for marker in _HIGH_SIGNAL_QUALITY_GATE_MARKERS)
        or line.lstrip().startswith("[ERROR]")
        or (line.lstrip().startswith(">") and " SURVIVED " in line and " SURVIVED 0" not in line)
    ]
    if signal_lines:
        return "\n".join(signal_lines).strip()
    return "\n".join(sanitized_lines).strip()



def _delegated_gate_context(state: Dict[str, Any]) -> Dict[str, Any]:
    context = state.get("rdc_context")
    if isinstance(context, dict):
        return context
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager
            from uta.tasks.models import json_loads

            task = TaskManager(task_db_path).get_task(int(task_id))
            return json_loads(task.get("rdc_context_json") or "{}") if task else {}
        except Exception:
            logger.debug("Failed to load RDC context for delegated quality gate", exc_info=True)
    return {}



def _run_delegated_quality_gate_once(
    state: Dict[str, Any],
    repo_path: str,
    *,
    run_command: Optional[Any] = None,
    batch: Optional[List[str]] = None,
    target_scoped: bool = False,
) -> Dict[str, Any]:
    from uta_enforce_core.commands import SafeProcessRunner
    from uta_enforce_core.contracts import (
        EnforcementInvocationContext,
        EnforcementRequest,
        EnforcementTarget,
        RuntimeSelection,
    )
    from uta_enforce_core.dispatch import enforce
    from uta_enforce_core.registry import EnforcementRegistry
    from uta.enforcement.bindings.java import JavaEnforcementBinding

    context = _delegated_gate_context(state)
    enforcement_ctx = context.get("enforcement") if isinstance(context.get("enforcement"), dict) else {}
    raw_command = enforcement_ctx.get("command") if isinstance(enforcement_ctx, dict) else None
    configured_command = str(state.get("quality_gate_command") or "").strip()
    command = (
        configured_command
        or (
            shlex.join([str(item) for item in raw_command])
            if isinstance(raw_command, (list, tuple))
            # A normalised argv arriving as a tuple must not reach str(): that
            # renders a Python repr as the gate command.
            else str(raw_command or uta_settings.ci_enforcement_command)
        )
    )
    if target_scoped and batch:
        command = _delegated_target_scoped_maven_command(state, repo_path, command, batch) or command

    preserve_scope = bool(target_scoped and batch)
    targets = tuple(
        EnforcementTarget(
            language="java",
            target_id=target_id,
            source_path=f"src/main/java/{target_id.replace('.', '/')}.java",
            metadata={"command": command, "preserve_explicit_target_scope": preserve_scope},
        )
        for target_id in (batch or [])
    ) or (
        EnforcementTarget(
            language="java",
            target_id="repo",
            source_path="pom.xml",
            metadata={"command": command, "preserve_explicit_target_scope": preserve_scope},
        ),
    )
    timeout = int(uta_settings.ci_enforcement_timeout_seconds or 1800)
    request = EnforcementRequest(
        repo_path=Path(repo_path),
        language="java",
        targets=targets,
        runtime=RuntimeSelection(timeout_seconds=timeout),
    )

    class _InjectedRunner:
        def run(self, cmd, *, cwd=None, env=None, timeout_seconds=None, is_cancelled=None):
            if run_command is not None:
                try:
                    return run_command(cmd, cwd=cwd, env=env, timeout=timeout_seconds)
                except TypeError:
                    return run_command(cmd)
            return SafeProcessRunner().run(cmd, cwd=cwd, env=env, timeout_seconds=timeout_seconds, is_cancelled=is_cancelled)

    inv_context = EnforcementInvocationContext(run_command=_InjectedRunner())
    registry = EnforcementRegistry([JavaEnforcementBinding()])

    try:
        result = enforce(request, registry=registry, context=inv_context)
        return dict(result.evidence)
    except Exception as exc:
        return {
            "status": "command_error",
            "passed": False,
            "command": shlex.split(command),
            "stderr": str(exc),
            "summary": "UTA test-enforcement command failed to start",
        }



def _delegated_target_scoped_maven_command(
    state: Dict[str, Any],
    repo_path: str,
    command: str,
    batch: List[str],
) -> Optional[str]:
    """Return a Maven enforcer command narrowed to the current CI repair batch."""
    from uta.language.java.enforcement_runner.planning import (
        _with_changed_modules,
        _with_target_tests,
    )

    target_tests = _ci_incremental_target_tests_for_batch(state, repo_path, batch)
    target_sources = _ci_incremental_target_sources_for_batch(state, repo_path, batch)
    modules = _ci_incremental_modules_for_batch(state, repo_path, batch)
    if not target_tests and not target_sources and not modules:
        return None
    cmd = _remove_maven_option_with_value(
        shlex.split(command),
        {"-pl", "--projects"},
        {
            "-DtargetTests=",
            "-Dtest=",
            "-Dsurefire.failIfNoSpecifiedTests=",
            "-Dtest.enforcement.targetSource=",
            "-Dtest.enforcement.targetSources=",
            "-pl=",
            "--projects=",
        },
    )
    if target_tests:
        cmd = _with_target_tests(cmd, target_tests)
    if target_sources:
        cmd.append(f"-Dtest.enforcement.targetSources={','.join(target_sources)}")
    if modules:
        cmd = _with_changed_modules(cmd, modules)
    return shlex.join(cmd)



def _remove_maven_option_with_value(
    cmd: List[str],
    option_names: set[str],
    option_prefixes: set[str],
) -> List[str]:
    updated: List[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            skip_next = False
            continue
        if item in option_names:
            skip_next = True
            continue
        if any(item.startswith(prefix) for prefix in option_prefixes):
            continue
        updated.append(item)
    return updated



def _ci_incremental_target_tests_for_batch(state: Dict[str, Any], repo_path: str, batch: List[str]) -> List[str]:
    tests: List[str] = []
    for class_fqn in batch:
        module = _class_module({**state, "repo_path": repo_path}, class_fqn)
        for test_path in _discover_ci_incremental_java_test_files(state, repo_path, class_fqn, module):
            fqn = _java_fqn_from_test_path(test_path)
            if fqn:
                tests.append(fqn)
    return list(dict.fromkeys(tests))


def _ci_incremental_modules_for_batch(state: Dict[str, Any], repo_path: str, batch: List[str]) -> List[str]:
    modules: List[str] = []
    for class_fqn in batch:
        module = _class_module({**state, "repo_path": repo_path}, class_fqn)
        if module and module not in modules:
            modules.append(module)
    return modules



def _xml_child_text(parent: ET.Element, name: str) -> Optional[str]:
    for child in list(parent):
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == name and child.text:
            return child.text.strip()
    return None



def _maven_artifact_id_for_module(repo_path: str, module: Optional[str]) -> Optional[str]:
    pom_path = (Path(repo_path) / module / "pom.xml") if module else (Path(repo_path) / "pom.xml")
    if not pom_path.is_file():
        return None
    try:
        root = ET.parse(pom_path).getroot()
    except ET.ParseError:
        return None
    return _xml_child_text(root, "artifactId")



def _ci_incremental_module_identities_for_batch(state: Dict[str, Any], repo_path: str, batch: List[str]) -> List[str]:
    identities: List[str] = []
    for module in _ci_incremental_modules_for_batch(state, repo_path, batch):
        for value in (module, Path(module).name, _maven_artifact_id_for_module(repo_path, module)):
            normalized = str(value or "").strip()
            if normalized and normalized not in identities:
                identities.append(normalized)
    return identities


def _ci_incremental_target_sources_for_batch(state: Dict[str, Any], repo_path: str, batch: List[str]) -> List[str]:
    repo = Path(repo_path).resolve()
    sources: List[str] = []
    for class_fqn in batch:
        source_path = _candidate_source_path({**state, "repo_path": repo_path}, class_fqn)
        if not source_path:
            continue
        path = Path(source_path)
        try:
            if path.is_absolute():
                source_rel = path.resolve().relative_to(repo).as_posix()
            else:
                source_rel = path.as_posix()
        except Exception:
            source_rel = path.as_posix()
        source_rel = source_rel.replace("\\", "/").lstrip("/")
        if source_rel and source_rel.endswith(".java"):
            sources.append(source_rel)
    return list(dict.fromkeys(sources))



def _delegated_quality_gate_feedback(result: Dict[str, Any], max_chars: int = 6000) -> str:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    combined = _sanitize_quality_gate_output(stdout + "\n" + stderr)
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
    command = " ".join(str(item) for item in result.get("command") or [])
    return (
        f"Summary: {result.get('summary') or ''}\n"
        f"Status: {result.get('status') or ''}\n"
        f"Command: {command}\n\n"
        f"Output:\n{combined}"
    ).strip()


def _delegated_quality_gate_feedback_excerpt(feedback: str, max_chars: int = 1800) -> str:
    markers = ("Summary:", "Status:", "[test-enforcer]", "Tests run:", ">> ", "> KILLED", "BUILD FAILURE", "[ERROR]")
    lines = [line for line in feedback.splitlines() if line.strip() and any(marker in line for marker in markers)]
    excerpt = "\n".join(lines or [line for line in feedback.splitlines() if line.strip()])
    if len(excerpt) > max_chars:
        return excerpt[:max_chars].rstrip() + "\n... (truncated; read the full feedback artifact)"
    return excerpt



def _delegated_quality_gate_prompt_feedback(repo_path: str, stage: str, attempt: int, feedback: str) -> str:
    safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage or "delegated_gate").strip("_") or "delegated_gate"
    feedback_abs = _write_context_artifact(
        repo_path,
        f"{safe_stage}_delegated_quality_gate_attempt_{attempt}.md",
        "# Delegated Quality Gate Feedback\n\n"
        "```text\n"
        f"{feedback}\n"
        "```\n",
    )
    excerpt = _delegated_quality_gate_feedback_excerpt(feedback)
    return (
        f"Full feedback artifact: `{feedback_abs}`\n"
        "Read the artifact before editing tests. Key excerpt:\n"
        "```text\n"
        f"{excerpt}\n"
        "```"
    )



def _delegated_gate_failure_matches_batch(state: Dict[str, Any], repo_path: str, result: Dict[str, Any], batch: List[str]) -> bool:
    """Return False when Maven clearly failed on a different CI target/module."""
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    if not output.strip():
        return True

    batch_modules = set(_ci_incremental_module_identities_for_batch(state, repo_path, batch))
    failed_modules = _failed_maven_modules_from_output(output)
    if failed_modules and batch_modules and failed_modules.isdisjoint(batch_modules):
        return False

    batch_simple_names = {class_fqn.rsplit(".", 1)[-1] for class_fqn in batch}
    failed_tests = _failed_test_simple_names_from_output(output)
    if failed_tests and batch_simple_names:
        matching_tests = {
            test_name
            for test_name in failed_tests
            if any(simple_name in test_name for simple_name in batch_simple_names)
        }
        if not matching_tests:
            return False
    return True



def _annotate_out_of_scope_gate_failure(
    result: Dict[str, Any],
    *,
    state: Dict[str, Any],
    repo_path: str,
    batch: List[str],
) -> Dict[str, Any]:
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    annotated = dict(result)
    annotated["passed"] = False
    annotated["status"] = "out_of_scope_gate_failure"
    annotated["summary"] = (
        "Current target-scoped repair did not own the Maven failure; "
        "remaining delegated gate failure belongs to another target and must not be marked passed"
    )
    annotated["outOfScopeGateFailure"] = {
        "currentBatch": list(batch),
        "batchModules": _ci_incremental_modules_for_batch(state, repo_path, batch),
        "batchModuleIdentities": _ci_incremental_module_identities_for_batch(state, repo_path, batch),
        "failedModules": sorted(_failed_maven_modules_from_output(output)),
        "failedTests": sorted(_failed_test_simple_names_from_output(output)),
    }
    return annotated



def _failed_maven_modules_from_output(output: str) -> set[str]:
    modules: set[str] = set()
    for match in re.finditer(r"on project\s+([A-Za-z0-9_.-]+)", output, flags=re.IGNORECASE):
        modules.add(match.group(1).strip())
    for line in output.splitlines():
        if " FAILURE " not in line:
            continue
        match = re.search(r"\[INFO\]\s+([A-Za-z0-9_.-]+)\s+\.+\s+FAILURE\b", line)
        if match:
            modules.add(match.group(1).strip())
    return modules


def _failed_test_simple_names_from_output(output: str) -> set[str]:
    from uta.language.java.compile.error_classifier import classify_compile_errors

    tests: set[str] = set()
    # Maven compiler failures identify tests by source path rather than the
    # Surefire-style "ClassTest.method" form below. Treat those paths as test
    # ownership evidence so a stale test in the same Maven module cannot be
    # misattributed to the active repair target.
    for error in classify_compile_errors(output):
        normalized = str(error.file or "").replace("\\", "/")
        if "/src/test/java/" in normalized or normalized.startswith("src/test/java/"):
            tests.add(Path(normalized).stem)
    for match in re.finditer(r"\[ERROR\]\s+([A-Za-z0-9_.$]+Test)(?:[.:]\w+)?", output):
        tests.add(match.group(1).rsplit(".", 1)[-1])
    return tests



def _delegated_gate_failure_stage(result: Dict[str, Any]) -> str:
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    if (
        "check-coverage failed" in output
        or "diff coverage failed" in output
        or "diff coverage check failed" in output
        or "coverage gate failed" in output
        or ("diff line coverage" in output and "below required" in output)
    ):
        return "coverage_fix"
    if (
        "check-mutation failed" in output
        or "mutation gate failed" in output
        or "mutation coverage failed" in output
        or "mutation score is below" in output
        or ("diff mutation score" in output and "below required" in output)
        or "test-strength failed" in output
        or "test strength failed" in output
        or "test strength score" in output
        or ("test strength" in output and "below threshold" in output)
    ):
        return "mutation_fix"
    return "coverage_fix"





__all__ = [
    "_annotate_out_of_scope_gate_failure",
    "_ci_incremental_module_identities_for_batch",
    "_ci_incremental_modules_for_batch",
    "_ci_incremental_target_sources_for_batch",
    "_ci_incremental_target_tests_for_batch",
    "_delegated_gate_context",
    "_delegated_gate_failure_matches_batch",
    "_delegated_gate_failure_stage",
    "_delegated_quality_gate_feedback",
    "_delegated_quality_gate_feedback_excerpt",
    "_delegated_quality_gate_prompt_feedback",
    "_delegated_target_scoped_maven_command",
    "_failed_maven_modules_from_output",
    "_failed_test_simple_names_from_output",
    "_maven_artifact_id_for_module",
    "_remove_maven_option_with_value",
    "_run_delegated_quality_gate_once",
    "_sanitize_quality_gate_output",
    "_xml_child_text",
]
