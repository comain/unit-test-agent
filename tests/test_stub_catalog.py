"""Tests for uta.templates.stub_catalog (token_opt_phase2 strategy J)."""

import pytest
from uta.templates.stub_catalog import (
    get_stub_patterns,
    get_stub_patterns_for_deps,
    format_stub_catalog_md,
    seed_from_project_summary,
)


def test_known_type_returns_patterns():
    patterns = get_stub_patterns("JdbcTemplate")
    assert len(patterns) >= 1
    assert any("jdbcTemplate" in p for p in patterns)


def test_unknown_type_returns_empty():
    patterns = get_stub_patterns("UnknownService")
    assert patterns == []


def test_get_stub_patterns_for_deps_filters_known():
    result = get_stub_patterns_for_deps(["JdbcTemplate", "MyCustomRepo", "RestTemplate"])
    assert "JdbcTemplate" in result
    assert "RestTemplate" in result
    assert "MyCustomRepo" not in result


def test_get_stub_patterns_for_deps_accepts_fqn():
    result = get_stub_patterns_for_deps(["org.springframework.web.client.RestTemplate"])
    assert "RestTemplate" in result


def test_format_stub_catalog_md_returns_empty_for_no_known_types():
    md = format_stub_catalog_md(["MyCustomRepo", "AnotherService"])
    assert md == ""


def test_format_stub_catalog_md_includes_type_headers():
    md = format_stub_catalog_md(["JdbcTemplate", "ObjectMapper"])
    assert "JdbcTemplate" in md
    assert "ObjectMapper" in md


def test_format_stub_catalog_md_has_code_fences():
    md = format_stub_catalog_md(["JdbcTemplate"])
    assert "```java" in md


def test_seed_from_project_summary_returns_known_symbols():
    summary = {
        "top_resolved_symbols": [
            {"short_name": "JdbcTemplate", "fqns": ["org.springframework.jdbc.core.JdbcTemplate"], "frequency": 5},
            {"short_name": "MyUnknownService", "fqns": ["com.example.MyUnknownService"], "frequency": 3},
        ]
    }
    known = seed_from_project_summary(summary)
    assert "JdbcTemplate" in known
    assert "MyUnknownService" not in known


def test_seed_from_empty_summary():
    known = seed_from_project_summary({})
    assert known == {}
