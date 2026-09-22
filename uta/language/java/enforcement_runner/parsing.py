"""Reading Maven/PIT output: what the enforcement run actually reported.

Everything here takes text (or a Surefire report on disk) and answers one
question about it -- was the gate failed, was PIT scoped, what were the test
strengths, which tests failed. No command is built and no process is started,
so these predicates can be tested against captured output alone.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set


def _has_required_evidence(output: str) -> bool:
    has_coverage = bool(
        re.search(r"\bdiff(?:\s+line)?\s+coverage\s*:?\s+[0-9]+(?:\.[0-9]+)?%", output, flags=re.IGNORECASE)
    )
    has_mutation = bool(
        re.search(
            r"\bdiff\s+mutation\s+score\s+[0-9]+(?:\.[0-9]+)?%\s+passed\s+for\s+[^\s]+\s+"
            r"\(\d+/\d+\s+detected;\s+\d+\s+survived;\s+\d+\s+no\s+coverage\s+excluded\)",
            output,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bno\s+scored\s+PIT\s+mutations\s+on\s+changed\s+lines\s+for\s+[^\s]+\s+"
            r"\(0/0\s+scored;\s+\d+\s+no\s+coverage\s+excluded\)",
            output,
            flags=re.IGNORECASE,
        )
    )
    return has_coverage and has_mutation


def _has_scoped_pitest_targets(output: str) -> bool:
    for match in re.finditer(r"\bpitest\.targets=(\d+)\s+\[([^\]]*)\]", output, flags=re.IGNORECASE):
        if int(match.group(1)) > 0 and match.group(2).strip():
            return True
    return False


def _pitest_target_patterns(output: str) -> List[str]:
    patterns: List[str] = []
    for match in re.finditer(r"\bpitest\.targets=\d+\s+\[([^\]]*)\]", output, flags=re.IGNORECASE):
        raw = match.group(1)
        for item in raw.split(","):
            value = item.strip()
            if value:
                patterns.append(value)
    return list(dict.fromkeys(patterns))


def _filtered_classes_from_pitest_patterns(patterns: Sequence[str], changed_classes: Sequence[str]) -> List[str]:
    matched: List[str] = []
    changed = [str(item) for item in changed_classes if item]
    for pattern in patterns:
        normalized = str(pattern).strip()
        while normalized.endswith("*"):
            normalized = normalized[:-1]
        normalized = normalized.strip(".")
        if not normalized:
            continue
        if "." in normalized:
            matched.append(normalized)
            continue
        suffix = f".{normalized}"
        matched.extend(item for item in changed if item == normalized or item.endswith(suffix))
    return list(dict.fromkeys(matched))


def _looks_skipped(output: str) -> bool:
    lowered = output.lower()
    return "skip test enforcement" in lowered or "test-enforcement skipped" in lowered


def _looks_no_enforceable_java_lines(output: str) -> bool:
    lowered = output.lower()
    # Reactor roots and sibling modules commonly have no retained lines while a
    # changed child module still owns PIT targets. Those empty-module markers
    # must never turn a missing check-mutation verdict into a pass.
    if _has_scoped_pitest_targets(output):
        return False
    return (
        "test-enforcer" in lowered
        and "no changed java source lines" in lowered
        and "build success" in lowered
    )


def _looks_gate_failed(output: str) -> bool:
    lowered = output.lower()
    failure_markers = (
        "test-enforcement failed",
        "test enforcement failed",
        "diff coverage failed",
        "diff coverage check failed",
        "coverage gate failed",
        "mutation gate failed",
        "mutation coverage failed",
        "mutation score is below",
        "test-strength failed",
        "test strength failed",
        "test strength score",
        "test-enforcer check-coverage failed",
        "test-enforcer check-mutation failed",
        "check-coverage failed",
        "check-mutation failed",
    )
    return any(marker in lowered for marker in failure_markers) or (
        "diff line coverage" in lowered and "below required" in lowered
    ) or (
        "test strength" in lowered and "below threshold" in lowered
    )


def _looks_pitest_baseline_failure(output: str) -> bool:
    lowered = output.lower()
    return (
        "mutation testing requires a green suite" in lowered
        or "tests failing without mutation" in lowered
        or "tests did not pass without mutation" in lowered
    )


def _looks_build_broken(output: str) -> bool:
    lowered = output.lower()
    build_break_markers = (
        "dependencyresolutionexception",
        "could not resolve dependencies",
        "could not find artifact",
        "could not collect dependencies",
        "failed to collect dependencies",
        "compilation failure",
        "compilation error",
        "fatal error compiling",
        "maven-compiler-plugin",
        "cannot find symbol",
    )
    return any(marker in lowered for marker in build_break_markers) or (
        "package " in lowered and " does not exist" in lowered
    )


def _diff_mutation_scores(output: str) -> List[float]:
    return [
        float(match.group(1))
        for match in re.finditer(
            r"\bdiff\s+mutation\s+score\s+([0-9]+(?:\.[0-9]+)?)%\s+passed\b",
            output,
            flags=re.IGNORECASE,
        )
    ]


def _diff_coverage_rates(output: str) -> List[float]:
    """Every diff line-coverage percentage the plugin printed."""
    return [
        float(match.group(1))
        for match in re.finditer(
            r"\bdiff(?:\s+line)?\s+coverage\s*:?\s+([0-9]+(?:\.[0-9]+)?)%",
            output,
            flags=re.IGNORECASE,
        )
    ]


#: One PIT statistics block. The gap is bounded so "no coverage" is read from the
#: same module's block as the "Generated ... Killed ..." line above it, never the
#: next module's.
#: The compat extension prints this only after reading the value back off the PIT
#: mojo, so it is evidence the flag reached PIT -- not that UTA asked for it.
_PIT_SKIP_FAILING_APPLIED = re.compile(
    r"\[uta-pit-compat\][^\n]*\bskipFailingTests=true\b", flags=re.IGNORECASE
)

_PIT_STATISTICS_BLOCK = re.compile(
    r"Generated\s+(\d+)\s+mutations\s+Killed\s+(\d+)\s+\([0-9]+(?:\.[0-9]+)?%\)"
    r".{0,200}?Mutations with no coverage\s+(\d+)\.",
    flags=re.IGNORECASE | re.DOTALL,
)


def _pit_modules_without_covered_mutants(output: str) -> List[int]:
    """Mutation counts for modules PIT scored with an empty denominator.

    Test strength is killed/covered, so a module where no mutant was reached by
    any test reports 100% on zero evidence -- the vacuous pass that made
    `skipFailingTests` a ship NO-GO, since the only tests covering a changed class
    may be exactly the red ones PIT dropped.

    Read only when the compat extension reports that PIT actually took
    `skipFailingTests`. Without the flag an all-uncovered block is the ordinary
    consequence of PIT mutating a whole class whose changed lines are covered but
    whose remainder is not, which this gate has accepted since 2026-05.
    """
    if not _PIT_SKIP_FAILING_APPLIED.search(output):
        return []
    empty: List[int] = []
    for match in _PIT_STATISTICS_BLOCK.finditer(output):
        generated, _killed, no_coverage = (int(value) for value in match.groups())
        if generated > 0 and no_coverage >= generated:
            empty.append(generated)
    return empty


def _failed_surefire_tests(repo_path: Path, limit: int = 12) -> List[Dict[str, str]]:
    failures: List[Dict[str, str]] = []
    for report_path in sorted(Path(repo_path).glob("**/target/surefire-reports/*.txt")):
        if report_path.name.startswith("TEST-"):
            continue
        try:
            text = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<<< FAILURE!" not in text and "<<< ERROR!" not in text:
            continue
        failure = _parse_surefire_text_failure(repo_path, report_path, text)
        if failure:
            failures.append(failure)
            if len(failures) >= limit:
                break
    return failures


def _surefire_report_snapshot(repo_path: Path) -> Dict[str, tuple[int, int]]:
    """Metadata for reports that predate the enforcement invocation."""
    snapshot: Dict[str, tuple[int, int]] = {}
    for report_path in Path(repo_path).glob("**/target/surefire-reports/TEST-*.xml"):
        try:
            stat = report_path.stat()
            snapshot[str(report_path.resolve())] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snapshot


def _surefire_test_class_results(
    repo_path: Path,
    previous_reports: Optional[Dict[str, tuple[int, int]]] = None,
) -> Dict[str, bool]:
    """Every test class Surefire reported, mapped to whether it failed.

    The `.txt` reader above is for humans and stops at twelve records. This one
    decides which classes leave the enforcement run, so it must be complete and
    it must be right about the class name: `TEST-*.xml` is always written and
    carries the fully qualified name as an attribute, while the `.txt` form is
    conditional on `useFile` and degrades to a regex that needs a Test/IT
    suffix.
    """
    results: Dict[str, bool] = {}
    for report_path in sorted(Path(repo_path).glob("**/target/surefire-reports/TEST-*.xml")):
        try:
            stat = report_path.stat()
            if previous_reports is not None and previous_reports.get(
                str(report_path.resolve())
            ) == (stat.st_mtime_ns, stat.st_size):
                continue
            root = ET.parse(report_path).getroot()
        except (ET.ParseError, OSError):
            continue
        name = (root.get("name") or "").strip()
        if not name:
            continue
        try:
            broken = int(root.get("errors") or 0) + int(root.get("failures") or 0)
        except ValueError:
            continue
        results[name] = results.get(name, False) or broken > 0
    return results


def _failed_surefire_test_classes(repo_path: Path) -> List[str]:
    return [name for name, failed in _surefire_test_class_results(repo_path).items() if failed]


#: Surefire 2.13 introduced `surefire.includesFile`. Older reactors receive the
#: same positive inventory through a size-guarded `-Dtest` selector.
_INCLUDES_FILE_MIN_SUREFIRE = (2, 13)

_SUREFIRE_GOAL = re.compile(
    r"---\s+(?:maven-)?surefire(?:-plugin)?:(\d+(?:\.\d+)*[^\s:]*):test\b"
)


def _surefire_versions(output: str) -> List[str]:
    """Every Surefire version the reactor actually ran, as Maven printed it."""
    return sorted(set(_SUREFIRE_GOAL.findall(output or "")))


def _surefire_supports_includes_file(output: str) -> bool:
    """Can the reactor consume UTA's file-backed positive test inventory?

    Read from the first run's own output rather than the POMs: the effective
    version is what a parent POM, a profile and a pluginManagement block argue
    out between them, and only Maven knows the answer. Every module has to
    agree, because `-Dtest` is applied to all of them.

    Unknown means no. A version we could not read is a reactor whose behaviour
    we cannot predict, and the failure it produces is total -- the build dies
    before any gate reports, so there is no verdict to fall back on.
    """
    versions = _surefire_versions(output)
    if not versions:
        return False
    return all(_version_tuple(v) >= _INCLUDES_FILE_MIN_SUREFIRE for v in versions)


def _version_tuple(version: str) -> tuple:
    parts = []
    for chunk in str(version or "").split("."):
        digits = re.match(r"\d+", chunk)
        if not digits:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts)


_REACTOR_SKIPPED = re.compile(r"^\[INFO\]\s+\S.*\bSKIPPED\s*$", re.MULTILINE)


def _reactor_left_modules_unbuilt(output: str) -> bool:
    """Did the first run stop before every module had a chance to run its tests?

    Maven is fail-fast, so a module that fails takes every module after it with
    it -- they are reported SKIPPED and write no Surefire reports at all. That
    matters only to the positive dialect, which builds its "what should run" list
    from those reports: a module the first run never reached contributes no names,
    would run nothing in the re-run, and would report zero coverage for lines that
    are in fact covered. UTA names those modules' tests from source instead
    (`_declared_test_classes_by_module`) and refuses the rerun only when it cannot.
    """
    return bool(_REACTOR_SKIPPED.search(output or ""))


_REACTOR_SKIPPED_MODULE = re.compile(r"^\[INFO\]\s+(\S.*?)\s+\.+\s+SKIPPED\s*$", re.MULTILINE)


def _reactor_skipped_modules(output: str) -> List[str]:
    """The display names the reactor summary reported as SKIPPED, in build order."""
    return list(
        dict.fromkeys(match.group(1).strip() for match in _REACTOR_SKIPPED_MODULE.finditer(output or ""))
    )


def _surefire_defaults_include_tests_suffix(output: str) -> bool:
    """Does every Surefire in the reactor include `**/*Tests.java` by default?

    Surefire 3 added that pattern; 2.x only knows `Test*`, `*Test` and
    `*TestCase`. Unknown means no, so a skipped module is never given a test the
    reactor would not have discovered on its own.
    """
    versions = _surefire_versions(output)
    return bool(versions) and all(_version_tuple(v) >= (3, 0) for v in versions)


def _surefire_test_classes_by_module(
    repo_path: Path,
    previous_reports: Optional[Dict[str, tuple[int, int]]] = None,
) -> Dict[str, Set[str]]:
    """Every test class Surefire reported, grouped by the module that ran it.

    The flat view answers "did this class fail". This one answers "what would be
    left if these classes were excluded", and the module is the unit that
    question has to be asked about: Maven applies the inventory to every module in
    the reactor, so an exclusion list that leaves plenty of tests repo-wide can
    still empty a single module. Surefire then aborts that module with
    `No tests were executed!`, and the reactor never reaches the modules after
    it -- which is how a real coverage verdict became no verdict at all.

    Keyed by the module directory relative to the repository root; the root
    module itself is the empty string.
    """
    modules: Dict[str, Set[str]] = {}
    root = Path(repo_path)
    for report_path in sorted(root.glob("**/target/surefire-reports/TEST-*.xml")):
        try:
            stat = report_path.stat()
            if previous_reports is not None and previous_reports.get(
                str(report_path.resolve())
            ) == (stat.st_mtime_ns, stat.st_size):
                continue
            name = (ET.parse(report_path).getroot().get("name") or "").strip()
        except (ET.ParseError, OSError):
            continue
        if not name:
            continue
        try:
            module = report_path.parents[2].relative_to(root).as_posix()
        except ValueError:
            continue
        modules.setdefault("" if module == "." else module, set()).add(name)
    return modules


def _parse_surefire_text_failure(repo_path: Path, report_path: Path, text: str) -> Dict[str, str]:
    class_name = ""
    summary = ""
    test_name = ""
    failure_type = ""
    message = ""
    source_location = ""
    lines = text.splitlines()
    for line in lines:
        if line.startswith("Test set: "):
            class_name = line.split("Test set: ", 1)[1].strip()
        elif not summary and line.startswith("Tests run: "):
            summary = line.strip()
        match = re.match(r"([A-Za-z_][\w.$]*(?:Test|Tests|IT)\.[\w$]+)\s+--.*<<<\s+(FAILURE|ERROR)!", line)
        if match and not test_name:
            test_name = match.group(1)
            failure_type = match.group(2).lower()
    if test_name and not class_name:
        class_name = test_name.rsplit(".", 1)[0]
    if not class_name and not test_name:
        return {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("at "):
            continue
        if (
            (".AssertionError:" in stripped or ".Exception:" in stripped or ".Error:" in stripped)
            and not stripped.startswith("Tests run:")
        ):
            message = stripped
            break
    if class_name:
        source_match = re.search(
            rf"\bat\s+{re.escape(class_name)}\.[\w$<>]+\(({re.escape(class_name.rsplit('.', 1)[-1])}\.java:\d+)\)",
            text,
        )
        if source_match:
            source_location = source_match.group(1)
    rel_path = str(report_path)
    try:
        rel_path = str(report_path.relative_to(repo_path))
    except ValueError:
        pass
    return {
        "className": class_name,
        "testName": test_name,
        "type": failure_type,
        "summary": summary,
        "message": message,
        "sourceLocation": source_location,
        "reportPath": rel_path,
    }


__all__ = [
    "_diff_coverage_rates",
    "_diff_mutation_scores",
    "_failed_surefire_test_classes",
    "_surefire_test_class_results",
    "_failed_surefire_tests",
    "_filtered_classes_from_pitest_patterns",
    "_has_required_evidence",
    "_has_scoped_pitest_targets",
    "_looks_build_broken",
    "_looks_gate_failed",
    "_looks_no_enforceable_java_lines",
    "_looks_pitest_baseline_failure",
    "_has_test_enforcer_marker",
    "_looks_skipped",
    "_parse_surefire_text_failure",
    "_pitest_target_patterns",
    "_reactor_left_modules_unbuilt",
    "_surefire_report_snapshot",
    "_surefire_supports_includes_file",
    "_surefire_test_classes_by_module",
    "_surefire_versions",
]


def _has_test_enforcer_marker(output: str) -> bool:
    """Did the Maven test-enforcer plugin actually speak?

    In full mode UTA no longer re-derives the scope, so the plugin's own output
    is the only proof that anything was enforced. A build that exits zero
    without ever emitting a `[test-enforcer]` line enforced nothing -- that is
    missing evidence, not a pass.
    """
    return "[test-enforcer]" in (output or "").lower()


# One parser for the plugin's gate-miss sentences. The report panel
# (`ci_evidence.java_gate_failure_summary`) and the task-level summary
# (`classification._gate_failure_summary`) both read the same Maven output, so
# they read it through here -- two regex sets over one log drift apart and then
# describe the same failed run two different ways.
_COVERAGE_MISS = re.compile(
    r"diff\s+(?:line\s+)?coverage\s+([0-9]+(?:\.[0-9]+)?)%\s+is\s+below\s+required\s+"
    r"([0-9]+(?:\.[0-9]+)?)%(?:\s*\((\d+)/(\d+)\))?",
    re.IGNORECASE,
)
_MUTATION_MISS_DETAILED = re.compile(
    r"diff\s+mutation\s+score\s+([0-9]+(?:\.[0-9]+)?)%\s+for\s+[^\s]+\s+"
    r"\((\d+)/(\d+)\s+detected(?:;[^)]*)?\)\s+is\s+below\s+required\s+"
    r"([0-9]+(?:\.[0-9]+)?)%",
    re.IGNORECASE,
)
_MUTATION_MISS = re.compile(
    r"(?:mutation\s+score|test\s+strength)\s+([0-9]+(?:\.[0-9]+)?)%\s+(?:is\s+)?below\s+"
    r"(?:required|threshold|gate)\s+([0-9]+(?:\.[0-9]+)?)%",
    re.IGNORECASE,
)
# PIT's own wording, which prints bare numbers instead of percentages.
_MUTATION_MISS_BARE = re.compile(
    r"(?:test\s+strength|mutation)\s+score\s+of\s+([0-9]+(?:\.[0-9]+)?)\s+is\s+below\s+"
    r"threshold\s+of\s+([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

GATE_MISS_SUMMARY_MAX_CHARS = 700


def coverage_gate_miss_reasons(output: str) -> List[str]:
    """Every diff-coverage gate miss the plugin printed, in its own numbers."""
    reasons: List[str] = []
    for match in _COVERAGE_MISS.finditer(output):
        reason = "Coverage gate failed: %s%% < %s%%" % (match.group(1), match.group(2))
        if match.group(3):
            reason += " (%s/%s)" % (match.group(3), match.group(4))
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def mutation_gate_miss_reasons(output: str) -> List[str]:
    """Every mutation/test-strength gate miss, detailed wording winning ties.

    The detailed and generic sentences describe the same miss, so keying on the
    pair of numbers keeps one reason per miss instead of two renderings of it.
    """
    reasons: Dict[tuple, str] = {}
    for match in _MUTATION_MISS_DETAILED.finditer(output):
        reasons[(match.group(1), match.group(4))] = (
            "Mutation gate failed: %s%% < %s%% (%s/%s detected)"
            % (match.group(1), match.group(4), match.group(2), match.group(3))
        )
    for pattern in (_MUTATION_MISS, _MUTATION_MISS_BARE):
        for match in pattern.finditer(output):
            key = (match.group(1), match.group(2))
            reasons.setdefault(
                key, "Mutation gate failed: %s%% < %s%%" % (match.group(1), match.group(2))
            )
    return list(reasons.values())


def gate_miss_reasons(output: str) -> List[str]:
    """Both gates' misses, coverage first."""
    return coverage_gate_miss_reasons(output) + mutation_gate_miss_reasons(output)


