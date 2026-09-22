"""Deterministic Maven, git, and artifact commands used by Java generation.

These are the process-level leaves of Java generation: run a compile, run a
test selector, write a context artifact, build the index-query command line.
They own no phase policy, so every other generation module may depend on them
without creating a cycle back into phase logic.
"""

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from uta.language.java.maven.invocation import targeted_maven_command
from uta.language.java.maven.jacoco import parse_surefire_results
from uta.shared.config import settings as uta_settings
from uta.shared.git import git
from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file

logger = logging.getLogger("uta")


def _git_run(repo_path: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run git in repo_path, through the one place this project invokes git.

    Two dozen call sites reach this. It used to resolve credentials itself and
    run unbounded; both now come from `uta.shared.git`, so a change to either
    reaches every git call in the project rather than this file alone.

    ``check`` stays False by default because these call sites read
    ``returncode`` themselves. A timeout still raises, since a command that
    never returned has no exit code to read.
    """
    kwargs.setdefault("check", False)
    timeout = kwargs.pop("timeout", -1.0)
    return git(timeout=timeout).run(repo_path, *args, **kwargs)




def _index_query_command(module: Optional[str], *, section: Optional[str] = None) -> str:
    cmd = [str((Path(__file__).resolve().parents[2] / "bin" / "uta-query-index").resolve())]
    if module:
        cmd.extend(["--module", module])
    if section:
        cmd.extend(["--section", section])
    return " ".join(shlex.quote(part) for part in cmd)



def _stage_introspect_section(repo_path: str, stage: str) -> str:
    try:
        path = ensure_stage_introspect_file(repo_path, stage)
    except Exception:
        logger.debug("Stage introspect path unavailable for %s", stage, exc_info=True)
        return ""
    return (
        "\n\n### STAGE INTROSPECT\n"
        f"- Prior lessons for this stage: `{path}`\n"
        "- Read this file before broad exploration. Apply only lessons relevant to the current target and do not repeat already-avoided mistakes."
    )




def _format_compile_error_block(errors: List[Any], *, heading: Optional[str] = None) -> str:
    if not errors:
        return ""
    lines: List[str] = []
    if heading:
        lines.append(heading)
    for err in errors:
        line = f"- [{err.category.upper()}] {err.file}:{err.line}: {err.message}"
        if getattr(err, "symbol", None):
            line += f" ({err.symbol})"
        lines.append(line)
        for detail in list(getattr(err, "detail", ()) or ())[:4]:
            lines.append(f"  {detail}")
    return "\n".join(lines)


def _render_compile_fix_feedback(
    current_errors: List[Any],
    *,
    new_errors: Optional[List[Any]] = None,
    recurring_errors: Optional[List[Any]] = None,
) -> str:
    if not current_errors:
        return ""
    if new_errors is None or recurring_errors is None:
        return _format_compile_error_block(current_errors, heading="ALL CURRENT COMPILATION ERRORS")

    sections: List[str] = [
        "Fix the full current compile-error set below. Do not focus only on the first error.",
    ]
    if new_errors:
        sections.append(_format_compile_error_block(new_errors, heading="NEW ERROR CLUSTERS"))
    if recurring_errors:
        sections.append(_format_compile_error_block(recurring_errors, heading="RECURRING ERROR CLUSTERS"))
    return "\n\n".join(section for section in sections if section.strip())



def _expected_test_paths_for_batch(module: Optional[str], batch: List[str]) -> List[str]:
    module_prefix = f"{module}/" if module else ""
    paths = []
    for fqn in batch:
        test_name = f"{fqn.split('.')[-1]}Test"
        package_path = fqn.rsplit(".", 1)[0].replace(".", "/")
        paths.append(f"{module_prefix}src/test/java/{package_path}/{test_name}.java")
    return paths


def _filter_compile_errors_to_paths(errors: List[Any], expected_paths: List[str]) -> List[Any]:
    normalized = [path.replace("\\", "/").lstrip("/") for path in expected_paths]
    filtered = []
    for error in errors:
        error_file = str(getattr(error, "file", "") or "").replace("\\", "/")
        if any(error_file.endswith(path) for path in normalized):
            filtered.append(error)
    return filtered



def _refresh_test_failure_summary(
    repo_path: str,
    test_selector: str,
    module: Optional[str],
    fallback_output: str,
) -> str:
    test_classes = [name.strip() for name in test_selector.split(",") if name.strip()]
    if not test_classes:
        return fallback_output

    try:
        results = parse_surefire_results(repo_path, test_classes, module)
    except Exception:
        logger.warning("Failed to refresh Surefire failure summary for %s", test_selector, exc_info=True)
        return fallback_output

    if not results:
        return fallback_output

    failed_sections: List[str] = []
    for test_class in test_classes:
        result = results.get(test_class)
        if not result or result.get("passed", False):
            continue
        output = (result.get("output") or "").strip()
        if output:
            failed_sections.append(f"## {test_class}\n{output}")
        else:
            failed_sections.append(f"## {test_class}\nSurefire reported failures but did not capture details.")

    return "\n\n".join(failed_sections).strip() or fallback_output



def _run_test_selector(
    repo_path: str,
    test_selector: str,
    module: Optional[str] = None,
    timeout: int = 300,
    quality_gate_command: str = "",
) -> tuple[bool, str]:
    """Run one or more tests selected by ``-Dtest=...`` and return success plus concise failure text."""
    cmd = targeted_maven_command(
        repo_path, "test", module=module, test_selector=test_selector,
        quality_gate_command=quality_gate_command,
    )
    try:
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            stdout = result.stdout.decode(errors="replace") if result.stdout else ""
            error_text = stderr or stdout
            lines = error_text.split("\n")
            error_lines = [
                l for l in lines
                if "[ERROR]" in l or "FAILURE" in l or "Tests run:" in l or "Failed tests:" in l
            ]
            return False, "\n".join(error_lines[-40:]) if error_lines else error_text[-2500:]
        stdout = result.stdout.decode(errors="replace") if result.stdout else ""
        stderr = result.stderr.decode(errors="replace") if result.stderr else ""
        warnings = [
            line.strip() for line in (stdout + "\n" + stderr).splitlines()
            if "[WARNING]" in line and "POM for " in line and " is missing" in line
        ]
        return True, "\n".join(warnings[-10:])
    except subprocess.TimeoutExpired:
        return False, "Test execution timed out"









def _compile_test(
    repo_path: str, module: str = None, timeout: int = 300,
    quality_gate_command: str = "",
) -> tuple:
    """Run mvn test-compile independently. Returns (success, error_output)."""
    cmd = targeted_maven_command(
        repo_path, "test-compile", module=module,
        quality_gate_command=quality_gate_command,
    )
    try:
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            stdout = result.stdout.decode(errors="replace") if result.stdout else ""
            # Extract the most useful error lines
            error_text = stderr or stdout
            # Find compilation error section
            lines = error_text.split("\n")
            error_lines = [l for l in lines if "[ERROR]" in l]
            return False, "\n".join(error_lines[-30:]) if error_lines else error_text[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out"



def _write_context_artifact(repo_path: str, filename: str, content: str) -> str:
    ctx_dir = Path(repo_path) / ".uta_cache" / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = ctx_dir / filename
    out.write_text(content, encoding="utf-8")
    return str(out.resolve())



def _run_test(repo_path: str, test_class: str, module: str = None, timeout: int = 300) -> tuple:
    """Run a specific test class. Returns (success, error_output)."""
    return _run_test_selector(repo_path, test_class, module=module, timeout=timeout)

# Re-exported on purpose: these names are imported from this module


__all__ = [
    "_compile_test",
    "_expected_test_paths_for_batch",
    "_filter_compile_errors_to_paths",
    "_format_compile_error_block",
    "_git_run",
    "_index_query_command",
    "_refresh_test_failure_summary",
    "_render_compile_fix_feedback",
    "_run_test",
    "_run_test_selector",
    "_stage_introspect_section",
    "_write_context_artifact",
]
