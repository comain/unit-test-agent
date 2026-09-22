# Plan: Python Mutation Batched Generation

Source: [`docs/design-python-mutation-batched-generation.md`](design-python-mutation-batched-generation.md),
[ADR-002](decisions/ADR-002-python-mutation-batched-generation.md),
[`docs/spec-python-mutation-batched-generation.md`](spec-python-mutation-batched-generation.md).
Tracking: non-Jira (uta personal). Task list: [`docs/todo-python-mutation-batched-generation.md`](todo-python-mutation-batched-generation.md).

## Dependency Graph

```
T1 config (flag initially hard_cap; later verified default batch) ── no deps, behavior-neutral
        │
T2 _partition_opportunities_into_batches ── deps T1 (settings), existing opportunity model
        │
T3 _clean_generated_mutants (keep stats) ── deps none (parallel with T2)
        │
T4 per-batch generate+score loop         ── deps T2, T3, existing policy writer + metadata cmd
        │
T5 aggregate per-batch plans + plan-id   ── deps T4, existing candidate_plan_counts/make_candidate_plan_id
        │
T6 strategy branch + evidence + warnings ── deps T2,T4,T5
        │
T7 parity + collision integration tests  ── deps T6
        │
T8 node2 real verification + I3 check     ── deps T6 (gates default-flip, separate change)
        │
T9 usage doc + todo close                ── deps T6 (text after behavior lands)
```

Vertical slices: each task is one complete path (config→partition→one batch end-to-end→aggregate→wire→
verify), not horizontal layers. `hard_cap` stays the default throughout — no production behavior change
until a separate, later flip.

## Tasks

### T1 — Config settings (behavior-neutral)
- Add to `uta/config.py`: `python_mutation_generation_strategy: str = "hard_cap"`,
  `python_mutation_generation_max_generated_bytes: int = 8_000_000`,
  `python_mutation_generation_max_batches: int = <sized vs timeout>` (+ env aliases).
- **AC:** defaults present; `hard_cap` path unchanged. **Verify:** existing Python mutation tests green;
  unit test asserts defaults + env override parsing.

### T2 — Deterministic partition
- Implement `_partition_opportunities_into_batches(selected_opps, *, budget, max_batches)` in `runner.py`:
  group by `symbol` (via `_symbol_by_line` ranges), `est_bytes(func) = (selected_lines_in_func + 1) ×
  function_source_bytes`, First-Fit-Decreasing (sort `(est_bytes desc, symbol asc)`), emit `BatchPlan`s
  with a stable partition signature.
- Oversized single function (`est_bytes > budget`): own batch, cap its lines to fit →
  `omittedByGeneratedBytesCap`. Partition > `max_batches`: trim lowest-priority functions →
  `omittedByMaxBatches`. Both deterministic.
- **AC:** deterministic for fixed input; every batch ≤ budget (except a capped oversized function, flagged);
  ≤ `max_batches`; reason codes populated. **Verify:** unit tests for determinism, budget, oversized cap,
  max-batch trim, signature stability.

### T3 — Between-batch cleanup preserving stats
- Add `_clean_generated_mutants(repo)` (remove only generated `mutants/`, keep `.coverage`/stats), used
  between batches; keep `_cleanup_mutation_state` for the final teardown.
- **AC:** generated module removed, stats/coverage retained. **Verify:** unit test on a temp tree; the I3
  reuse assumption is confirmed empirically in T8 (not assumed here).

### T4 — Per-batch generate + score loop
- Under `strategy == "batch"`, loop batches sequentially in the modern flow: `_clean_generated_mutants` →
  write batch-scoped generation policy (`allowedLines` = batch lines, via existing
  `_write_mutmut_generation_policy`) → run `_mutmut_generate_metadata_command` → build per-batch plan with
  `build_mutmut3_candidate_plan_from_meta` (read `.meta` before next batch overwrites). Record
  `batchGeneratedBytes`/`overBudget` per batch.
- **AC:** on a small target, produces N batches each with a per-batch plan; per-batch module ≤ budget;
  any batch failure/timeout → fail closed with batch index. **Verify:** integration test on a small repo.

### T5 — Aggregation
- Merge per-batch plans into one `MutationCandidatePlan`: concat candidates/opportunities, sum counts
  (`candidate_plan_counts`), union survivors; `make_candidate_plan_id` fingerprint += `strategy`,
  `batchCount`, `maxGeneratedBytes`, partition signature; combine per-batch generation-policy fingerprints.
