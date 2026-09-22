"""Delivery must terminalize the batch it was given, whatever it commits.

Found on beta: a generation unit finished its checkpointed workflow, its class
rows stayed `CREATED`, and the daemon reselected the same batch on every poll.
Because the unit was complete in its checkpoint, each reinvocation was
`reused_completed` -- it ran no nodes, so nothing could ever write those rows.
Seventy iterations at ten-second intervals against a single-slot daemon, with
no error anywhere.

`commit_to_branch` is the only node that moves class rows to a terminal state,
and it did so on one path: after a successful commit. Its two other exits --
nothing to stage, and a commit-recording failure -- returned first. A unit that
produced no test file (a failed generation is exactly that) took the first exit
and was stranded by construction.

`_guard_reused_unit` in `graph/durable_cycle.py` is the backstop that turns the
loop into one loud failure. This is the cause it was covering for.
"""

from __future__ import annotations

import logging

from uta.testgen.delivery import _sync_batch_results


class RecordingPorts:
    def __init__(self, explode: bool = False):
        self.calls = []
        self._explode = explode

    def sync_results(self, task_id, results, **kwargs):
        self.calls.append((task_id, results, kwargs))
        if self._explode:
            raise RuntimeError("db is gone")


FAILED_RESULT = {"status": "failed", "test_file_path": None}


def test_a_unit_that_produced_no_file_is_still_terminalized():
    """The stranding case. A failed generation writes no test file, and that
    is precisely when the rows most need to reach a terminal state."""
    ports = RecordingPorts()

    _sync_batch_results(ports, 7, {}, ["a.B"], {"a.B": FAILED_RESULT})

    assert len(ports.calls) == 1
    task_id, results, _ = ports.calls[0]
    assert task_id == "7"
    assert results == {"a.B": FAILED_RESULT}


def test_only_the_selected_batch_is_synced():
    """Results accumulate across units; syncing all of them would report
    targets this delivery never touched."""
    ports = RecordingPorts()

    _sync_batch_results(
        ports, 7, {}, ["a.B"], {"a.B": FAILED_RESULT, "c.D": FAILED_RESULT}
    )

    assert ports.calls[0][1] == {"a.B": FAILED_RESULT}


def test_a_key_mismatch_names_both_key_sets(caplog):
    """The other way to strand a unit: results keyed differently than the
    batch was selected with, so no row ever matches. Silent before."""
    ports = RecordingPorts()

    with caplog.at_level(logging.WARNING):
        _sync_batch_results(ports, 7, {}, ["pyfile:jobs/forecast.py"], {"py": FAILED_RESULT})

    assert ports.calls == [], "nothing matched, so nothing should be claimed"
    message = caplog.text
    assert "py" in message and "pyfile:jobs/forecast.py" in message


def test_a_sync_failure_is_reported_at_warning(caplog):
    """It used to be `logger.debug`, which is how this hid."""
    ports = RecordingPorts(explode=True)

    with caplog.at_level(logging.WARNING):
        _sync_batch_results(ports, 7, {}, ["a.B"], {"a.B": FAILED_RESULT})

    assert "class rows stay non-terminal" in caplog.text


def test_nothing_to_report_stays_quiet():
    """An empty batch is not a stranding; it is a no-op."""
    ports = RecordingPorts()

    _sync_batch_results(ports, 7, {}, [], {})

    assert ports.calls == []
