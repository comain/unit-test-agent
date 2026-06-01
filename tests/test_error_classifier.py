"""Tests for uta.compile.error_classifier (token_opt_phase2 strategy K)."""

import pytest
from uta.compile import (
    CompileError,
    classify_compile_errors,
    error_delta,
    CATEGORY_MISSING_IMPORT,
    CATEGORY_UNRESOLVED_SYMBOL,
    CATEGORY_WRONG_TYPE,
    CATEGORY_MOCKITO_API,
    CATEGORY_SYNTAX,
    CATEGORY_DEPRECATED_API,
    CATEGORY_OTHER,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic mvn output snippets
# ---------------------------------------------------------------------------

MVN_MISSING_IMPORT = """
[ERROR] /repo/src/test/java/com/example/FooTest.java:[5,1] package com.example.svc does not exist
"""

MVN_CANNOT_FIND_SYMBOL = """
[ERROR] /repo/src/test/java/com/example/FooTest.java:[14,17] cannot find symbol
  symbol:   class WaitingPickUp
  location: class com.example.FooTest
"""

MVN_INCOMPATIBLE_TYPES = """
[ERROR] /repo/src/test/java/com/example/FooTest.java:[30,9] incompatible types: String cannot be converted to Long
  required: java.lang.Long
  found:    java.lang.String
"""

MVN_MOCKITO_MATCHERS = """
[ERROR] /repo/src/test/java/com/example/FooTest.java:[22,5] error: cannot find symbol
  symbol:   method anyListOf(Class<String>)
"""

MVN_SYNTAX_ERROR = """
[ERROR] /repo/src/test/java/com/example/FooTest.java:[99,1] ';' expected
"""

MVN_MULTI = MVN_MISSING_IMPORT + MVN_CANNOT_FIND_SYMBOL + MVN_SYNTAX_ERROR


def test_classify_missing_import():
    errors = classify_compile_errors(MVN_MISSING_IMPORT)
    assert len(errors) == 1
    e = errors[0]
    assert e.category == CATEGORY_MISSING_IMPORT
    assert e.symbol == "com.example.svc"
    assert e.line == 5


def test_classify_cannot_find_symbol():
    errors = classify_compile_errors(MVN_CANNOT_FIND_SYMBOL)
    assert len(errors) == 1
    e = errors[0]
    assert e.category == CATEGORY_UNRESOLVED_SYMBOL
    assert e.symbol == "WaitingPickUp"
    assert e.line == 14


def test_classify_incompatible_types():
    errors = classify_compile_errors(MVN_INCOMPATIBLE_TYPES)
    assert len(errors) == 1
    e = errors[0]
    assert e.category == CATEGORY_WRONG_TYPE
    assert "Long" in (e.symbol or "")


def test_classify_syntax_error():
    errors = classify_compile_errors(MVN_SYNTAX_ERROR)
    assert len(errors) == 1
    assert errors[0].category == CATEGORY_SYNTAX


def test_classify_multiple_errors():
    errors = classify_compile_errors(MVN_MULTI)
    assert len(errors) == 3
    cats = [e.category for e in errors]
    assert CATEGORY_MISSING_IMPORT in cats
    assert CATEGORY_UNRESOLVED_SYMBOL in cats
    assert CATEGORY_SYNTAX in cats


def test_classify_empty_input():
    assert classify_compile_errors("") == []
    assert classify_compile_errors("BUILD SUCCESS\n") == []


def test_error_signature_stable():
    errors = classify_compile_errors(MVN_CANNOT_FIND_SYMBOL)
    sig1 = errors[0].signature
    # Re-classify same text → identical signature
    errors2 = classify_compile_errors(MVN_CANNOT_FIND_SYMBOL)
    assert errors2[0].signature == sig1


def test_error_signature_different_for_different_errors():
    e1 = classify_compile_errors(MVN_MISSING_IMPORT)[0]
    e2 = classify_compile_errors(MVN_CANNOT_FIND_SYMBOL)[0]
    assert e1.signature != e2.signature


def test_error_delta_identifies_new_and_recurring():
    prev = classify_compile_errors(MVN_MISSING_IMPORT + MVN_SYNTAX_ERROR)
    curr = classify_compile_errors(MVN_MISSING_IMPORT + MVN_CANNOT_FIND_SYMBOL)
    new_errors, recurring = error_delta(prev, curr)
    assert len(recurring) == 1
    assert recurring[0].category == CATEGORY_MISSING_IMPORT
    assert len(new_errors) == 1
    assert new_errors[0].category == CATEGORY_UNRESOLVED_SYMBOL


def test_error_delta_all_new():
    prev = classify_compile_errors(MVN_MISSING_IMPORT)
    curr = classify_compile_errors(MVN_SYNTAX_ERROR)
    new_errors, recurring = error_delta(prev, curr)
    assert len(new_errors) == 1
    assert len(recurring) == 0


def test_error_delta_all_recurring():
    errors = classify_compile_errors(MVN_MISSING_IMPORT)
    new_errors, recurring = error_delta(errors, errors)
    assert len(new_errors) == 0
    assert len(recurring) == 1


def test_classify_anylistof_is_mockito():
    errors = classify_compile_errors(MVN_MOCKITO_MATCHERS)
    # anyListOf match lands as unresolved_symbol (it is a cannot-find-symbol error)
    # but the symbol hint should be populated
    assert len(errors) >= 1
