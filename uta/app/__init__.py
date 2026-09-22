"""How the lanes are wired together and reached from outside.

The composition root. `cli.py` and `service.py` are the only two modules in
this project that touch all three layers below them -- shared, enforcement and
testgen -- which is what a composition root is for and why they belong here
rather than in either lane.

Everything under here is delivery: the CLI, the HTTP app and its routes, the
trigger protocols, the record store, and report rendering.

Nothing in the lanes may import this package. The check is in
`tests/test_lane_layering.py`, with the one exception it has not been worth
paying to remove yet, named there with its reason.
"""

from uta.app.persistence import register_task_persistence

register_task_persistence()
