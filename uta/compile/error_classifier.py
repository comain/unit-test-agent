"""Parse and classify `mvn test-compile` output into typed error records.

Supports strategies D (error-delta fix loops), G (hopeless-loop detection),
K-subset (replace LLM categorization with Python), and L1 (inefficiency recorder).

Design goals:
- No network / no subprocess — pure text-in, records-out.
- Tolerant to variations in Maven / javac output formatting.
- Stable per-record signature for deduplication across fix iterations.

Only a handful of categories; unknown errors fall into "other" so nothing is lost.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# Categories are intentionally coarse; downstream callers can refine.
CATEGORY_MISSING_IMPORT = "missing_import"
CATEGORY_UNRESOLVED_SYMBOL = "unresolved_symbol"
CATEGORY_WRONG_TYPE = "wrong_type"
CATEGORY_MOCKITO_API = "mockito_api"
CATEGORY_SYNTAX = "syntax"
CATEGORY_DEPRECATED_API = "deprecated_api"
CATEGORY_OTHER = "other"

_CATEGORY_ORDER = [
    CATEGORY_MISSING_IMPORT,
    CATEGORY_UNRESOLVED_SYMBOL,
    CATEGORY_WRONG_TYPE,
    CATEGORY_MOCKITO_API,
    CATEGORY_SYNTAX,
    CATEGORY_DEPRECATED_API,
    CATEGORY_OTHER,
]

# Matches these Maven/javac output formats:
#   [ERROR] /abs/path/Foo.java:[42,17] cannot find symbol
#   /abs/path/Foo.java:42: error: cannot find symbol
#
# The two variants after the file path are:
#   :[line,col] msg
#   :line: [error:] msg
_LOC_RE = re.compile(
    r"(?P<path>[\w./\\\- ]+\.java)"
    r"(?:"
    r":\[(?P<line1>\d+),\d+\]\s*"   # :[line,col] form
    r"|:(?P<line2>\d+):\s*(?:error:\s*)?"  # :line: form
    r")"
    r"(?P<msg>[^\n]*)"
)


@dataclass(frozen=True)
class CompileError:
    """One classified compile error."""

    category: str
    file: str
    line: int
    message: str
    # The symbol / type that triggered the error, when we can extract it.
    symbol: Optional[str] = None
    # Extra follow-up lines from javac (e.g. the "symbol:" / "location:" lines).
    detail: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def signature(self) -> str:
        """Stable hash used to detect identical errors across fix loops."""
        base = f"{self.category}|{self.file}|{self.line}|{self.symbol or ''}|{self.message.strip()}"
        return hashlib.blake2b(base.encode("utf-8"), digest_size=10).hexdigest()


def error_signature(error: CompileError) -> str:
    return error.signature


def classify_compile_errors(raw: str) -> List[CompileError]:
    """Parse raw `mvn test-compile` output into CompileError records.

    Multi-line javac errors (cannot find symbol + symbol: + location:) are grouped
    into one record with the follow-up lines in ``detail``.
    """
    if not raw:
        return []

    errors: List[CompileError] = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _LOC_RE.search(line)
        if not match:
            i += 1
            continue

        msg = match.group("msg").strip() if match.group("msg") else ""
        line_num = int(match.group("line1") or match.group("line2") or "0")
        detail: List[str] = []
        j = i + 1
        # Group javac's "symbol: class Foo" / "location: class Bar" follow-ups.
        while j < len(lines):
            follow = lines[j].strip()
            if not follow:
                break
            if follow.startswith("symbol:") or follow.startswith("location:") or follow.startswith("required:") or follow.startswith("found:") or follow.startswith("required type") or follow.startswith("reason:"):
                detail.append(follow)
                j += 1
                continue
            break

        category, symbol = _categorize(msg, detail)
        errors.append(
            CompileError(
                category=category,
                file=match.group("path").strip(),
                line=line_num,
                message=msg,
                symbol=symbol,
                detail=tuple(detail),
            )
        )
        i = j

    return errors


def _categorize(message: str, detail: Sequence[str]) -> Tuple[str, Optional[str]]:
    """Pick the most specific category that matches the message/detail block."""
    msg = message or ""
    lower = msg.lower()

    # Missing package/import.
    m = re.search(r"package\s+([\w\.]+)\s+does not exist", msg)
    if m:
        return CATEGORY_MISSING_IMPORT, m.group(1)

    # Cannot find symbol — javac follows up with "symbol: class Foo" etc.
    if "cannot find symbol" in lower:
        symbol = None
        for line in detail:
            m = re.search(r"symbol:\s*(?:class|variable|method)\s+([\w\.\$]+)", line)
            if m:
                symbol = m.group(1)
                break
        return CATEGORY_UNRESOLVED_SYMBOL, symbol

    # Deprecated Mockito APIs flagged either as warnings or by our guidance.
    if "org.mockito.matchers" in lower or "anylistof" in lower:
        return CATEGORY_MOCKITO_API, "org.mockito.ArgumentMatchers"
    if "has been deprecated" in lower:
        return CATEGORY_DEPRECATED_API, None

    # Type mismatch.
    if "incompatible types" in lower or "cannot be converted" in lower or "no suitable method" in lower:
        symbol = None
        for line in detail:
            m = re.search(r"required:\s*([\w\.\$<>, ]+)", line)
            if m:
                symbol = m.group(1).strip()
                break
        return CATEGORY_WRONG_TYPE, symbol

    # Basic syntax errors.
    if any(
        token in lower
        for token in (
            "';' expected",
            "<identifier> expected",
            "illegal start of",
            "not a statement",
            "reached end of file",
        )
    ):
        return CATEGORY_SYNTAX, None

    return CATEGORY_OTHER, None


def error_delta(
    previous: Iterable[CompileError],
    current: Iterable[CompileError],
) -> Tuple[List[CompileError], List[CompileError]]:
    """Return (new_errors, recurring_errors) when comparing two fix iterations.

    New errors appear in ``current`` but not ``previous`` (by signature).
    Recurring errors appear in both — these feed the hopeless-loop guard (G).
    """
    previous_sigs = {e.signature for e in previous}
    new_errors: List[CompileError] = []
    recurring: List[CompileError] = []
    for err in current:
        if err.signature in previous_sigs:
            recurring.append(err)
        else:
            new_errors.append(err)
    return new_errors, recurring
