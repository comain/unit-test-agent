from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Mapping, Optional

from uta.language.python.verification.mutmut_runtime import (
    _changed_line_payload,
    _normalize_changed_lines,
    _normalize_relpath,
)


@dataclass(frozen=True)
class CoverageSummary:
    covered: int
    total: int
    rate: float
    gate: float
    passed: bool
    xml_path: str
    scope: str = "target_file"
    changed_lines: Dict[str, List[int]] = field(default_factory=dict)
    uncovered_lines: Dict[str, List[int]] = field(default_factory=dict)
    no_executable_changed_lines: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "covered": self.covered,
            "total": self.total,
            "rate": self.rate,
            "gate": self.gate,
            "passed": self.passed,
            "xml_path": self.xml_path,
            "scope": self.scope,
            "changed_lines": dict(self.changed_lines),
            "uncovered_lines": dict(self.uncovered_lines),
            "no_executable_changed_lines": self.no_executable_changed_lines,
        }


@dataclass(frozen=True)
class MutationSummary:
    runtime_lane: str
    generated: int
    killed: int
    survived: int
    no_coverage: int
    rate: float
    gate: float
    passed: bool
    no_tests: int = 0
    timeout: int = 0
    suspicious: int = 0
    skipped: int = 0
    caught_by_type_check: int = 0
    survivors: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    scope: str = "target_file"
    changed_lines: Dict[str, List[int]] = field(default_factory=dict)
    diff_survivors: List[Dict[str, Any]] = field(default_factory=list)
    changed_line_mutants_generated: int = 0
    changed_line_mutants_killed: int = 0
    changed_line_mutants_scored: int = 0
    sampling: Dict[str, Any] = field(default_factory=dict)
    candidate_plan: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "runtime_lane": self.runtime_lane,
            "generated": self.generated,
            "killed": self.killed,
            "survived": self.survived,
            "no_coverage": self.no_coverage,
            "no_tests": self.no_tests,
            "timeout": self.timeout,
            "suspicious": self.suspicious,
            "skipped": self.skipped,
            "caught_by_type_check": self.caught_by_type_check,
            "notChecked": int(self.candidate_plan.get("notChecked") or 0),
            "rate": self.rate,
            "gate": self.gate,
            "passed": self.passed,
            "survivors": list(self.survivors),
            "artifacts": dict(self.artifacts),
            "scope": self.scope,
            "changed_lines": dict(self.changed_lines),
            "diff_survivors": list(self.diff_survivors),
            "changedLineMutantsGenerated": self.changed_line_mutants_generated,
            "changedLineMutantsKilled": self.changed_line_mutants_killed,
            "changedLineMutantsScored": self.changed_line_mutants_scored,
            "sampling": dict(self.sampling),
        }
        if self.candidate_plan:
            payload["candidatePlan"] = dict(self.candidate_plan)
        return payload


def parse_coverage_xml(
    xml_path: Path,
    source_paths: Iterable[str],
    *,
    gate: float,
    changed_lines: Optional[Mapping[str, Iterable[int]]] = None,
) -> CoverageSummary:
    normalized_sources = {_normalize_relpath(path) for path in source_paths if path}
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    requested_changed_lines = (
        normalized_changed_lines is not None
        and any(normalized_changed_lines.get(source, set()) for source in normalized_sources)
    )
    root = ET.parse(xml_path).getroot()
    covered = 0
    total = 0
    uncovered_lines: Dict[str, List[int]] = {}
    source_seen = False
    for class_node in root.findall(".//class"):
        filename = _normalize_relpath(class_node.attrib.get("filename", ""))
        if filename not in normalized_sources:
            continue
        source_seen = True
        for line in class_node.findall(".//line"):
            line_number = int(line.attrib.get("number") or 0)
            if normalized_changed_lines is not None and line_number not in normalized_changed_lines.get(filename, set()):
                continue
            total += 1
            if int(line.attrib.get("hits") or 0) > 0:
                covered += 1
            else:
                uncovered_lines.setdefault(filename, []).append(line_number)
    no_executable_changed_lines = requested_changed_lines and source_seen and total == 0
    if requested_changed_lines and total == 0 and not source_seen:
        rate = 0.0
    else:
        rate = 100.0 if total == 0 else round((covered / total) * 100.0, 4)
    return CoverageSummary(
        covered=covered,
        total=total,
        rate=rate,
        gate=float(gate),
        passed=rate >= float(gate),
        xml_path=str(xml_path),
        scope="changed_lines" if normalized_changed_lines is not None else "target_file",
        changed_lines=_changed_line_payload(normalized_changed_lines, normalized_sources),
        uncovered_lines=uncovered_lines,
        no_executable_changed_lines=no_executable_changed_lines,
    )


