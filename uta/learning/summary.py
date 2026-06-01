"""Project-level rollup and regression detection from per-class learning files (L3+L4).

L3: Aggregate per-class JSONL into .uta_cache/learning/project_summary.json
    - top-20 re-grepped collaborator types (→ stub catalog seed)
    - top compile-error signatures (→ test_generation_guidance additions)
    - per-phase median token costs (→ timeout / planning-trigger tuning)

L4: Regression detection
    - On every run, compare observed phase tokens vs rolling median.
    - If any phase exceeds 2× median, emit RETROSPECT_REGRESSION in the report.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_SUMMARY_FILENAME = "project_summary.json"
_REGRESSION_MULTIPLIER = 2.0


def _learning_dir(repo_path: str) -> Path:
    d = Path(repo_path) / ".uta_cache" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_all_records(repo_path: str, max_files: int = 200) -> List[Dict[str, Any]]:
    d = _learning_dir(repo_path)
    records: List[Dict[str, Any]] = []
    for path in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]:
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(raw.strip())
                records.append(rec)
        except Exception:
            pass
    return records


def build_project_summary(repo_path: str) -> Dict[str, Any]:
    """Aggregate all class-level JSONL records into a project summary dict."""
    records = _load_all_records(repo_path)

    # Resolved symbols: frequency count by short_name → best FQN candidates
    symbol_freq: Counter = Counter()
    symbol_best: Dict[str, List[str]] = {}
    for rec in records:
        if rec.get("kind") == "resolved_symbol":
            name = rec.get("short_name", "")
            fqns = rec.get("fqns") or []
            if name and fqns:
                symbol_freq[name] += 1
                symbol_best[name] = fqns

    top_symbols = [
        {"short_name": name, "fqns": symbol_best[name], "frequency": freq}
        for name, freq in symbol_freq.most_common(20)
    ]

    # Recurring errors: top compile-error signatures by frequency
    err_freq: Counter = Counter()
    for rec in records:
        if rec.get("kind") == "recurring_error":
            sig = rec.get("signature", "")
            if sig:
                err_freq[sig] += 1

    top_errors = [{"signature": sig, "frequency": freq} for sig, freq in err_freq.most_common(20)]

    # Per-phase token medians
    phase_token_lists: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        if rec.get("kind") == "phase_tokens":
            phase = rec.get("phase", "")
            tokens = rec.get("tokens") or {}
            if phase:
                for metric, value in tokens.items():
                    phase_token_lists[phase][metric].append(int(value or 0))

    phase_medians: Dict[str, Dict[str, float]] = {}
    for phase, metrics in phase_token_lists.items():
        phase_medians[phase] = {
            metric: statistics.median(vals) for metric, vals in metrics.items() if vals
        }

    summary = {
        "generated_at": _ts(),
        "record_count": len(records),
        "top_resolved_symbols": top_symbols,
        "top_recurring_errors": top_errors,
        "phase_token_medians": phase_medians,
    }

    out = _learning_dir(repo_path) / _SUMMARY_FILENAME
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def load_project_summary(repo_path: str) -> Dict[str, Any]:
    out = _learning_dir(repo_path) / _SUMMARY_FILENAME
    if not out.exists():
        return {}
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_phase_regression(
    repo_path: str,
    observed_tokens: Dict[str, Dict[str, int]],
) -> List[str]:
    """Return a list of RETROSPECT_REGRESSION warning strings.

    ``observed_tokens`` is ``{phase: {metric: int}}``, matching the format
    emitted by ``analyze_session_tokens`` in the OpenCode client.

    Returns warning strings (empty list = no regression).
    """
    summary = load_project_summary(repo_path)
    medians = summary.get("phase_token_medians", {})
    if not medians:
        return []

    warnings: List[str] = []
    for phase, tokens in observed_tokens.items():
        phase_medians = medians.get(phase, {})
        for metric in ("input", "total"):
            observed = int(tokens.get(metric, 0) or 0)
            median = float(phase_medians.get(metric, 0) or 0)
            if median > 0 and observed > median * _REGRESSION_MULTIPLIER:
                pct = round((observed / median - 1) * 100)
                warnings.append(
                    f"RETROSPECT_REGRESSION: phase={phase} metric={metric} "
                    f"observed={observed:,} median={int(median):,} (+{pct}% above 2× threshold)"
                )
    return warnings
