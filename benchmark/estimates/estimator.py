"""Budget estimator for uta batch runs.

Cold-start formula: $0.99/class (calibrated from prod: $201.46 / 204 classes).
If db has ≥ 5 completed runs, switches to linear regression on (class_count, total_cost).

Returns (preliminary_usd, coarse_cap_usd, sample_order) where
  sample_order = [p0_class, p90_class, p95_class] sorted by LOC ascending.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

COST_PER_CLASS = 0.99
CAP_MULTIPLIER = 1.5  # coarse cap = preliminary * 1.5


def _count_java_classes(repo_path: str) -> list[tuple[str, int]]:
    """Return [(fqn_or_path, loc)] for each .java file under repo_path/src/main."""
    results: list[tuple[str, int]] = []
    root = Path(repo_path)
    for java_file in sorted(root.rglob("*.java")):
        if "src/main" not in str(java_file) and "src\\main" not in str(java_file):
            continue
        try:
            lines = java_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        loc = sum(1 for line in lines if line.strip())
        # Build a rough FQN from the file path
        parts = java_file.parts
        try:
            idx = next(i for i, p in enumerate(parts) if p == "java") + 1
            fqn = ".".join(parts[idx:]).removesuffix(".java")
        except StopIteration:
            fqn = java_file.stem
        results.append((fqn, loc))
    return results


def _regression_estimate(db_path: str) -> Optional[float]:
    """Return cost-per-class from linear regression on completed runs, or None."""
    if not db_path or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT rt.id,
                   COUNT(ct.id) AS class_count,
                   COALESCE(SUM(ct.provider_cost_usd), 0) AS total_cost
            FROM repo_tasks rt
            JOIN class_tasks ct ON ct.repo_task_id = rt.id
            WHERE rt.status = 'COMPLETED'
            GROUP BY rt.id
            HAVING class_count > 0
            """
        ).fetchall()
        conn.close()
    except Exception:
        return None

    if len(rows) < 5:
        return None

    xs = [r["class_count"] for r in rows]
    ys = [r["total_cost"] for r in rows]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    return max(slope, 0.10)  # floor at $0.10/class


def estimate(
    repo_path: str,
    db_path: Optional[str] = None,
) -> tuple[float, float, list[str]]:
    """Estimate cost for repo.

    Returns:
        (preliminary_usd, coarse_cap_usd, sample_order)
        where sample_order is [p0, p90, p95] class FQNs by LOC.
    """
    classes = _count_java_classes(repo_path)
    class_count = len(classes)

    cost_per_class = _regression_estimate(db_path or "") or COST_PER_CLASS
    preliminary_usd = class_count * cost_per_class
    coarse_cap_usd = preliminary_usd * CAP_MULTIPLIER

    # Sample order: p0 (smallest), p90, p95 by LOC
    if not classes:
        sample_order: list[str] = []
    else:
        sorted_classes = sorted(classes, key=lambda t: t[1])
        n = len(sorted_classes)

        def _at_percentile(pct: float) -> str:
            idx = max(0, min(n - 1, round(n * pct / 100)))
            return sorted_classes[idx][0]

        seen: set[str] = set()
        sample_order = []
        for fqn in [_at_percentile(0), _at_percentile(90), _at_percentile(95)]:
            if fqn not in seen:
                sample_order.append(fqn)
                seen.add(fqn)

    return preliminary_usd, coarse_cap_usd, sample_order


if __name__ == "__main__":
    import sys, json

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    db = sys.argv[2] if len(sys.argv) > 2 else None
    prelim, cap, order = estimate(repo, db)
    print(json.dumps({
        "repo": repo,
        "preliminary_usd": round(prelim, 2),
        "coarse_cap_usd": round(cap, 2),
        "sample_order": order,
    }, indent=2))