def measured_gate_details(output: str, mutation_gate: float) -> List[str]:
    """Measured coverage/mutation numbers, for when no plugin sentence exists."""
    details: List[str] = []
    coverage = _diff_coverage_rates(output)
    if coverage:
        details.append("diff line coverage %.2f%%" % min(coverage))
    scores = _diff_mutation_scores(output)
    if scores:
        detail = "mutation score %.2f%%" % min(scores)
        if mutation_gate > 0:
            detail += " (gate %.2f%%)" % mutation_gate
        details.append(detail)
    return details


def bounded_reasons(reasons: Sequence[str], max_chars: int = GATE_MISS_SUMMARY_MAX_CHARS) -> str:
    """Join reasons without letting a wide reactor write an unbounded summary.

    One sentence per module means a large reactor can print dozens; this text
    reaches the task record, the report and the outbound RDC callback body.
    """
    text = "; ".join(reasons)
    if len(text) <= max_chars:
        return text
    kept: List[str] = []
    for reason in reasons:
        candidate = "; ".join(kept + [reason])
        if kept and len(candidate) > max_chars - 12:
            break
        kept.append(reason)
    remaining = len(reasons) - len(kept)
    text = "; ".join(kept)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text + (", +%d more" % remaining if remaining > 0 else "")


def unfinished_surefire_class(output: str) -> Optional[str]:
    """Pair Surefire lifecycle lines, including named parallel completions."""
    pending: List[str] = []
    for raw in output.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        line = re.sub(r"^\[(?:INFO|WARNING|ERROR)\]\s*", "", line)
        opened = re.fullmatch(r"Running\s+([\w.$]+)", line)
        if opened:
            pending.append(opened.group(1))
        elif line.startswith("Tests run:"):
            named = re.search(r"(?:- in|-- in)\s+([\w.$]+)", line)
            if named:
                if named.group(1) in pending:
                    pending.remove(named.group(1))
            elif pending:
                pending.pop()
    return pending[-1] if pending else None
