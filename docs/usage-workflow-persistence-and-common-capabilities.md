# UTA Workflow Persistence And Shared Capabilities

UTA 0.7 uses agent-core as a matched runtime for checkpoint persistence, typed
agent turns, secure artifacts and prompt bundles, neutral session diagnostics,
and scoped publication. UTA retains product truth: stable batch identity,
workspace-aware operation reconciliation, task accounting/caps, prompt policy,
delivery policy, and retention selection.

## Persistence model

LangGraph's native SQLite saver records graph position and state. It does not
prove that repository edits or product evidence survived. UTA's operation
ledger remains the authoritative side-effect record:

1. start the operation row;
2. run the guarded agent or deterministic phase;
3. write and verify the secure operation artifact;
4. complete the operation row;
5. return from the node, allowing LangGraph to checkpoint.

On restart, a pending checkpoint is invoked with no fresh input. A completed
checkpoint is reused without entering an expensive node. UTA still revalidates
the terminal operation chain before product delivery. A corrupt checkpoint is
an error, never absence.

Retention is deliberately checkpoint-first: progress retention runs, the exact
LangGraph lineage is deleted, operation artifacts are deleted, product operation
rows are deleted, then prompt artifacts are deleted. Failure at any step stops
the lineage cleanup before later evidence is removed.

## Filesystem policy

Checkpoint, result, prompt, and standalone roots must be owner-only, symlink-free,
and outside the target repository. Existing shared roots are refused rather than
silently chmodded. Each prompt operation is a manifest-last bundle containing
`prompt.md`, `inputs.json`, and `manifest.json`; only a verified manifest marks
the bundle complete.

## Sessions and diagnostics

Durable workflow evidence stores ordered neutral references:

```json
{"harness":"configured-name","locator":"opaque-id","scope":"durable"}
```

All fallback candidate references survive checkpoint and operation-artifact
round trips. Existing `session_id` and `session_ids` response fields remain
locator-only compatibility projections and must never be parsed to choose a
harness.

`uta assess` resolves the configured optional diagnostics provider. Available,
unsupported, unavailable, mixed, limit-exceeded, and truncated outcomes remain
distinct. Raw provider rows, prompts, reasoning, commands, tool input/output,
and private summaries are not public output.

## Operator checks

Before promotion, run:

```bash
.venv312/bin/python scripts/check_package_dependencies.py --check
.venv312/bin/ruff check .
.venv312/bin/python -m pytest -q
```

For a managed canary, verify that a normal run records completed operation rows,
an interrupted run resumes the same workflow/unit/thread identity, and a second
invocation reuses the terminal checkpoint without another model turn. For a
taskless canary, verify the public result contains no synthetic task/database or
workflow paths and the leased standalone root is removed after close.

Rollback is a matched-pair operation: restore UTA and agent-core 0.6.11 together.
Do not mix UTA's 0.7 consumer code with a 0.6 runtime or restore only one side of
the pair.
