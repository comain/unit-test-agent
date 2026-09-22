"""Stable, persisted units of work for the generation cycle.

The cycle checkpoints one unit at a time. A restart must therefore recover the
same membership even when one member already finished; regrouping only the
remaining targets would create a new checkpoint identity and repeat paid work.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple


TERMINAL_CLASS_STATUSES = frozenset({"PASS", "FAIL", "MUTATION_FAIL", "SKIPPED"})


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()



_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class GenerationBatchIdentityError(RuntimeError):
    """Persisted batch identity is missing, mixed, or contradictory."""


@dataclass(frozen=True)
class StableGenerationBatch:
    """One checkpoint lineage and its original, ordered target membership."""

    repo_task_id: int
    workflow_run_id: str
    unit_id: str
    target_ids: Tuple[str, ...]
    class_task_ids: Tuple[int, ...]

    @property
    def batch_key(self) -> str:
        return f"{self.workflow_run_id}/{self.unit_id}"


def ensure_stable_generation_batches(
    db: Any,
    *,
    repo_task_id: int,
    ordered_target_ids: Iterable[str],
    batch_size: int,
    workflow_run_id: Optional[str] = None,
) -> List[StableGenerationBatch]:
    """Assign once, then reconstruct exclusively from ``class_tasks.batch_key``.

    The caller must supply the task's full original order. This deliberately
    rejects a filtered restart list: silently accepting it is how a partially
    completed Java batch gets regrouped into a different lineage.
    """
    requested = tuple(str(item) for item in ordered_target_ids)
    if len(set(requested)) != len(requested):
        raise GenerationBatchIdentityError("the ordered target list contains duplicates")
    size = int(batch_size)
    if size < 1:
        raise GenerationBatchIdentityError("batch_size must be at least one")

    with db.transaction() as conn:
        rows = list(
            conn.execute(
                "SELECT * FROM class_tasks WHERE repo_task_id=? "
                "ORDER BY priority ASC, id ASC",
                (int(repo_task_id),),
            )
        )
        stored = tuple(str(row["target_id"] or row["class_fqn"]) for row in rows)
        if stored != requested:
            raise GenerationBatchIdentityError(
                "generation batching requires the exact full ordered target list"
            )
        if not rows:
            return []

        keyed = [bool(row["batch_key"]) for row in rows]
        if any(keyed) and not all(keyed):
            raise GenerationBatchIdentityError(
                "persisted generation batch keys are mixed between present and missing"
            )

        if not any(keyed):
            run_id = str(workflow_run_id or uuid.uuid4().hex)
            _validate_identifier(run_id, label="workflow run")
            for index, start in enumerate(range(0, len(rows), size), start=1):
                members = rows[start : start + size]
                member_ids = [str(row["target_id"] or row["class_fqn"]) for row in members]
                digest = hashlib.sha256("\x00".join(member_ids).encode("utf-8")).hexdigest()[:12]
                unit_id = f"unit-{index:04d}-{digest}"
                key = f"{run_id}/{unit_id}"
                updated_at = now_iso()
                conn.executemany(
                    "UPDATE class_tasks SET batch_key=?, updated_at=? WHERE id=?",
                    [(key, updated_at, int(row["id"])) for row in members],
                )
            rows = list(
                conn.execute(
                    "SELECT * FROM class_tasks WHERE repo_task_id=? "
                    "ORDER BY priority ASC, id ASC",
                    (int(repo_task_id),),
                )
            )

        batches = _reconstruct(repo_task_id=int(repo_task_id), rows=rows)
        if workflow_run_id is not None and batches:
            if batches[0].workflow_run_id != str(workflow_run_id):
                raise GenerationBatchIdentityError(
                    "requested workflow run does not match persisted batch keys"
                )
        return batches


def first_non_terminal_batch(
    db: Any, batches: Sequence[StableGenerationBatch]
) -> Optional[StableGenerationBatch]:
    """Select work by status without changing its original membership."""
    task_ids = {batch.repo_task_id for batch in batches}
    if len(task_ids) > 1:
        raise GenerationBatchIdentityError("stable batches cross product tasks")
    rows = db.list_class_tasks(next(iter(task_ids))) if task_ids else []
    statuses = {
        int(row["id"]): str(row["status"])
        for row in rows
    }
    for batch in batches:
        if any(
            statuses.get(class_task_id) not in TERMINAL_CLASS_STATUSES
            for class_task_id in batch.class_task_ids
        ):
            return batch
    return None


def _reconstruct(*, repo_task_id: int, rows: Sequence[object]) -> List[StableGenerationBatch]:
    run_id: Optional[str] = None
    order: List[str] = []
    members: dict[str, list[object]] = {}
    closed: set[str] = set()
    previous: Optional[str] = None

    for row in rows:
        key = str(row["batch_key"] or "")  # type: ignore[index]
        parts = key.split("/")
        if len(parts) != 2:
            raise GenerationBatchIdentityError(f"malformed generation batch key: {key!r}")
        row_run, unit_id = parts
        try:
            _validate_identifier(row_run, label="workflow run")
            _validate_identifier(unit_id, label="unit")
        except GenerationBatchIdentityError as exc:
            raise GenerationBatchIdentityError(
                f"malformed generation batch key: {key!r}"
            ) from exc
        if run_id is None:
            run_id = row_run
        elif row_run != run_id:
            raise GenerationBatchIdentityError(
                "persisted generation batch keys contain multiple workflow runs"
            )
        if unit_id != previous:
            if unit_id in closed:
                raise GenerationBatchIdentityError(
                    f"persisted generation batch {unit_id!r} is not contiguous"
                )
            if previous is not None:
                closed.add(previous)
            order.append(unit_id)
            members[unit_id] = []
            previous = unit_id
        members[unit_id].append(row)

    assert run_id is not None
    return [
        StableGenerationBatch(
            repo_task_id=repo_task_id,
            workflow_run_id=run_id,
            unit_id=unit_id,
            target_ids=tuple(
                str(row["target_id"] or row["class_fqn"])  # type: ignore[index]
                for row in members[unit_id]
            ),
            class_task_ids=tuple(int(row["id"]) for row in members[unit_id]),  # type: ignore[index]
        )
        for unit_id in order
    ]


def _validate_identifier(value: str, *, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise GenerationBatchIdentityError(f"{label} ID is not safe: {value!r}")


__all__ = [
    "GenerationBatchIdentityError",
    "StableGenerationBatch",
    "ensure_stable_generation_batches",
    "first_non_terminal_batch",
]
