"""Tests for uta.learning recorder and replayer (token_opt_phase2 L1+L2)."""

import json
import textwrap
import tempfile
from pathlib import Path

import pytest
from uta.engine.learning import TargetLearningKey
from uta.learning.recorder import record_run_inefficiencies
from uta.learning.replayer import load_prior_hints, preseed_compile_context


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path)


# ---------------------------------------------------------------------------
# L1: recorder
# ---------------------------------------------------------------------------

def test_recorder_creates_jsonl(repo):
    path = record_run_inefficiencies(
        repo,
        "com.example.FooService",
        compile_fix_iterations=3,
    )
    assert path.exists()
    assert path.suffix == ".jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    kinds = [r["kind"] for r in records]
    assert "compile_fix_iterations" in kinds


def test_recorder_writes_resolved_symbols(repo):
    record_run_inefficiencies(
        repo,
        "com.example.FooService",
        resolved_symbols={"WaitingPickUp": ["com.example.enum.WaitingPickUp"]},
    )
    path = Path(repo) / ".uta_cache" / "learning" / "com.example.FooService.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    sym_recs = [r for r in records if r["kind"] == "resolved_symbol"]
    assert len(sym_recs) == 1
    assert sym_recs[0]["short_name"] == "WaitingPickUp"
    assert "com.example.enum.WaitingPickUp" in sym_recs[0]["fqns"]


def test_recorder_writes_phase_tokens(repo):
    record_run_inefficiencies(
        repo,
        "com.example.FooService",
        phase_tokens={"compile_fix": {"input": 8000, "output": 2000, "cache_read": 5000}},
    )
    path = Path(repo) / ".uta_cache" / "learning" / "com.example.FooService.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    tok_recs = [r for r in records if r["kind"] == "phase_tokens"]
    assert tok_recs[0]["phase"] == "compile_fix"
    assert tok_recs[0]["tokens"]["input"] == 8000


def test_recorder_appends_across_runs(repo):
    for _ in range(3):
        record_run_inefficiencies(repo, "com.example.FooService", compile_fix_iterations=2)
    path = Path(repo) / ".uta_cache" / "learning" / "com.example.FooService.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(records) == 3


def test_recorder_writes_repeated_tools(repo):
    record_run_inefficiencies(
        repo,
        "com.example.FooService",
        repeated_tools=[{"tool": "grep", "title": "Find State enum", "count": 5}],
    )
    path = Path(repo) / ".uta_cache" / "learning" / "com.example.FooService.jsonl"
    records = [json.loads(l) for l in path.read_text().splitlines()]
    repeated = [r for r in records if r["kind"] == "repeated_tool"]
    assert len(repeated) == 1
    assert repeated[0]["class_fqn"] == "com.example.FooService"
    assert repeated[0]["tool"] == "grep"
    assert repeated[0]["title"] == "Find State enum"
    assert repeated[0]["count"] == 5


def test_recorder_supports_python_target_learning_key(repo):
    key = TargetLearningKey(language="python", target_id="pysymbol:jobs/forecast.py::run")

    path = record_run_inefficiencies(
        repo,
        target_key=key,
        phase_tokens={"generate": {"input": 10}},
        repeated_tools=[{"tool": "rg", "title": "Find function", "count": 2}],
    )

    assert path.name == "pysymbol_jobs_forecast.py__run.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert {record["language"] for record in records} == {"python"}
    assert {record["target_id"] for record in records} == {"pysymbol:jobs/forecast.py::run"}
    hints = load_prior_hints(repo, target_key=key)
    assert hints["repeated_tools"][0]["tool"] == "rg"


# ---------------------------------------------------------------------------
# L2: replayer
# ---------------------------------------------------------------------------

def test_load_prior_hints_empty_when_no_file(repo):
    hints = load_prior_hints(repo, "com.example.Unknown")
    assert hints["resolved_symbols"] == {}
    assert hints["prior_fix_iterations"] == 0
    assert hints["prompt_lines"] == []


def test_load_prior_hints_returns_symbols(repo):
    record_run_inefficiencies(
        repo,
        "com.example.Bar",
        resolved_symbols={"Foo": ["com.example.dto.Foo"]},
        compile_fix_iterations=2,
    )
    hints = load_prior_hints(repo, "com.example.Bar")
    assert "Foo" in hints["resolved_symbols"]
    assert hints["resolved_symbols"]["Foo"] == ["com.example.dto.Foo"]


def test_load_prior_hints_median_iters(repo):
    for iters in [1, 3, 5]:
        record_run_inefficiencies(repo, "com.example.Baz", compile_fix_iterations=iters)
    hints = load_prior_hints(repo, "com.example.Baz")
    assert hints["prior_fix_iterations"] == 3  # median of [1, 3, 5]


def test_load_prior_hints_adds_prompt_line_for_high_iterations(repo):
    for _ in range(3):
        record_run_inefficiencies(repo, "com.example.Heavy", compile_fix_iterations=4)
    hints = load_prior_hints(repo, "com.example.Heavy")
    assert any("compile-fix" in line for line in hints["prompt_lines"])


def test_load_prior_hints_adds_prompt_line_for_repeated_tools(repo):
    record_run_inefficiencies(
        repo,
        "com.example.Tooly",
        repeated_tools=[{"tool": "grep", "title": "Find State enum", "count": 4}],
    )
    hints = load_prior_hints(repo, "com.example.Tooly")
    assert hints["repeated_tools"][0]["tool"] == "grep"
    assert any("same tool lookup repeated" in line.lower() for line in hints["prompt_lines"])


def test_preseed_compile_context_writes_to_symbols_md(repo, tmp_path):
    # Set up a dummy .symbols.md
    sym_file = tmp_path / "FooService.symbols.md"
    sym_file.write_text("## Imported Symbols\n- `java.util.List`\n")

    record_run_inefficiencies(
        repo,
        "com.example.FooService",
        resolved_symbols={"OrderState": ["com.example.domain.OrderState"]},
    )
    preseed_compile_context(repo, "com.example.FooService", str(sym_file))

    content = sym_file.read_text()
    assert "Prior-Run Resolved Candidates" in content
    assert "OrderState" in content
    assert "com.example.domain.OrderState" in content


def test_preseed_compile_context_no_file_no_error(repo):
    record_run_inefficiencies(
        repo,
        "com.example.X",
        resolved_symbols={"Foo": ["com.example.Foo"]},
    )
    # symbols_abs points to non-existent file — should not raise
    preseed_compile_context(repo, "com.example.X", "/nonexistent/path/X.symbols.md")


def test_preseed_compile_context_idempotent(repo, tmp_path):
    sym_file = tmp_path / "Svc.symbols.md"
    sym_file.write_text("## Imported Symbols\n")
    record_run_inefficiencies(
        repo, "com.example.Svc",
        resolved_symbols={"Bar": ["com.example.Bar"]},
    )
    preseed_compile_context(repo, "com.example.Svc", str(sym_file))
    preseed_compile_context(repo, "com.example.Svc", str(sym_file))
    content = sym_file.read_text()
    # The header should appear only once
    assert content.count("Prior-Run Resolved Candidates") == 1