- **AC:** aggregate counts = sums, survivors = union; `candidatePlanId` deterministic + stable across
  reruns. **Verify:** unit test merging synthetic per-batch plans; plan-id stability test.

### T6 — Strategy wiring, evidence, warnings
- Branch `verify_python_target`/modern flow on the flag (`hard_cap` = current path untouched). Add evidence
  fields `generationStrategy`, `batchCount`, `batches[]`, `omittedByGeneratedBytesCap`,
  `omittedByMaxBatches`; emit user-facing warnings on oversized/trimmed functions; log
  `batch mode: K batches, max batch bytes, total wall`.
- **AC:** `hard_cap` byte-for-byte unchanged; `batch` emits evidence + warnings. **Verify:** unit/integration
  asserting hard_cap evidence unchanged and batch evidence shape.

### T7 — Parity + collision tests (AC2/I4, §13.9)
- Integration: `batch` vs `hard_cap` on a small target → **equal set of `opportunity_id`s, equal
  per-`opportunity_id` exit code, equal verdict** (NOT plan-id/raw keys). Collision: each function in
  exactly one batch; no conflated raw tool-key report fields.
- **AC:** parity holds; no collisions. **Verify:** the integration tests above.

### T8 — Real verification on node2 + I3 confirmation (checkpoint-gated)
- Run the Appendix-A repro target with `batch` enabled on node2: completes within timeout; record per-batch
  sizes + total wall (compare to §13.7/K-split data). **Confirm I3:** measure whether preserving
  `.coverage`/stats across batches avoids K× stats collection; if not, revise T3 / perf budget.
- **AC:** target completes within timeout; per-batch modules ≤ budget; I3 assumption resolved (confirmed or
  perf budget updated). **Verify:** node2 run evidence.

### T9 — Documentation
- Write `docs/usage-python-mutation-batched-generation.md` (flag, settings, behavior, rollout default
  `hard_cap`, ops, how to enable `batch`). Update `docs/todo-...md`. Confirm design/spec still match as-built;
  amend if drift.
- **AC:** usage doc complete; todo closed; docs match code. **Verify:** doc review.

## Checkpoints

- **CP-A (after T1):** flag in, `hard_cap` default, all existing tests green — zero behavior change.
- **CP-B (after T2):** partition unit-tested and deterministic.
- **CP-C (after T5+T7):** `batch` produces an aggregated plan at parity with `hard_cap` on a small target —
  gate before the real run.
- **CP-D (after T8):** node2 repro completes within timeout and the I3 stats-reuse assumption is resolved —
  gate before proposing any default flip (a separate, approved change, out of this round).

## Requirement-Coverage Matrix

| Spec AC / design decision | Task(s) |
| --- | --- |
| AC1 hang fixed within timeout | T2 (budget), T4 (loop), T8 (proof) |
| AC2 lossless parity (opportunity_id set + exit code + verdict; I4) | T5 (aggregate), T7 (parity test) |
| AC3 honest aggregation (sum/union) | T5 |
| AC4 determinism + fingerprint | T2 (signature), T5 (plan-id) |
| AC5 no collisions; sequential + cleanup; key on opportunity_id | T3, T4, T7 |
| AC6 batching replaces full-profile hardcap; CI sampling untouched | T6 (branch; hard_cap/CI paths unchanged) |
| AC7 over-budget fn + max-batch → reason-coded + warning | T2 (fallbacks), T6 (warnings/evidence) |
| AC8 gated rollout, verified default batch with hard_cap rollback | T1 (flag), T6 (branch), CP-D |
| Design §4 one-op-per-line estimate (I1) | T2 |
| Design §8 max_batches total-time guard (I2) | T1, T2, T6 |
| Design §8/§10 stats-cache reuse verified (I3) | T3, T8 |
| Design §6 partition signature in plan-id | T2, T5 |
| ADR-002 function-granular partition; deferred intra-function split | T2 (whole-function + fallback) |

No orphan tasks: every task maps to ≥1 AC/decision above.

## Out Of This Round (recorded)
- Lossless intra-function split for oversized functions.
- Orphan process-group reaping; skipping mutmut's validation parse; parallel batches.
