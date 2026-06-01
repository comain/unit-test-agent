"""Read prior run inefficiency records and preseed artifacts for the next run (L2).

On the *next* run for a class, call ``preseed_compile_context`` before the
compile-fix loop starts.  It reads the class's learning JSONL, extracts
``resolved_symbol`` and ``recurring_error`` records, and injects them into
``compile_context.md`` so the agent doesn't re-discover them.

It also returns a ``hints`` list that the caller can prepend to the generation
prompt as a "previously wasted N turns on X — do Y instead" line.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from uta.engine.learning import TargetLearningKey


def _learning_dir(repo_path: str) -> Path:
    return Path(repo_path) / ".uta_cache" / "learning"


def load_prior_hints(
    repo_path: str,
    class_fqn: Optional[str] = None,
    *,
    target_key: Optional[TargetLearningKey] = None,
    language: Optional[str] = None,
    target_id: Optional[str] = None,
    max_records: int = 200,
) -> Dict[str, Any]:
    """Parse the learning JSONL for a target and return structured hints.

    Returns a dict with:
    - ``resolved_symbols``: {short_name: [best_fqn, ...]} — pre-known resolutions
    - ``recurring_errors``: list of error signature strings that kept recurring
    - ``repeated_tools``: repeated tool patterns from previous transcripts
    - ``prior_fix_iterations``: median compile-fix iterations from prior runs
    - ``phase_tokens``: {phase: {metric: running_total}} across all recorded runs
    - ``prompt_lines``: list of human-readable warning/hint strings for prompt injection
    """
    key = target_key or TargetLearningKey.from_parts(
        language=language,
        target_id=target_id or class_fqn,
        legacy_class_fqn=class_fqn if not language or language == "java" else None,
    )
    path = _learning_dir(repo_path) / key.filename()
    if not path.exists():
        return {
            "resolved_symbols": {},
            "recurring_errors": [],
            "repeated_tools": [],
            "prior_fix_iterations": 0,
            "phase_tokens": {},
            "prompt_lines": [],
        }

    resolved: Dict[str, List[str]] = {}
    recurring: List[str] = []
    repeated_tools: List[Dict[str, Any]] = []
    fix_iter_counts: List[int] = []
    phase_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    lines_read = 0
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            if lines_read >= max_records:
                break
            lines_read += 1
            try:
                rec = json.loads(raw.strip())
            except (ValueError, json.JSONDecodeError):
                continue

            kind = rec.get("kind", "")
            if kind == "resolved_symbol":
                short = rec.get("short_name", "")
                fqns = rec.get("fqns") or []
                if short and fqns:
                    # Keep the most-frequent resolution (last write wins).
                    resolved[short] = fqns
            elif kind == "recurring_error":
                sig = rec.get("signature", "")
                if sig and sig not in recurring:
                    recurring.append(sig)
            elif kind == "compile_fix_iterations":
                n = rec.get("iterations", 0)
                if n:
                    fix_iter_counts.append(int(n))
            elif kind == "phase_tokens":
                phase = rec.get("phase", "")
                tokens = rec.get("tokens") or {}
                if phase:
                    for k, v in tokens.items():
                        phase_totals[phase][k] += int(v or 0)
            elif kind == "repeated_tool":
                tool = str(rec.get("tool") or "").strip()
                count = int(rec.get("count", 0) or 0)
                if tool and count:
                    repeated_tools.append(
                        {
                            "tool": tool,
                            "title": str(rec.get("title") or "").strip(),
                            "count": count,
                        }
                    )

    median_iters = _median(fix_iter_counts) if fix_iter_counts else 0

    prompt_lines: List[str] = []
    if median_iters >= 2:
        prompt_lines.append(
            f"[PRIOR RUN HINT] This class previously required ~{median_iters} compile-fix iterations. "
            "Consult the pre-resolved symbol candidates in the compile-context file before grepping."
        )
    if recurring:
        prompt_lines.append(
            f"[PRIOR RUN HINT] {len(recurring)} compile error(s) recurred across multiple fix attempts last time. "
            "Prioritize resolving these first rather than fixing unrelated errors."
        )
    if repeated_tools:
        repeated_tools.sort(
            key=lambda item: (
                -int(item.get("count", 0) or 0),
                item.get("tool") or "",
                item.get("title") or "",
            )
        )
        top = repeated_tools[0]
        desc = f"{top['tool']}" + (f" ({top['title']})" if top.get("title") else "")
        prompt_lines.append(
            f"[PRIOR RUN HINT] The same tool lookup repeated {top['count']} times via {desc}. "
            "Read cached context and compile facts first instead of re-running the same discovery steps."
        )

    return {
        "resolved_symbols": resolved,
        "recurring_errors": recurring,
        "repeated_tools": repeated_tools,
        "prior_fix_iterations": median_iters,
        "phase_tokens": dict(phase_totals),
        "prompt_lines": prompt_lines,
    }


def preseed_compile_context(
    repo_path: str,
    class_fqn: Optional[str],
    symbols_abs: Optional[str],
    *,
    target_key: Optional[TargetLearningKey] = None,
    language: Optional[str] = None,
    target_id: Optional[str] = None,
) -> List[str]:
    """Inject prior-run resolved symbols into compile_context.md and the class
    .symbols.md.  Returns a list of hint strings for prompt injection.

    Call this at the start of each run for a class, before any fix loop.
    """
    hints = load_prior_hints(
        repo_path,
        class_fqn,
        target_key=target_key,
        language=language,
        target_id=target_id,
    )
    resolved = hints["resolved_symbols"]
    prompt_lines = hints["prompt_lines"]

    if not resolved:
        return prompt_lines

    from uta.language.java.symbol_resolver import format_candidates_markdown, SymbolCandidate
    # Build pseudo-candidates from saved FQNs (locality unknown — use 2=other).
    pseudo: Dict[str, List[SymbolCandidate]] = {}
    for short_name, fqns in resolved.items():
        pseudo[short_name] = [
            SymbolCandidate(short_name=short_name, fqn=fqn, path="(prior run)", locality=2)
            for fqn in fqns
        ]
    md_block = format_candidates_markdown(pseudo)

    # Write to class .symbols.md
    if symbols_abs:
        sym_path = Path(symbols_abs)
        if sym_path.exists():
            existing = sym_path.read_text(encoding="utf-8")
            header = "## Prior-Run Resolved Candidates"
            if header not in existing:
                sym_path.write_text(
                    existing.rstrip() + f"\n\n{header}\n\n" + md_block,
                    encoding="utf-8",
                )

    # Write to compile_facts.md
    from uta.engine.project_summary_artifacts import merge_compile_fix_facts
    facts = [
        f"Prior-run resolution: `{name}` → " + ", ".join(f"`{c.fqn}`" for c in cands)
        for name, cands in pseudo.items()
        if cands
    ]
    if facts:
        merge_compile_fix_facts(repo_path, facts)

    return prompt_lines


def _median(values: List[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) // 2
