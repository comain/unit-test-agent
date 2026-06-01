"""Tests for uta.language.java.symbol_resolver (token_opt_phase2 strategy K)."""

import os
import textwrap
import tempfile
from pathlib import Path

import pytest
from uta.language.java.symbol_resolver import (
    resolve_symbol,
    resolve_symbols,
    format_candidates_markdown,
    invalidate_cache,
)


@pytest.fixture
def mini_repo(tmp_path):
    """A tiny synthetic Java project with a few source files."""
    # service module
    svc = tmp_path / "svc" / "src" / "main" / "java" / "com" / "example" / "svc"
    svc.mkdir(parents=True)
    (svc / "OrderService.java").write_text(textwrap.dedent("""\
        package com.example.svc;
        public class OrderService {}
    """))
    (svc / "OrderState.java").write_text(textwrap.dedent("""\
        package com.example.svc;
        public enum OrderState { PENDING, DONE }
    """))

    # api module
    api = tmp_path / "api" / "src" / "main" / "java" / "com" / "example" / "api"
    api.mkdir(parents=True)
    (api / "OrderService.java").write_text(textwrap.dedent("""\
        package com.example.api;
        public interface OrderService {}
    """))
    (api / "ProductDto.java").write_text(textwrap.dedent("""\
        package com.example.api;
        public class ProductDto {}
    """))

    # Skip files under target/ (compiled output)
    target = tmp_path / "svc" / "target" / "classes" / "com" / "example"
    target.mkdir(parents=True)
    (target / "OrderService.java").write_text("package com.example; public class OrderService {}")

    return tmp_path


@pytest.fixture(autouse=True)
def clear_cache(mini_repo):
    invalidate_cache()
    yield
    invalidate_cache()


def test_resolve_unique_symbol(mini_repo):
    candidates = resolve_symbol("ProductDto", str(mini_repo))
    assert len(candidates) == 1
    assert candidates[0].fqn == "com.example.api.ProductDto"


def test_resolve_ambiguous_symbol_returns_multiple(mini_repo):
    candidates = resolve_symbol("OrderService", str(mini_repo))
    fqns = [c.fqn for c in candidates]
    assert "com.example.svc.OrderService" in fqns
    assert "com.example.api.OrderService" in fqns
    # target/ directory must be excluded
    assert "com.example.OrderService" not in fqns


def test_locality_same_package_ranked_first(mini_repo):
    candidates = resolve_symbol(
        "OrderService", str(mini_repo), target_package="com.example.svc"
    )
    assert candidates[0].locality == 0
    assert candidates[0].fqn == "com.example.svc.OrderService"


def test_resolve_unknown_symbol(mini_repo):
    assert resolve_symbol("NonExistent", str(mini_repo)) == []


def test_resolve_enum(mini_repo):
    candidates = resolve_symbol("OrderState", str(mini_repo))
    assert len(candidates) == 1
    assert candidates[0].fqn == "com.example.svc.OrderState"


def test_resolve_symbols_bulk(mini_repo):
    result = resolve_symbols(["ProductDto", "OrderState", "Missing"], str(mini_repo))
    assert "ProductDto" in result
    assert result["ProductDto"][0].fqn == "com.example.api.ProductDto"
    assert "OrderState" in result
    assert result["Missing"] == []


def test_format_candidates_markdown(mini_repo):
    candidates = resolve_symbol("ProductDto", str(mini_repo))
    md = format_candidates_markdown({"ProductDto": candidates})
    assert "ProductDto" in md
    assert "com.example.api.ProductDto" in md
    assert "| `ProductDto`" in md


def test_format_candidates_no_resolution(mini_repo):
    md = format_candidates_markdown({"Ghost": []})
    assert "Ghost" in md
    assert "no candidates" in md


def test_import_line_helper(mini_repo):
    candidates = resolve_symbol("ProductDto", str(mini_repo))
    assert candidates[0].import_line == "import com.example.api.ProductDto;"


def test_empty_repo_returns_empty(tmp_path):
    candidates = resolve_symbol("Anything", str(tmp_path))
    assert candidates == []
