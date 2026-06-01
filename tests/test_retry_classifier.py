"""Tests for uta.tasks.run_error_classifier."""
import pytest
from uta.tasks.run_error_classifier import classify_run_error


@pytest.mark.parametrize("msg,expected", [
    # transient
    ("429 Too Many Requests", "transient"),
    ("rate limit exceeded", "transient"),
    ("500 Server Error from provider", "transient"),
    ("Connection reset by peer", "transient"),
    # budget
    ("budget_exceeded: hard cap reached", "budget_abort"),
    # class_fail_continue
    ("Baseline compilation failed", "class_fail_continue"),
    ("Unsafe LLM-authored patch detected", "class_fail_continue"),
    ("Pre-existing unsafe dir /tmp/x", "class_fail_continue"),
    # internal_retry_once
    ("tree_sitter.Query object is not iterable", "internal_retry_once"),
    ("Expecting value: line 1 column 1", "internal_retry_once"),
    ("[Errno 2] No such file or directory: '/tmp/foo'", "internal_retry_once"),
    ("JSONDecodeError: bad json", "internal_retry_once"),
    # unknown → class_fail_continue
    ("some random unexpected error", "class_fail_continue"),
    ("NullPointerException in parser", "class_fail_continue"),
])
def test_classify_run_error(msg, expected):
    assert classify_run_error(msg) == expected
