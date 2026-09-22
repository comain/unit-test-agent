from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping, Sequence, Union

from uta.enforcement.test_quality import (
    TestQualityFinding,
    aggregate_test_quality,
    cap_findings,
    normalize_evidence_snippet,
    read_bounded_test_file,
)


def scan_java_test_quality_evidence(repo: Path, test_paths: Sequence[Union[str, Path]]) -> dict:
    """Scan existing Java test files and return aggregated evidence.

    Paths that do not resolve to a file are skipped silently: Java error and
    rate-limited result payloads carry conventional expected paths for test
    files that were never created, and those must not produce scan-failed noise.
    """
    findings: list[TestQualityFinding] = []
    for test_path in test_paths:
        path = Path(test_path)
        abs_path = path if path.is_absolute() else Path(repo) / path
        if not abs_path.is_file():
            continue
        rel_path = _relative_path(Path(repo), abs_path)
        try:
            text = read_bounded_test_file(abs_path)
            findings.extend(scan_java_test_quality(rel_path, text))
        except Exception as exc:
            findings.append(
                TestQualityFinding(
                    language="java",
                    file_path=rel_path.as_posix(),
                    rule_id="java-test-quality-scan-failed",
                    category="scanner_failure",
                    severity="info",
                    message=f"Test quality scanner could not inspect this test file: {type(exc).__name__}",
                )
            )
    return aggregate_test_quality(findings) if findings else {}


def attach_java_test_quality(repo_path: Union[str, Path], results: Mapping[str, dict]) -> None:
    """Enrich per-class workflow result dicts with test-quality evidence in place.

    Never raises: warnings are advisory and must not break task sync or the run.
    """
    repo = Path(repo_path)
    for result in results.values():
        if not isinstance(result, dict) or result.get("testQuality"):
            continue
        paths = [path for path in (result.get("candidate_test_file_paths") or []) if path]
        if not paths and result.get("test_file_path"):
            paths = [result["test_file_path"]]
        if not paths:
            continue
        try:
            quality = scan_java_test_quality_evidence(repo, paths)
        except Exception:
            continue
        if quality:
            result["testQuality"] = quality


def scan_java_test_quality(path: Path, text: str) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    blocks = _java_test_blocks(text)
    has_any_failure_or_boundary = False
    has_no_observable_warning = False
    for test_name, start_line, body in blocks:
        assertion_lines = _java_assertion_lines(body)
        has_failure_or_boundary = _has_failure_or_boundary_evidence(test_name, body, assertion_lines)
        has_any_failure_or_boundary = has_any_failure_or_boundary or has_failure_or_boundary
        if assertion_lines and all(_is_smoke_only_java_assertion(line) for _, line in assertion_lines):
            has_no_observable_warning = True
            line_no, evidence = assertion_lines[0]
            findings.append(
                _finding(
                    path,
                    "java-smoke-only-no-behavior",
                    "smoke_only_no_behavior",
                    "Test only proves execution did not throw; add an assertion on returned value, state, output, or persisted effect.",
                    start_line + line_no - 1,
                    evidence,
                )
            )
            continue
        if not assertion_lines and not re.search(r"\bverify\s*\(", body) and not has_failure_or_boundary:
            has_no_observable_warning = True
            findings.append(
                _finding(
                    path,
                    "java-no-observable-assertion",
                    "no_observable_assertion",
                    "Test executes code without an observable behavior assertion; assert the returned value, state change, emitted error, or persisted effect that users depend on.",
                    start_line,
                    _first_executable_line(body),
                )
            )
            continue
        if assertion_lines and all(_is_weak_java_assertion(line) for _, line in assertion_lines):
            line_no, evidence = assertion_lines[0]
            findings.append(
                _finding(
                    path,
                    "java-weak-not-null" if "assertNotNull" in evidence else "java-weak-size-only",
                    "weak_assertion",
                    "Primary assertions only check non-null or non-empty state; assert a concrete value, state transition, emitted error, or persisted effect.",
                    start_line + line_no - 1,
                    evidence,
                )
            )
        if re.search(r"\bverify\s*\(", body) and not _has_value_assertion(assertion_lines):
            line_no, evidence = _first_matching_line(body, r"\bverify\s*\(")
            findings.append(
                _finding(
                    path,
                    "java-impl-detail-verify-only",
                    "implementation_detail",
                    "Test only verifies a collaborator call without an observable value/state assertion; add an assertion on returned value, state, output, or persisted effect.",
                    start_line + line_no - 1,
                    evidence,
                )
            )
        mirrored = _java_mirrored_formula_line(body)
        if mirrored:
            line_no, evidence = mirrored
            findings.append(
                _finding(
                    path,
                    "java-mirroring-formula",
                    "implementation_mirroring",
                    "Expected value appears to mirror implementation arithmetic instead of a known behavior example.",
                    start_line + line_no - 1,
                    evidence,
                )
            )
    if blocks and not has_any_failure_or_boundary and not has_no_observable_warning:
        findings.append(
            _finding(
                path,
                "java-happy-path-only-hint",
                "happy_path_only_hint",
                "Low-confidence advisory: no exception or failure-path test was recognized in this file; consider boundary and failure-mode cases if the target has error branches.",
                1,
                "",
                severity="info",
            )
        )
    return cap_findings(findings)


