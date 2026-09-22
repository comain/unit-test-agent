from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import ClassVar, Iterable, Mapping, Sequence

MAX_TEST_QUALITY_FILE_BYTES = 128 * 1024
MAX_TEST_QUALITY_FINDINGS_PER_FILE = 20
MAX_TEST_QUALITY_FINDINGS_PER_TARGET = 50
MAX_TEST_QUALITY_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class TestQualityFinding:
    """Advisory signal that a generated test may be weak or implementation-bound."""

    __test__: ClassVar[bool] = False

    language: str
    file_path: str
    rule_id: str
    category: str
    severity: str
    message: str
    line: int | None = None
    evidence: str = ""

    def to_evidence_dict(self) -> dict:
        payload = {
            "language": self.language,
            "filePath": self.file_path,
            "ruleId": self.rule_id,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "evidence": normalize_evidence_snippet(self.evidence),
        }
        return {key: value for key, value in payload.items() if value not in (None, "")}

    @classmethod
    def from_evidence_dict(cls, payload: Mapping[str, object]) -> "TestQualityFinding":
        return cls(
            language=str(payload.get("language") or ""),
            file_path=str(payload.get("filePath") or payload.get("file_path") or ""),
            rule_id=str(payload.get("ruleId") or payload.get("rule_id") or ""),
            category=str(payload.get("category") or ""),
            severity=str(payload.get("severity") or "warning"),
            message=str(payload.get("message") or ""),
            line=_optional_int(payload.get("line")),
            evidence=str(payload.get("evidence") or ""),
        )


def read_bounded_test_file(path: Path, *, max_bytes: int = MAX_TEST_QUALITY_FILE_BYTES) -> str:
    with Path(path).open("rb") as handle:
        data = handle.read(max(0, int(max_bytes)))
    return data.decode("utf-8", errors="replace")


def normalize_evidence_snippet(value: str, *, max_chars: int = MAX_TEST_QUALITY_SNIPPET_CHARS) -> str:
    snippet = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(snippet) <= max_chars:
        return snippet
    return snippet[: max(0, max_chars - 3)].rstrip() + "..."


def cap_findings(
    findings: Iterable[TestQualityFinding],
    *,
    max_findings: int = MAX_TEST_QUALITY_FINDINGS_PER_FILE,
) -> list[TestQualityFinding]:
    capped: list[TestQualityFinding] = []
    for finding in findings:
        capped.append(finding)
        if len(capped) >= max_findings:
            break
    return capped


def aggregate_test_quality(
    findings: Sequence[TestQualityFinding] | Sequence[Mapping[str, object]],
    *,
    max_warnings: int = MAX_TEST_QUALITY_FINDINGS_PER_TARGET,
) -> dict:
    normalized = [
        finding if isinstance(finding, TestQualityFinding) else TestQualityFinding.from_evidence_dict(finding)
        for finding in findings
    ]
    rule_counts = Counter(finding.rule_id for finding in normalized if finding.rule_id)
    return {
        "warningCount": len(normalized),
        "warnings": [finding.to_evidence_dict() for finding in normalized[:max_warnings]],
        "topRuleIds": [
            {"ruleId": rule_id, "count": count}
            for rule_id, count in rule_counts.most_common(10)
        ],
        "scannerFailureCount": sum(1 for finding in normalized if finding.rule_id.endswith("scan-failed")),
    }


def summarize_test_quality_payloads(payloads: Iterable[Mapping[str, object]]) -> dict:
    """Merge already-aggregated test-quality payloads into a compact summary."""
    warning_count = 0
    scanner_failure_count = 0
    rule_counts: Counter[str] = Counter()
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        warning_count += int(payload.get("warningCount") or len(payload.get("warnings") or []))
        scanner_failure_count += int(payload.get("scannerFailureCount") or 0)
        for item in payload.get("topRuleIds") or []:
            if isinstance(item, Mapping) and item.get("ruleId"):
                rule_counts[str(item["ruleId"])] += int(item.get("count") or 0)
    if not warning_count and not scanner_failure_count:
        return {}
    return {
        "warningCount": warning_count,
        "warnings": [],
        "topRuleIds": [
            {"ruleId": rule_id, "count": count}
            for rule_id, count in rule_counts.most_common(10)
        ],
        "scannerFailureCount": scanner_failure_count,
    }


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
