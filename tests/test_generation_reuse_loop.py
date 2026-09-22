"""A unit complete in its checkpoint but unfinished in the product.

A batch is only selected while its class rows are non-terminal, and a reused
invocation runs no nodes — so a reuse of a *selected* batch means its terminal
product evidence was never recorded. The daemon then selects it, reuses it, and
never progresses: on beta this span 70+ runs in ten-second intervals with a
single-slot daemon, wedged until someone noticed.
"""

from __future__ import annotations

import pytest

from uta.testgen.batches import GenerationBatchIdentityError, StableGenerationBatch


def _batch() -> StableGenerationBatch:
    return StableGenerationBatch(
        repo_task_id=1,
        workflow_run_id="run-1",
        unit_id="unit-0001-abcdef",
        target_ids=("pyfile:app/thing.py",),
        class_task_ids=(11,),
    )


class _Ports:
    """Enough of the persistence port for the guard's decision."""

    def __init__(self, *, still_unfinished: bool):
        self._still_unfinished = still_unfinished
        self.events: list = []

    def first_non_terminal_batch(self, batches):
        return batches[0] if self._still_unfinished else None

    def add_event(self, task_id, event, payload):
        self.events.append(event)


def test_a_reused_unit_with_unfinished_rows_fails_instead_of_looping():
    from uta.testgen.graph import durable_cycle

    ports = _Ports(still_unfinished=True)
    with pytest.raises(GenerationBatchIdentityError, match="unit-0001-abcdef"):
        durable_cycle._guard_reused_unit(  # type: ignore[attr-defined]
            ports=ports, batch=_batch(), disposition="reused_completed"
        )
    assert ports.events == [], "it recorded progress for a unit that made none"


def test_a_reused_unit_whose_rows_are_finished_is_fine():
    """The ordinary resume: the workflow completed earlier, the product rows
    were written then, and the batch is no longer selected."""
    from uta.testgen.graph import durable_cycle

    ports = _Ports(still_unfinished=False)
    durable_cycle._guard_reused_unit(  # type: ignore[attr-defined]
        ports=ports, batch=_batch(), disposition="reused_completed"
    )


@pytest.mark.parametrize("disposition", ["started", "resumed"])
def test_a_unit_that_actually_ran_is_never_guarded(disposition):
    """Nodes ran, so the rows are written by the delivery step in this same
    pipeline — after this point. Checking here would fail every healthy run."""
    from uta.testgen.graph import durable_cycle

    ports = _Ports(still_unfinished=True)
    durable_cycle._guard_reused_unit(  # type: ignore[attr-defined]
        ports=ports, batch=_batch(), disposition=disposition
    )
