"""Tests for uta.learning.summary — L3 project rollup and L4 regression detection."""

import json
import tempfile
from pathlib import Path

import pytest
from uta.learning.summary import build_project_summary, load_project_summary, check_phase_regression


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path)


def _write_jsonl(repo_path: str, class_fqn: str, records: list) -> Path:
    safe = class_fqn.replace(".", "_").replace("$", "_")
    d = Path(repo_path) / ".uta_cache" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{safe}.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return p


# ---------------------------------------------------------------------------
# L3: build_project_summary
# ---------------------------------------------------------------------------

def test_build_creates_summary_file(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "resolved_symbol", "short_name": "OrderService", "fqns": ["com.example.OrderService"]},
    ])
    summary = build_project_summary(repo)
    summary_path = Path(repo) / ".uta_cache" / "learning" / "project_summary.json"
    assert summary_path.exists()
    loaded = json.loads(summary_path.read_text())
    assert loaded["record_count"] >= 1


def test_top_resolved_symbols_aggregated(repo):
    for fqn in ["com.example.A", "com.example.B", "com.example.C"]:
        _write_jsonl(repo, fqn, [
            {"kind": "resolved_symbol", "short_name": "OrderService",
             "fqns": ["com.example.OrderService"]},
        ])
    _write_jsonl(repo, "com.example.D", [
        {"kind": "resolved_symbol", "short_name": "UserRepo",
         "fqns": ["com.example.UserRepo"]},
    ])
    summary = build_project_summary(repo)
    symbols = {s["short_name"]: s["frequency"] for s in summary["top_resolved_symbols"]}
    assert symbols["OrderService"] == 3
    assert symbols["UserRepo"] == 1


def test_top_recurring_errors_aggregated(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "recurring_error", "signature": "aabbcc"},
        {"kind": "recurring_error", "signature": "aabbcc"},
        {"kind": "recurring_error", "signature": "ddeeff"},
    ])
    summary = build_project_summary(repo)
    errors = {e["signature"]: e["frequency"] for e in summary["top_recurring_errors"]}
    assert errors["aabbcc"] == 2
    assert errors["ddeeff"] == 1


def test_phase_token_medians_computed(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 2000, "total": 2500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1500, "total": 2000}},
    ])
    summary = build_project_summary(repo)
    medians = summary["phase_token_medians"]
    assert "compile_fix" in medians
    assert medians["compile_fix"]["input"] == 1500.0
    assert medians["compile_fix"]["total"] == 2000.0


def test_empty_repo_returns_empty_summary(repo):
    summary = build_project_summary(repo)
    assert summary["record_count"] == 0
    assert summary["top_resolved_symbols"] == []
    assert summary["top_recurring_errors"] == []
    assert summary["phase_token_medians"] == {}


def test_top_symbols_capped_at_20(repo):
    for i in range(25):
        _write_jsonl(repo, f"com.example.Class{i}", [
            {"kind": "resolved_symbol", "short_name": f"Symbol{i}", "fqns": [f"com.example.Symbol{i}"]},
        ])
    summary = build_project_summary(repo)
    assert len(summary["top_resolved_symbols"]) <= 20


def test_load_project_summary_returns_empty_when_missing(repo):
    result = load_project_summary(repo)
    assert result == {}


def test_load_project_summary_reads_built_summary(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "resolved_symbol", "short_name": "Bar", "fqns": ["com.example.Bar"]},
    ])
    build_project_summary(repo)
    loaded = load_project_summary(repo)
    assert "top_resolved_symbols" in loaded
    assert loaded["top_resolved_symbols"][0]["short_name"] == "Bar"


# ---------------------------------------------------------------------------
# L4: check_phase_regression
# ---------------------------------------------------------------------------

def test_no_regression_when_no_summary(repo):
    warnings = check_phase_regression(repo, {"compile_fix": {"input": 99999, "total": 99999}})
    assert warnings == []


def test_no_regression_below_2x_threshold(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
    ])
    build_project_summary(repo)
    # 1900 is < 2×1000 = 2000
    warnings = check_phase_regression(repo, {"compile_fix": {"input": 1900, "total": 1400}})
    assert warnings == []


def test_regression_fires_above_2x_threshold(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1500}},
    ])
    build_project_summary(repo)
    # 2500 > 2×1000 = 2000
    warnings = check_phase_regression(repo, {"compile_fix": {"input": 2500, "total": 1400}})
    assert len(warnings) == 1
    assert "RETROSPECT_REGRESSION" in warnings[0]
    assert "compile_fix" in warnings[0]
    assert "input" in warnings[0]


def test_regression_both_metrics_can_fire(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1000}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1000}},
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1000}},
    ])
    build_project_summary(repo)
    warnings = check_phase_regression(repo, {"compile_fix": {"input": 5000, "total": 5000}})
    assert len(warnings) == 2
    metrics = {w.split("metric=")[1].split()[0] for w in warnings}
    assert "input" in metrics
    assert "total" in metrics


def test_regression_warning_includes_percentage(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "p", "tokens": {"input": 1000, "total": 0}},
        {"kind": "phase_tokens", "phase": "p", "tokens": {"input": 1000, "total": 0}},
        {"kind": "phase_tokens", "phase": "p", "tokens": {"input": 1000, "total": 0}},
    ])
    build_project_summary(repo)
    # 3000 is 200% above median 1000 (3× = +200%)
    warnings = check_phase_regression(repo, {"p": {"input": 3000, "total": 0}})
    assert len(warnings) == 1
    assert "+200%" in warnings[0]


def test_regression_unknown_phase_ignored(repo):
    _write_jsonl(repo, "com.example.Foo", [
        {"kind": "phase_tokens", "phase": "compile_fix", "tokens": {"input": 1000, "total": 1000}},
    ])
    build_project_summary(repo)
    # unknown_phase has no median data, so no regression can fire
    warnings = check_phase_regression(repo, {"unknown_phase": {"input": 99999, "total": 99999}})
    assert warnings == []