def parse_mutmut_summary(text: str, *, gate: float, runtime_lane: str) -> MutationSummary:
    generated = _extract_count(text, "generated")
    killed = _extract_count(text, "killed")
    survived = _extract_count(text, "survived")
    no_coverage = _extract_count(text, "no coverage", "no_coverage", "no-coverage")
    no_tests = _extract_count(text, "no tests", "no_tests", "untested")
    timeout = _extract_count(text, "timeout", "timed out")
    suspicious = _extract_count(text, "suspicious")
    skipped = _extract_count(text, "skipped")
    caught_by_type_check = _extract_count(text, "caught by type check", "caught_by_type_check")
    if generated == 0:
        progress = _extract_mutmut_progress_counts(text)
        if progress:
            generated = progress["generated"]
            killed = progress["killed"]
            survived = progress["survived"]
            no_coverage = progress["no_coverage"]
            no_tests = progress["no_tests"]
            timeout = progress["timeout"]
            suspicious = progress["suspicious"]
            skipped = progress["skipped"]
            caught_by_type_check = progress["caught_by_type_check"]
    if generated == 0:
        generated = killed + survived + no_coverage + no_tests + timeout + suspicious + skipped + caught_by_type_check
    detected = killed + caught_by_type_check
    # mutmut's no-tests bucket means the mutant was not mapped to a test case.
    # Coverage is gated separately, so mutation strength scores only mutants
    # that were actually test-associated and executable by mutmut.
    denominator = detected + survived + timeout + suspicious
    rate = 100.0 if denominator == 0 else round((detected / denominator) * 100.0, 4)
    return MutationSummary(
        runtime_lane=runtime_lane,
        generated=generated,
        killed=detected,
        survived=survived,
        no_coverage=no_coverage,
        rate=rate,
        gate=float(gate),
        passed=rate >= float(gate),
        no_tests=no_tests,
        timeout=timeout,
        suspicious=suspicious,
        skipped=skipped,
        caught_by_type_check=caught_by_type_check,
    )


def parse_mutmut_survivors(text: str) -> List[Dict[str, Any]]:
    survivors: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        if not re.search(r"\bSURVIVED\b", line, flags=re.IGNORECASE):
            continue
        mutant_id = _mutmut_id_from_line(line)
        match = re.search(r"([^:\s]+\.py):(\d+)\s*(.*)$", line, flags=re.IGNORECASE)
        if not match:
            if mutant_id:
                survivors.append({"file": "", "line": 0, "description": "survived", "id": mutant_id})
            continue
        tail = match.group(3).strip()
        survivors.append(
            {
                "file": _normalize_relpath(match.group(1)),
                "line": int(match.group(2)),
                "description": tail,
                **({"id": mutant_id} if mutant_id else {}),
            }
        )
    return survivors


def _mutmut_id_from_line(line: str) -> Optional[str]:
    match = re.search(r"\b([A-Za-z0-9_.]+(?:\.x_|\.xǁ)[^\s:]*__mutmut_\d+)\b", str(line or ""))
    if match:
        return match.group(1)
    return None


def _extract_count(text: str, *labels: str) -> int:
    haystack = str(text or "")
    for label in labels:
        patterns = [
            rf"(\d+)\s+{re.escape(label)}",
            rf"{re.escape(label)}\s*[=:]\s*(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, haystack, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
    return 0


def _extract_mutmut_progress_counts(text: str) -> Optional[Dict[str, int]]:
    best: Optional[Dict[str, int]] = None
    for line in str(text or "").splitlines():
        match = re.search(r"(?P<done>\d+)\s*/\s*(?P<generated>\d+)", line)
        if not match:
            continue
        generated = int(match.group("generated"))
        tail = line[match.end() :]
        emoji_counts = {
            emoji: int(value)
            for emoji, value in re.findall(r"([🎉🫥⏰🤔🙁🔇🧙])\s*(\d+)", tail)
        }
        if emoji_counts:
            best = {
                "generated": generated,
                "killed": emoji_counts.get("🎉", 0),
                "no_tests": emoji_counts.get("🫥", 0),
                "timeout": emoji_counts.get("⏰", 0),
                "suspicious": emoji_counts.get("🤔", 0),
                "survived": emoji_counts.get("🙁", 0),
                "skipped": emoji_counts.get("🔇", 0),
                "caught_by_type_check": emoji_counts.get("🧙", 0),
                "no_coverage": 0,
            }
            continue
        counts = [int(value) for value in re.findall(r"\b\d+\b", tail)]
        if len(counts) < 4:
            continue
        # Older mutmut output carries at least four counts after done/generated:
        # killed, timeout, suspicious, survived. Newer output is parsed above by
        # emoji so the "no tests" bucket is not confused with killed or survived.
        best = {
            "generated": generated,
            "killed": counts[0],
            "no_tests": 0,
            "survived": counts[3],
            "timeout": counts[1],
            "suspicious": counts[2],
            "skipped": counts[4] if len(counts) > 4 else 0,
            "caught_by_type_check": counts[5] if len(counts) > 5 else 0,
            "no_coverage": 0,
        }
    return best
