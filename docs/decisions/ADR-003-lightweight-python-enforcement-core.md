# ADR-003: Extract Lightweight Python Enforcement Core

## Status

Proposed

## Date

2026-06-25

## Context

UTA Python enforcement is now used by CI reports, repair sessions, full CLI usage, and dev-skills local development gates. The deployed UTA implementation supports strict test selection, changed-line coverage, mutmut candidate planning, operator-level filtering, deterministic hard caps, batch generation, exact-key evidence, and Python 2 legacy behavior.

Local dev-skills usage currently has two unsatisfactory choices:

1. require a full UTA checkout or executable, which is heavy for developers and pulls app-only concerns into local development; or
2. use a simplified standalone script, which is lightweight but semantically wrong because it uses stock file-scoped mutmut and does not support UTA's candidate planner, batch generation, operator filtering, hard caps, or exact-key evidence.

The spec requires one source of truth for Python enforcement semantics across full UTA CLI, UTA CI/API trigger, repair-session verification, and dev-skills local enforcement.

## Decision

Extract lightweight UTA-owned enforcement packages under `tools/python-enforcement/`:

1. `uta_enforce_core` for language-neutral contracts, evidence, diff, target, and mutation candidate-plan data.
2. `uta_py_enforce` for Python-specific enforcement behavior.

The packages will contain reusable enforcement logic: neutral diff/evidence/target/candidate-plan contracts in `uta_enforce_core`, plus Python runtime config, strict test selection, coverage verification, mutation candidate planning, batch mutmut adapter behavior, and Python evidence assembly in `uta_py_enforce`.

Full UTA code will call this core through adapters. `uta python-enforce` remains the compatibility CLI command. `tools/python-enforcement/uta_python_test_enforce.py` becomes the lightweight local entrypoint. dev-skills remains a launcher plus evidence validator and will prefer `UTA_PYTHON_ENFORCE_SCRIPT` pointing at that lightweight entrypoint.

CI mutation sampling remains UTA CI adapter-owned. The lightweight core exposes only a narrow mutation-selection policy injection point and the default full hard-cap policy. The lightweight local distribution does not expose the CI sampling implementation, local CLI flags, or documented environment variables that enable sampling. dev-skills rejects any local evidence that claims sampled mutation.

The current simplified standalone stock-mutmut implementation will be deleted after docs and tests move to `tools/python-enforcement/uta_python_test_enforce.py`; it will not remain as a separate enforcement algorithm.

## Alternatives Considered

### Require Full UTA Checkout For Local Dev

Pros:

- Minimal extraction work.
- Existing `uta python-enforce` already has the correct semantics.

Cons:

- Heavy local setup.
- Requires local users to configure full UTA app code that they do not otherwise need.
- Keeps dev-skills guidance coupled to UTA checkout layout.

Rejected because the immediate user need is lightweight local enforcement without full UTA.

### Keep A One-File Standalone Implementation

Pros:

- Easy to distribute.
- Easy to invoke from dev-skills.

Cons:

- Duplicates enforcement logic.
- Cannot safely track UTA's batch mutmut and operator-filter behavior without becoming a second UTA implementation.
- Creates false local passes on large or operator-sensitive diffs.

Rejected because one Python enforcement algorithm is the core invariant.

### Publish A Python Package Now

Pros:

- Clear versioning and installation story.
- Could be distributed by internal package tooling later.

Cons:

- Adds release/distribution work before the core is proven stable.
- Does not itself solve semantic sharing.
- Slower than sparse-checkout for the immediate dev-skills integration.

Rejected for this iteration. The extracted folder can be packaged later without changing the core API.

## Consequences

1. UTA gains a small distribution root, `tools/python-enforcement/`, that is safe to sparse-checkout.
2. `pyproject.toml` package discovery must include `uta_enforce_core*` and `uta_py_enforce*` from `tools/python-enforcement` so deployed UTA and editable local installs import the same source packages.
3. UTA CLI, CI enforcement, repair verification, and dev-skills local enforcement share one implementation.
4. The lightweight path cannot import UTA daemon/API/report/task DB modules.
5. UTA CI can still sample large mutation candidate sets, but only through its adapter-owned policy injection.
6. Local dev, repair, and full CLI use full hard-capped mutation and cannot enable sampling through the lightweight entrypoint.
7. Direct-copy distribution is allowed only as a fallback when it preserves the lightweight tool version file and evidence reports that version, so dev-skills can reject stale local tools.
8. Tests must compare lightweight and full UTA evidence for the same fixture.
9. dev-skills remains simple: launch configured command, validate evidence, reject stale or sampled local evidence.
10. A future package or zip distribution can be layered on top of the same folder.
