# TODO: Python Mutation Batched Generation

Plan: [`docs/plan-python-mutation-batched-generation.md`](plan-python-mutation-batched-generation.md).
Status legend: [ ] todo · [~] in progress · [x] done.

## Phase 0 — Scaffolding
- [x] T1 Config: `python_mutation_generation_strategy` (initial default `hard_cap`, flipped to `batch`
  after node2 verification), `_max_generated_bytes` (8 MB),
  `_max_batches`; env aliases; defaults unit test. → **CP-A: existing tests green, no behavior change** ✓

## Phase 1 — Partition
- [x] T2 `partition_units_into_batches` + `mutation_units_from_source` (function-granular FFD, one-per-line
  estimate, signature, oversized cap, max-batch trim); unit tests. → **CP-B: deterministic partition** ✓
  (placed in new pure module `uta/language/python/mutation_batching.py` for isolated testability)

## Phase 2 — Batch execution
- [x] T3 `_clean_generated_mutants` (keep stats/coverage); unit test ✓
- [x] T4 Per-batch generate+score loop `_run_batched_modern_mutation` + `_filter_generation_policy_to_lines`
  + `_aggregate_mutation_summaries`; pure-piece unit tests (filter/summary). Batch path validated by T7/T8. ✓

## Phase 3 — Aggregation
- [x] T5 `aggregate_batch_candidate_plans` (sum counts, concat lists, recomputed `candidatePlanId`
  with partition signature); unit tests ✓

## Phase 4 — Wiring / evidence / UX
- [x] T6 Strategy branch in verify_python_target; evidence
  (generationStrategy, batchCount, batches[], omittedBy*, mutationBatchWarnings); warnings via evidence
  + artifacts. Regression: 1283 unit tests pass, hard_cap path unchanged. ✓

## Phase 5 — Verification
- [x] T7 Partition-level parity: line-conservation (lossless, reason-coded omissions only) +
  collision-free disjoint cover (partition + per-batch policy filter). Execution-level verdict parity
  on node2 in T8 (local env can't run real mutmut). → **CP-C (partition parity) ✓; verdict parity → T8**
- [x] T8 node2 real verification (non-invasive scratch, real mutmut 3.5.0): real function-granular
  partition of the Appendix-A target → 4 batches, modules 4.4–9.1 MB, **total parse 17.1s vs >2400s**
  for the single 78 MB module. AC1 (no hang) validated. Added a calibration factor (2.0) because mutmut
  emits ~2 mutants/line so the raw one-per-line estimate under-counts ~2×. ✓ (AC1)
  → **CP-D complete:** node2 verification confirmed full mutmut stats collection is reused across batches
  on a real target; default flipped to `batch`. ✓

## Phase 6 — Docs
- [x] T9 `docs/usage-python-mutation-batched-generation.md` written; design/spec reconciled; todo closed. ✓

## Out of scope (this round)
- Lossless intra-function split; orphan reaping; mutmut validation-parse skip; parallel batches.
