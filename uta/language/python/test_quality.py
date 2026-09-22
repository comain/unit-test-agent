from __future__ import annotations

from pathlib import Path
import re
from typing import Sequence, Union

from uta.enforcement.test_quality import (
    TestQualityFinding,
    aggregate_test_quality,
    cap_findings,
    normalize_evidence_snippet,
    read_bounded_test_file,
)


def scan_python_test_quality_evidence(repo: Path, test_paths: Sequence[Union[str, Path]]) -> dict:
    findings: list[TestQualityFinding] = []
    for test_path in test_paths:
        path = Path(test_path)
        abs_path = path if path.is_absolute() else Path(repo) / path
        rel_path = _relative_path(Path(repo), abs_path)
        try:
            text = read_bounded_test_file(abs_path)
            findings.extend(scan_python_test_quality(rel_path, text))
        except Exception as exc:
            findings.append(
                TestQualityFinding(
                    language="python",
                    file_path=rel_path.as_posix(),
                    rule_id="python-test-quality-scan-failed",
                    category="scanner_failure",
                    severity="info",
                    message=f"Test quality scanner could not inspect this test file: {type(exc).__name__}",
                )
            )
    return aggregate_test_quality(findings) if findings else {}


def scan_python_test_quality(path: Path, text: str) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    blocks = _python_test_blocks(text)
    has_any_failure_or_boundary = False
    has_no_observable_warning = False
    for test_name, start_line, body in blocks:
        assertion_lines = _assertion_lines(body)
        has_failure_or_boundary = _has_failure_or_boundary_evidence(test_name, body, assertion_lines)
        has_any_failure_or_boundary = has_any_failure_or_boundary or has_failure_or_boundary
        if not assertion_lines and _has_smoke_only_context(body) and not has_failure_or_boundary:
            has_no_observable_warning = True
            findings.append(
                _finding(
                    path,
                    "python-smoke-only-no-behavior",
                    "smoke_only_no_behavior",
                    "Test only proves execution did not raise; add an assertion on returned value, state, output, or persisted effect.",
                    start_line,
                    _first_executable_line(body),
                )
            )
            continue
        if not assertion_lines and not _has_mock_call_count(body) and not has_failure_or_boundary:
            has_no_observable_warning = True
            findings.append(
                _finding(
                    path,
                    "python-no-observable-assertion",
                    "no_observable_assertion",
                    "Test executes code without an observable behavior assertion; assert the returned value, state change, emitted error, or persisted effect that users depend on.",
                    start_line,
                    _first_executable_line(body),
                )
            )
            continue
        if assertion_lines and all(_is_weak_python_assertion(line) for _, line in assertion_lines):
            line_no, evidence = assertion_lines[0]
            findings.append(
                _finding(
                    path,
                    "python-weak-len-only" if "len(" in evidence else "python-weak-assert-not-none",
                    "weak_assertion",
                    "Primary assertions only check existence, truthiness, or non-empty length; assert a concrete value, state transition, emitted error, or persisted effect.",
                    start_line + line_no,
                    evidence,
                )
            )
        if _has_mock_call_count(body) and not _has_non_mock_value_assertion(assertion_lines):
            line_no, evidence = _first_matching_line(body, r"assert_called_once(?:_with)?\(")
            findings.append(
                _finding(
                    path,
                    "python-impl-detail-mock-call-count",
                    "implementation_detail",
                    "Test only verifies a mock call count without an observable behavior assertion; add an assertion on returned value, state, output, or persisted effect.",
                    start_line + line_no,
                    evidence,
                )
            )
        mirrored = _mirrored_formula_line(body)
        if mirrored:
            line_no, evidence = mirrored
            findings.append(
                _finding(
                    path,
                    "python-mirroring-formula",
                    "implementation_mirroring",
                    "Expected value appears to mirror implementation arithmetic instead of a known behavior example.",
                    start_line + line_no,
                    evidence,
                )
            )
    if blocks and not has_any_failure_or_boundary and not has_no_observable_warning:
        findings.append(
            _finding(
                path,
                "python-happy-path-only-hint",
                "happy_path_only_hint",
                "Low-confidence advisory: no exception or failure-path test was recognized in this file; consider boundary and failure-mode cases if the target has error branches.",
                1,
                "",
                severity="info",
            )
        )
    return cap_findings(findings)


def _python_test_blocks(text: str) -> list[tuple[str, int, str]]:
    matches = list(re.finditer(r"(?m)^[ \t]*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", text))
    blocks: list[tuple[str, int, str]] = []
    lines = text.splitlines()
    for idx, match in enumerate(matches):
        start_line = text[: match.start()].count("\n") + 1
        end_line = text[: matches[idx + 1].start()].count("\n") + 1 if idx + 1 < len(matches) else len(lines) + 1
        body = "\n".join(lines[start_line:end_line])
        blocks.append((match.group(1), start_line, body))
    return blocks


def _assertion_lines(body: str) -> list[tuple[int, str]]:
    return [
        (idx, line.strip())
        for idx, line in enumerate(body.splitlines(), start=1)
        if _is_assertion_line(line.strip())
    ]


