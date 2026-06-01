"""Classify run-time errors into retry policies.

Policies
--------
transient           Exponential backoff, retry up to 3×.
class_fail_continue Fail this class, continue with the next.
internal_retry_once Reset class state and retry exactly once; fail on repeat.
budget_abort        Kill the in-flight agent run and mark BUDGET_EXCEEDED.
"""
from __future__ import annotations

import re
from typing import Literal

RetryPolicy = Literal["transient", "class_fail_continue", "internal_retry_once", "budget_abort"]

# Ordered rules — first match wins.
_RULES: list[tuple[re.Pattern, RetryPolicy]] = [
    # HTTP 429 / 5xx provider errors
    (re.compile(r"429|Too Many Requests|rate.limit", re.IGNORECASE), "transient"),
    (re.compile(r"50[0-9] |Server Error|Connection (reset|refused|timed? out)", re.IGNORECASE), "transient"),
    # Budget
    (re.compile(r"budget.exceeded|hard.cap", re.IGNORECASE), "budget_abort"),
    # Safe class-level failures — fail and continue
    (re.compile(r"Baseline compilation failed", re.IGNORECASE), "class_fail_continue"),
    (re.compile(r"Unsafe LLM.authored (patch|diff)|Pre-existing unsafe dir", re.IGNORECASE), "class_fail_continue"),
    # Internal / intermittent errors — retry once
    (re.compile(r"tree_sitter\.Query|TreeSitter", re.IGNORECASE), "internal_retry_once"),
    (re.compile(r"Expecting value: line 1", re.IGNORECASE), "internal_retry_once"),
    (re.compile(r"\[Errno 2\] No such file", re.IGNORECASE), "internal_retry_once"),
    (re.compile(r"JSON ?decode|JSONDecodeError", re.IGNORECASE), "internal_retry_once"),
]


def classify_run_error(message: str) -> RetryPolicy:
    """Return the retry policy for the given error message string."""
    for pattern, policy in _RULES:
        if pattern.search(message):
            return policy
    return "class_fail_continue"
