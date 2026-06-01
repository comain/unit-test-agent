"""Record per-run inefficiencies for cross-run learning (token_opt_phase2 L1).

After each class run, writes `.uta_cache/learning/<safe_fqn>.jsonl` with one
JSON-line record per observation type. The recorder is purely Python — zero LLM
tokens. Sources:

- compile_fix_loop data passed in by the caller (error signatures, iterations)
- symbol_resolver resolutions that were written back (resolved map if provided)
- phase-level token totals from analyze_session_tokens()

The format is an append-only JSONL file; each record has a ``kind`` field and a
``ts`` (ISO timestamp). All consumers must handle unknown ``kind`` values.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.engine.learning import TargetLearningKey


def _learning_dir(repo_path: str) -> Path:
    d = Path(repo_path) / ".uta_cache" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _append(path: Path, records: List[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record_run_inefficiencies(
    repo_path: str,
    class_fqn: Optional[str] = None,
    *,
    target_key: Optional[TargetLearningKey] = None,
    language: Optional[str] = None,
    target_id: Optional[str] = None,
    legacy_class_fqn: Optional[str] = None,
    compile_fix_iterations: int = 0,
    recurring_error_sigs: Optional[List[str]] = None,
    resolved_symbols: Optional[Dict[str, List[str]]] = None,
    phase_tokens: Optional[Dict[str, Dict[str, int]]] = None,
    over_spec_methods: Optional[List[str]] = None,
    repeated_tools: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Append inefficiency records for one language-neutral target.

    Parameters
    ----------
    compile_fix_iterations:
        Number of compile-fix turns the run actually used.
    recurring_error_sigs:
        Signatures of compile errors that recurred across iterations (hopeless-loop
        candidates in the *previous* run — help pre-warn the next run).
    resolved_symbols:
        Mapping of ``{short_name: [fqn, ...]}`` that Python resolved during the run.
        Used to pre-seed the next run's compile_context.md.
    phase_tokens:
        Token totals per phase, e.g. ``{"compile_fix": {"input": 12000, ...}}``.
    over_spec_methods:
        Methods that were in the plan but dropped at coverage/mutation phase,
        indicating the plan was too broad.
    repeated_tools:
        Transcript-derived repeated tool fingerprints, e.g.
        ``{"tool": "grep", "title": "Find State enum", "count": 5}``.
    """
    key = target_key or TargetLearningKey.from_parts(
        language=language,
        target_id=target_id or class_fqn,
        legacy_class_fqn=legacy_class_fqn or (class_fqn if not language or language == "java" else None),
    )
    identity = key.record_identity()
    path = _learning_dir(repo_path) / key.filename()
    records: List[Dict[str, Any]] = []

    if compile_fix_iterations:
        records.append({
            "kind": "compile_fix_iterations",
            **identity,
            "iterations": compile_fix_iterations,
            "ts": _ts(),
        })

    for sig in (recurring_error_sigs or []):
        records.append({
            "kind": "recurring_error",
            **identity,
            "signature": sig,
            "ts": _ts(),
        })

    for short_name, fqns in (resolved_symbols or {}).items():
        if fqns:
            records.append({
                "kind": "resolved_symbol",
                **identity,
                "short_name": short_name,
                "fqns": fqns,
                "ts": _ts(),
            })

    for phase, tokens in (phase_tokens or {}).items():
        if tokens:
            records.append({
                "kind": "phase_tokens",
                **identity,
                "phase": phase,
                "tokens": tokens,
                "ts": _ts(),
            })

    for method in (over_spec_methods or []):
        records.append({
            "kind": "over_spec_method",
            **identity,
            "method": method,
            "ts": _ts(),
        })

    for item in (repeated_tools or []):
        tool = str(item.get("tool") or "").strip()
        title = str(item.get("title") or "").strip()
        count = int(item.get("count", 0) or 0)
        if tool and count:
            records.append({
                "kind": "repeated_tool",
                **identity,
                "tool": tool,
                "title": title,
                "count": count,
                "ts": _ts(),
            })

    if records:
        _append(path, records)

    return path