def _has_failure_or_boundary_evidence(
    test_name: str,
    body: str,
    assertion_lines: list[tuple[int, str]],
) -> bool:
    return bool(
        _has_exception_assertion(body)
        or _has_try_catch_expected_failure(body)
        or (_name_suggests_negative_path(test_name) and _has_meaningful_java_assertion(assertion_lines))
        or _asserts_error_or_status(body)
    )


def _has_exception_assertion(text: str) -> bool:
    return bool(
        re.search(
            r"assertThrows|expectThrows|catchThrowable|assertThatThrownBy|ExpectedException|@Test\s*\(\s*expected",
            text,
        )
    )


def _has_try_catch_expected_failure(body: str) -> bool:
    return bool(
        re.search(r"\btry\s*\{", body)
        and re.search(r"\}\s*catch\s*\(", body)
        and re.search(
            r"(?:Assert\.|Assertions\.)?fail\s*\(|assertFalse\s*\(\s*true\s*\)|assertTrue\s*\(\s*false\s*\)",
            body,
        )
    )


def _name_suggests_negative_path(name: str) -> bool:
    return bool(
        re.search(
            r"(reject|invalid|error|exception|fail|empty|null|blank|missing|timeout|duplicate|terminal|retry|fallback)",
            name,
            re.IGNORECASE,
        )
    )


def _has_meaningful_java_assertion(assertion_lines: list[tuple[int, str]]) -> bool:
    return any(not _is_weak_java_assertion(line) for _, line in assertion_lines)


def _asserts_error_or_status(body: str) -> bool:
    for line in body.splitlines():
        if not re.search(r"\bassert\w*\s*\(", line):
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


def _java_test_blocks(text: str) -> list[tuple[str, int, str]]:
    matches = list(re.finditer(r"(?m)^\s*@Test\b", text))
    lines = text.splitlines()
    blocks: list[tuple[str, int, str]] = []
    for idx, match in enumerate(matches):
        start_line = text[: match.start()].count("\n") + 1
        end_line = text[: matches[idx + 1].start()].count("\n") + 1 if idx + 1 < len(matches) else len(lines) + 1
        body = "\n".join(lines[start_line - 1:end_line])
        blocks.append((_java_test_method_name(body), start_line, body))
    return blocks


def _java_test_method_name(body: str) -> str:
    match = re.search(
        r"\b(?:public|protected|private)?\s*(?:static\s+)?(?:void|[\w<>\[\], ?]+)\s+([A-Za-z_]\w*)\s*\(",
        body,
    )
    return match.group(1) if match else ""


def _java_assertion_lines(body: str) -> list[tuple[int, str]]:
    return [
        (idx, line.strip())
        for idx, line in enumerate(body.splitlines(), start=1)
        if re.search(r"\bassert[A-Z][A-Za-z0-9_]*\s*\(", line)
    ]


def _is_weak_java_assertion(line: str) -> bool:
    return bool(
        re.search(r"\bassertNotNull\s*\(", line)
        or re.search(r"\bassertTrue\s*\([^)]*\.size\(\)\s*>\s*0\)", line)
        or re.search(r"\bassertFalse\s*\([^)]*\.isEmpty\(\)\)", line)
    )


def _is_smoke_only_java_assertion(line: str) -> bool:
    return bool(re.search(r"\bassertDoesNotThrow\s*\(", line))


def _has_value_assertion(assertion_lines: list[tuple[int, str]]) -> bool:
    return any(not _is_weak_java_assertion(line) for _, line in assertion_lines)


def _first_matching_line(body: str, pattern: str) -> tuple[int, str]:
    for idx, line in enumerate(body.splitlines(), start=1):
        if re.search(pattern, line):
            return idx, line.strip()
    return 1, ""


def _first_executable_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("@") and not stripped.endswith("{") and not stripped.startswith("//"):
            return stripped
    return ""


def _java_mirrored_formula_line(body: str) -> tuple[int, str] | None:
    for idx, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        match = re.search(r"\bexpected\w*\s*=\s*(.+);", stripped)
        if match and _is_input_derived_arithmetic(match.group(1).strip()):
            return idx, stripped
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
        language="java",
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
