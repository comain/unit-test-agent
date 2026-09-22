"""Python-specific CI failure-diagnostics rendering.

The CI report layer (``uta.app.reporting``) is language-agnostic; the
logic for turning a *Python* enforcement result into human-facing failure
diagnostics lives here, next to the rest of the Python backend, so the report
layer only dispatches. The Java counterpart (JaCoCo/PIT/Surefire diagnostics)
remains inline in the report layer for now — see R1 in the convergence plan.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Sequence


def python_failure_diagnostics(
    enforcement: Dict[str, Any],
    evidence: Dict[str, Any],
) -> list[Dict[str, Any]]:
    """Diagnostics for a Python target whose selected test failed before gates."""
    language = str(enforcement.get("language") or evidence.get("language") or "")
    backend = str(enforcement.get("backend") or evidence.get("backend") or "")
    if language != "python" and backend != "python_enforcer":
        return []
    target_results = evidence.get("targetResults") if isinstance(evidence.get("targetResults"), list) else []
    diagnostics: list[Dict[str, Any]] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "passed":
            continue
        if str(item.get("reasonCode") or "") != "test_failed":
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        candidate_results = item.get("candidateResults") if isinstance(item.get("candidateResults"), list) else []
        failed_candidate = _first_failed_python_candidate(candidate_results) or {}
        message = str(failed_candidate.get("message") or item.get("message") or "")
        error_excerpt = _pytest_error_excerpt(message)
        selected_tests = _string_list(item.get("selectedTestPaths"))
        candidate_tests = _string_list(item.get("candidateTestPaths"))
        failing_tests = _string_list(failed_candidate.get("testPaths"))
        diagnostics.append(
            {
                "type": "python_test_failed",
                "title": "Python selected unit test failed before coverage/mutation",
                "message": (
                    "Pytest failed while running the selected target-specific test, so this target "
                    "did not reach coverage or mutation verification."
                ),
                "target": target.get("display_name") or target.get("source_path") or target.get("target_id") or "unknown",
                "selectedTestPaths": selected_tests,
                "candidateTestPaths": candidate_tests,
                "failingTestPaths": failing_tests or selected_tests,
                "reasonCode": item.get("reasonCode") or "",
                "errorExcerpt": error_excerpt,
                "hint": _python_test_failure_hint(error_excerpt),
            }
        )
        if len(diagnostics) >= 5:
            break
    return diagnostics


def _first_failed_python_candidate(candidate_results: Sequence[Any]) -> Dict[str, Any] | None:
    for candidate in candidate_results:
        if isinstance(candidate, dict) and candidate.get("status") != "passed":
            return candidate
    return None


def _pytest_error_excerpt(output: str) -> str:
    text = str(output or "")
    patterns = [
        r"(?m)^\s*E\s+((?:ImportError|ModuleNotFoundError|AttributeError|TypeError|ValueError|RuntimeError|AssertionError):[^\n]+)",
        r"(?m)^((?:ImportError|ModuleNotFoundError|AttributeError|TypeError|ValueError|RuntimeError|AssertionError):[^\n]+)",
        r"(?m)^FAILED\s+([^\n]+)",
        r"(?m)^ERROR\s+([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(token in stripped for token in ("ImportError", "ModuleNotFoundError", "AttributeError", "TypeError")):
            return stripped[-500:]
    return "pytest failed; see standard output for the full traceback"


def _python_test_failure_hint(error_excerpt: str) -> str:
    match = re.search(r"cannot import name '([^']+)' from '([^']+)'", str(error_excerpt or ""))
    if match:
        return (
            f"Check pytest/conftest or generated-test stubs for module `{match.group(2)}`; "
            f"the selected test environment must expose `{match.group(1)}` before importing the target."
        )
    if "ModuleNotFoundError" in str(error_excerpt or "") or "ImportError" in str(error_excerpt or ""):
        return "Check Python dependencies and test stubs used by pytest before importing the target module."
    return "Repair the selected target-specific unit test, then rerun Python enforcement."


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]
