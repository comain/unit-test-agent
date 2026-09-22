"""Deterministic verification helpers for the durable Python cycle."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from uta.language.python.enforcement import run_python_enforcement
from uta.language.python.verification.runner import (
    CommandEvidence,
    CoverageSummary,
    MutationSummary,
    PythonVerificationResult,
)
from uta.shared.targets import TargetRef


def enforce_generated_test(
    repo: Path,
    target: TargetRef,
    generated_abs: Path,
    *,
    context_payload: Dict[str, Any],
    enforcer: Optional[Callable[..., Dict[str, Any]]],
    coverage_gate: float,
    mutation_gate: float,
    run_mutation: bool = True,
    base_ref: str = "origin/master",
) -> PythonVerificationResult:
    """Run repair verification through the same canonical tool as CI.

    Repair used to call ``verify_python_target`` directly.  That bypassed the
    canonical target resolution, strict-test selection and evidence projection
    used by CI, creating two enforcement products that only happened to share
    low-level helpers.  The low-level verifier remains inside the distributed
    binding; generation consumes only the public enforcement envelope.
    """
    syntax_version = (context_payload.get("syntax") or {}).get("version") or "python3"
    generated_rel = generated_abs.relative_to(repo).as_posix()
    import_contract_error = validate_generated_test_import_contract(generated_abs)
    if import_contract_error:
        return PythonVerificationResult(
            status="failed",
            reason_code="invalid_generated_test_import",
            message=import_contract_error,
        )
    try:
        enforce = enforcer or run_python_enforcement
        evidence = enforce(
            repo_path=repo,
            target_values=[target.target_id],
            test_paths=[generated_rel],
            syntax_version=syntax_version,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            run_mutation=run_mutation,
            base_ref=base_ref,
        )
        return _verification_result_from_enforcement(evidence, run_mutation=run_mutation)
    except Exception as exc:
        return PythonVerificationResult(
            status="failed",
            reason_code="verification_error",
            message=str(exc),
        )


def _verification_result_from_enforcement(
    evidence: Mapping[str, Any], *, run_mutation: bool
) -> PythonVerificationResult:
    targets = evidence.get("targetResults")
    target = dict(targets[0]) if isinstance(targets, list) and targets else {}
    coverage = _coverage_summary(target.get("coverage"))
    mutation = _mutation_summary(target.get("mutation")) if run_mutation else None
    setup = target.get("setup") if isinstance(target.get("setup"), Mapping) else {}
    return PythonVerificationResult(
        status=str(target.get("status") or evidence.get("status") or "failed"),
        reason_code=str(
            target.get("reasonCode") or evidence.get("reasonCode") or "verification_error"
        ),
        tests_pass=bool(target.get("testsPass", False)),
        coverage=coverage,
        mutation=mutation,
        commands=[
            _command_evidence(command)
            for command in (target.get("commands") or evidence.get("commands") or [])
            if isinstance(command, Mapping)
        ],
        message=str(target.get("message") or evidence.get("summary") or ""),
        setup_status=str(setup.get("setupStatus") or "skipped"),
        environment_profile=str(setup.get("environmentProfile") or "default"),
        dependency_fingerprints=dict(setup.get("dependencyFingerprints") or {}),
        cache_key=str(setup.get("cacheKey") or ""),
    )


def _coverage_summary(value: Any) -> Optional[CoverageSummary]:
    if not isinstance(value, Mapping):
        return None
    return CoverageSummary(
        covered=int(value.get("covered") or 0),
        total=int(value.get("total") or 0),
        rate=float(value.get("rate") or 0.0),
        gate=float(value.get("gate") or 0.0),
        passed=bool(value.get("passed")),
        xml_path=str(value.get("xml_path") or value.get("xmlPath") or ""),
        scope=str(value.get("scope") or "target_file"),
        changed_lines=dict(value.get("changed_lines") or value.get("changedLines") or {}),
        uncovered_lines=dict(value.get("uncovered_lines") or value.get("uncoveredLines") or {}),
        no_executable_changed_lines=bool(
            value.get("no_executable_changed_lines") or value.get("noExecutableChangedLines")
        ),
    )


def _mutation_summary(value: Any) -> Optional[MutationSummary]:
    if not isinstance(value, Mapping):
        return None
    artifacts = value.get("artifacts") if isinstance(value.get("artifacts"), Mapping) else {}
    candidate_plan = (
        value.get("candidatePlan") if isinstance(value.get("candidatePlan"), Mapping) else {}
    )
    survivors = value.get("diff_survivors") or value.get("diffSurvivors") or value.get("survivors") or []
    return MutationSummary(
        runtime_lane=str(value.get("runtime_lane") or value.get("runtimeLane") or "not_run"),
        generated=int(value.get("generated") or 0),
        killed=int(value.get("killed") or 0),
        survived=int(value.get("survived") or 0),
        no_coverage=int(value.get("no_coverage") or value.get("noCoverage") or 0),
        rate=float(value.get("rate") or 0.0),
        gate=float(value.get("gate") or 0.0),
        passed=bool(value.get("passed")),
        no_tests=int(value.get("no_tests") or value.get("noTests") or 0),
        timeout=int(value.get("timeout") or 0),
        suspicious=int(value.get("suspicious") or 0),
        skipped=int(value.get("skipped") or 0),
        caught_by_type_check=int(value.get("caught_by_type_check") or value.get("caughtByTypeCheck") or 0),
        survivors=list(survivors),
        artifacts=dict(artifacts),
        scope=str(value.get("scope") or "target_file"),
        changed_lines=dict(value.get("changed_lines") or value.get("changedLines") or {}),
        diff_survivors=list(survivors),
        changed_line_mutants_generated=int(value.get("changedLineMutantsGenerated") or 0),
        changed_line_mutants_killed=int(value.get("changedLineMutantsKilled") or 0),
        changed_line_mutants_scored=int(value.get("changedLineMutantsScored") or 0),
        sampling=dict(value.get("sampling") or {}),
        candidate_plan=dict(candidate_plan),
    )


def _command_evidence(value: Mapping[str, Any]) -> CommandEvidence:
    command = value.get("command") or value.get("argv") or []
    if isinstance(command, str):
        command = [command]
    return CommandEvidence(
        name=str(value.get("name") or value.get("stage") or "enforcement"),
        command=[str(item) for item in command],
        exit_code=int(value.get("exit_code") or value.get("exitCode") or 0),
        elapsed_seconds=float(value.get("elapsed_seconds") or value.get("elapsedSeconds") or 0.0),
        stdout=str(value.get("stdout") or ""),
        stderr=str(value.get("stderr") or ""),
    )


def validate_generated_test_import_contract(generated_abs: Path) -> str:
    try:
        text = generated_abs.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=generated_abs.as_posix())
    except SyntaxError:
        return ""
    except OSError as exc:
        return f"Cannot read generated test file for import validation: {exc}"

    problems: List[str] = []
    forbidden_names = _generated_test_forbidden_runtime_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden_generated_test_import(module):
                problems.append(f"from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name or ""
                if _is_forbidden_generated_test_import(module):
                    problems.append(f"import {module}")
        elif isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in {
                "importlib.util.spec_from_file_location",
                "importlib.machinery.SourceFileLoader",
                "runpy.run_path",
            } or call_name.endswith(".exec_module"):
                problems.append(f"file-loader call {call_name}")
            forbidden_path = _generated_test_forbidden_runtime_path(
                node, call_name, forbidden_names
            )
            if forbidden_path:
                problems.append(f"runtime mutation of {forbidden_path}")

    if not problems:
        return ""
    unique = ", ".join(sorted(dict.fromkeys(problems)))
    return (
        "Generated Python test violates UTA import contract: "
        f"{unique}. Use the target's canonical dotted module import and keep any "
        "test helpers inline in the active verification test file. Do not write "
        "verifier configuration or mutmut runtime artifacts from generated tests."
    )


def _is_forbidden_generated_test_import(module: str) -> bool:
    top_level = str(module or "").split(".", 1)[0]
    return top_level.startswith("_uta_") or top_level.endswith("_loader")


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return ""


def _generated_test_forbidden_runtime_path(
    node: ast.Call, call_name: str, forbidden_names: Mapping[str, str]
) -> str:
    mutating_calls = {
        "write_text", "write_bytes", "touch", "mkdir", "unlink", "remove",
        "rmdir", "rmtree", "makedirs", "copy", "copyfile", "move",
    }
    if call_name == "open":
        if len(node.args) < 2:
            return ""
        mode = _literal_string(node.args[1])
        if mode and not any(flag in mode for flag in ("w", "a", "+", "x")):
            return ""
    elif call_name.rsplit(".", 1)[-1] not in mutating_calls:
        return ""
    if isinstance(node.func, ast.Attribute):
        forbidden = _forbidden_generated_test_expr_path(node.func.value, forbidden_names)
        if forbidden:
            return forbidden
    for literal in _string_literals(node):
        forbidden = _forbidden_generated_test_runtime_path(literal)
        if forbidden:
            return forbidden
    return ""


def _generated_test_forbidden_runtime_names(tree: ast.AST) -> Dict[str, str]:
    names: Dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                forbidden = _forbidden_generated_test_expr_path(node.value, names)
                if forbidden:
                    for target in node.targets:
                        changed = _record_forbidden_target_name(
                            target, forbidden, names
                        ) or changed
            elif isinstance(node, ast.AnnAssign):
                forbidden = (
                    _forbidden_generated_test_expr_path(node.value, names)
                    if node.value
                    else ""
                )
                if forbidden:
                    changed = _record_forbidden_target_name(
                        node.target, forbidden, names
                    ) or changed
    return names


def _record_forbidden_target_name(
    target: ast.AST, forbidden: str, names: Dict[str, str]
) -> bool:
    if isinstance(target, ast.Name) and names.get(target.id) != forbidden:
        names[target.id] = forbidden
        return True
    return False


def _forbidden_generated_test_expr_path(
    node: ast.AST, forbidden_names: Mapping[str, str]
) -> str:
    if isinstance(node, ast.Name):
        return forbidden_names.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _forbidden_generated_test_expr_path(
            node.left, forbidden_names
        ) or _forbidden_generated_test_expr_path(node.right, forbidden_names)
    if isinstance(node, ast.Call):
        for literal in _string_literals(node):
            forbidden = _forbidden_generated_test_runtime_path(literal)
            if forbidden:
                return forbidden
        if isinstance(node.func, ast.Attribute):
            return _forbidden_generated_test_expr_path(
                node.func.value, forbidden_names
            )
    if isinstance(node, ast.Attribute):
        return _forbidden_generated_test_expr_path(node.value, forbidden_names)
    for literal in _string_literals(node):
        forbidden = _forbidden_generated_test_runtime_path(literal)
        if forbidden:
            return forbidden
    return ""


def _forbidden_generated_test_runtime_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip().lstrip("./")
    config_files = {
        "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
        "noxfile.py",
    }
    if normalized in config_files:
        return normalized
    if normalized == "mutants" or normalized.startswith("mutants/") or "/mutants/" in normalized:
        return "mutants/"
    if normalized == ".mutmut-cache" or normalized.startswith(".mutmut-cache/"):
        return ".mutmut-cache/"
    if normalized == ".uta_cache" or normalized.startswith(".uta_cache/"):
        return ".uta_cache/"
    return ""


def _string_literals(node: ast.AST) -> List[str]:
    values: List[str] = []
    for child in ast.walk(node):
        literal = _literal_string(child)
        if literal:
            values.append(literal)
    return values


def _literal_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


__all__ = [
    "validate_generated_test_import_contract",
    "enforce_generated_test",
]
