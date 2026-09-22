"""The agent lane: generating tests, and everything that drives a model.

The workflow graph and its nodes, prompt templates, the learning store, and
the session machinery -- turn timeouts, stall recovery, and the budget/stop
guard that wraps every model phase.

Sits on top of `enforcement/`, and uses it: generated tests are checked by the
same coverage and mutation gates the deterministic lane runs on its own. The
dependency is one way. `enforcement` never imports anything here, which is
what lets the gates run on a node with no model credentials at all.
"""
