"""What both lanes stand on.

UTA is two things sharing a codebase. Enforcement runs coverage and mutation
gates and reaches no model. Test generation drives an agent, and uses
enforcement to check what it produced. The dependency runs one way::

    testgen  ->  enforcement  ->  shared

This package is the bottom of that: settings, the harness configuration,
language detection and the backend table, target vocabulary, parsing, and the
rules for what a workspace is allowed to look like. Nothing here knows what a
coverage gate is or what an agent turn is.

Two properties are worth keeping, and `tests/test_lane_layering.py` enforces
both: nothing in this package imports either lane, and nothing in the
enforcement lane imports the agent lane. The second is why `workspace_policy`
lives here while the budget guard that needs the task queue does not -- one
shared module was pulling 3,900 lines of queue machinery into a code path that
never schedules anything.
"""