def _is_assertion_line(stripped: str) -> bool:
    return stripped.startswith("assert ") or bool(re.match(r"(?:self|cls)\.assert[A-Z]\w*\(", stripped))


def _is_weak_python_assertion(line: str) -> bool:
    return bool(
        re.fullmatch(r"assert\s+.+\s+is\s+not\s+None", line)
        or re.fullmatch(r"assert\s+[A-Za-z_][\w.]*", line)
        or re.fullmatch(r"assert\s+bool\([^)]+\)", line)
        or re.fullmatch(r"assert\s+len\([^)]+\)\s*>\s*0", line)
        or re.fullmatch(r"(?:self|cls)\.assertIsNotNone\([^)]*\)", line)
        or re.fullmatch(r"(?:self|cls)\.assertTrue\([A-Za-z_][\w.]*\)", line)
        or re.fullmatch(r"(?:self|cls)\.assertGreater\(len\([^)]+\),\s*0\)", line)
    )


def _has_failure_or_boundary_evidence(
    test_name: str,
    body: str,
    assertion_lines: list[tuple[int, str]],
) -> bool:
    return bool(
        _has_exception_assertion(body)
        or _has_try_except_expected_failure(body)
        or (_name_suggests_negative_path(test_name) and _has_meaningful_python_assertion(assertion_lines))
        or _asserts_error_or_status(body)
    )


def _has_exception_assertion(text: str) -> bool:
    return bool(re.search(r"pytest\.raises|\braises\s*\(|assertRaises", text))


def _has_try_except_expected_failure(body: str) -> bool:
    return bool(
        re.search(r"\btry\s*:", body)
        and re.search(r"\bexcept\b", body)
        and re.search(r"pytest\.fail\s*\(|(?:self|cls)\.fail\s*\(|assert\s+False\b|raise\s+AssertionError\b", body)
    )


def _name_suggests_negative_path(name: str) -> bool:
    return bool(
        re.search(
            r"(reject|invalid|error|exception|fail|empty|none|null|blank|missing|timeout|duplicate|fallback|retry)",
            name,
            re.IGNORECASE,
        )
    )


def _has_meaningful_python_assertion(assertion_lines: list[tuple[int, str]]) -> bool:
    return any(not _is_weak_python_assertion(line) for _, line in assertion_lines)


def _asserts_error_or_status(body: str) -> bool:
    for line in body.splitlines():
        if "assert" not in line:
            continue
        if re.search(r"(error|message|reason|failed|failure)", line, re.IGNORECASE):
            return True
        if re.search(r"(status|state|code)", line, re.IGNORECASE) and re.search(
            r"(fail|error|reject|invalid|fallback|retry|timeout|missing|duplicate|terminal|blank|empty)",
            line,
            re.IGNORECASE,
        ):
            return True
    return False


def _has_mock_call_count(body: str) -> bool:
    return bool(re.search(r"\.assert_called_once(?:_with)?\(", body))


def _has_smoke_only_context(body: str) -> bool:
    return bool(re.search(r"\b(?:does_not_raise|not_raises|suppress)\s*\(", body))


def _has_non_mock_value_assertion(assertion_lines: list[tuple[int, str]]) -> bool:
    return any(not _is_weak_python_assertion(line) for _, line in assertion_lines)


def _first_matching_line(body: str, pattern: str) -> tuple[int, str]:
    for idx, line in enumerate(body.splitlines(), start=1):
        if re.search(pattern, line):
            return idx, line.strip()
    return 1, ""


def _first_executable_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _mirrored_formula_line(body: str) -> tuple[int, str] | None:
    assignments: dict[str, tuple[int, str]] = {}
    for idx, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        match = re.match(r"(expected|want|expected_[A-Za-z0-9_]*)\s*=\s*(.+)", stripped)
        if match and _is_input_derived_arithmetic(match.group(2).strip()):
            assignments[match.group(1)] = (idx, match.group(2).strip())
            continue
        if _is_assertion_line(stripped):
            for name, (line_no, expr) in assignments.items():
                if re.search(rf"\b{re.escape(name)}\b", stripped):
                    return line_no, f"{name} = {expr}"
    return None


def _is_input_derived_arithmetic(expr: str) -> bool:
    # Mirroring needs a formula over variables; literal arithmetic or operators
    # inside string literals are legitimate explicit expectations.
    without_strings = re.sub(r"\"[^\"]*\"|'[^']*'", "", expr)
    if not re.search(r"[+\-*/]", without_strings):
        return False
    return bool(re.search(r"[A-Za-z_]\w*", without_strings))


def _finding(
    path: Path,
    rule_id: str,
    category: str,
    message: str,
    line: int,
    evidence: str,
    *,
    severity: str = "warning",
) -> TestQualityFinding:
    return TestQualityFinding(
        language="python",
        file_path=Path(path).as_posix(),
        rule_id=rule_id,
        category=category,
        severity=severity,
        message=message,
        line=line,
        evidence=normalize_evidence_snippet(evidence),
    )


def _relative_path(repo: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(repo.resolve())
    except ValueError:
        return path
