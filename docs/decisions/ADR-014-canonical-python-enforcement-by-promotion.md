# ADR-014: Reach One Python Enforcement Implementation By Promotion, Not Redirection

## Status

Accepted

## Date

2026-08-21

## Context

The specification requires that full UTA, CI, repair, and the lightweight local
client execute one canonical Python enforcement implementation. The initial
design named `uta_py_enforce.api.PythonEnforcementBinding` as that
implementation and described a rollout in which callers are redirected to it.

That description skipped the part that carries the risk. Measured on
2026-08-21:

| Tree | Lines |
| --- | --- |
| `uta/language/python/`, whole tree | 11,213 |
| of which enforcement-relevant (excluding `parse/`, `selection.py`, `scoring.py`, `phases.py`, `adapter.py`, `generation_backend.py`, `project_summary.py`, `test_artifacts.py`, `workspace.py`, `module_resolver.py`, `cycle_inputs.py`, `batch.py`, `generation.py`) | 8,189 |
| `uta/language/python/verification/runner.py` alone | 2,917 |
| `tools/python-enforcement/` (`uta_py_enforce` + `uta_enforce_core`) | 3,405 |

The comparison that matters is 8,189 against 3,405, not 11,213 against 3,405:
the remaining ~3,000 lines are generation and never move. The argument survives
the correction and is worth stating precisely, because this number is the reason
for the decision.

The two are not two views of one algorithm. The UTA runner carries behaviour
the lightweight client has never had, including runtime resolution and
interpreter fallback, the runtime-incompatibility precheck, mutmut version
ownership and import compatibility, pytest import-root construction, changed-line
mutation masking and pragma/side-effect suppression, batched modern-mutmut
generation policy with representative selection and caps, zero-mutant and
no-test-association reconciliation, survivor diff annotation, and process-tree
termination.

"Redirect callers to the canonical binding" would therefore delete behaviour by
omission, and the evidence contract is frozen, so the loss would surface as
changed enforcement verdicts rather than as an error.

One reviewer assumption is also wrong and worth recording, because it would
have shaped the map: the UTA Python path has **no** cancellation today
(`grep -rn cancel uta/language/python` returns nothing). Timeout-driven
`_terminate_process_tree` is all there is. Cancellation is a capability the
neutral invocation context *adds*, not one to preserve.

A second "correction" made in the first disposition pass was itself wrong and is
retracted here: `verification/dependency_requirements.py` was called generation
policy, when its only importer is the enforcement runner and it drives a pip
overlay install whose result is recorded as enforcement evidence and folded into
the persisted `cache_key`. It has its own family in the map. The lesson is
recorded rather than quietly fixed: a claim about which domain a module belongs
to must be checked by reading its importers, not its name.

## Decision

Canonicalization proceeds by **promotion**: each behaviour family moves from
UTA into `uta_py_enforce` and is proven equivalent there, and only then is the
UTA caller redirected. No caller is redirected to a binding that does not yet
implement the behaviour that caller depends on.

The unit of work is a behaviour family, not a file. For each family the design
records its authoritative implementation, its destination, and the frozen
fixture that proves the move. The table lives in
`docs/design-uta-architecture-boundary-cleanup-uta.md` and is a merge gate: a
family may not be marked done without its fixture.

Fixtures are captured from the **current** UTA implementation before any code
moves, stored as golden evidence JSON, and asserted byte-identical after the
move except for fields the specification already allows to differ (timings,
paths, durations). A family whose fixture cannot be captured deterministically
is redesigned to be deterministic before it moves, not waived.

Behaviour that is genuinely UTA-only — CI mutation sampling, workflow progress
projection, task-scoped artifact placement — stays in UTA as policy injected
through the neutral invocation context. It does not move into the distributed
tree, and it does not fork the algorithm.

## Alternatives Considered

### Redirect callers first and fill gaps as they are reported

Rejected. The gap surfaces as a changed pass/fail verdict on someone else's
merge, and the evidence schema is frozen, so nothing raises.

### Canonicalize on the UTA runner and slim the lightweight client

Rejected. It would put UTA-owned orchestration into the sparse-checkout
distribution and re-import the dependency boundary ADR-003 exists to protect.

### Keep both implementations behind one contract

Rejected explicitly by the specification: two orchestrations sharing only
low-level utilities is the state being removed.

## Consequences

1. The Python slice becomes the longest in the rollout, and its length is now
   visible in the plan rather than discovered during it.
2. `uta_py_enforce` grows substantially. Its size is a consequence of owning
   the algorithm, and the size guardrail applies per module inside it.
3. Every promoted family carries a frozen fixture, so a later regression in the
   distributed client is caught by UTA's own suite.
4. The distributed client gains behaviour local developers did not have,
   including the runtime-incompatibility precheck. This is a deliberate
   improvement to the local lane, not a silent change: it is listed in the
   usage document.
