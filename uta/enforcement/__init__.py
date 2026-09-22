"""The deterministic lane: coverage, mutation, and the gates over them.

Runs the quality gates and reaches no model. That is the property worth
protecting, and `tests/test_lane_layering.py` protects it: nothing here may
import the agent workflow, the harness, or the task queue.

It sits above `shared/` and below `testgen/`. The direction is one way and it
is not symmetric -- test generation calls these gates to check what it
produced, which is why `testgen` may import `enforcement` and never the
reverse. Language-specific gate implementations stay in `language/{java,python}`,
reached through the backend table in `shared/backends.py`.
"""

from uta.enforcement import coverage_recompute, enforcement, test_quality

__all__ = ["coverage_recompute", "enforcement", "test_quality"]
