# Design: Python Mutation Batched Generation

Source spec: [`docs/spec-python-mutation-batched-generation.md`](spec-python-mutation-batched-generation.md).
Origin: [`docs/design-python-mutation-scalability.md` Appendix A §13.8](design-python-mutation-scalability.md).
Tracking: **non-Jira** (uta personal project). Scope: **single repo (uta)** — detail design is folded
into this overview (no separate `-<repo>.md`).

## 1. Goals And Non-Goals

**Goals**

- Make Python3 mutation verification complete for large targets that currently hang in
  `ast.parse`/`compile` of the one oversized mutmut schemata module, **without dropping mutants**.
- Make the verified `batch` strategy the Python3 full-profile default, with `hard_cap` retained as the
  rollback strategy.
- Keep generation deterministic and aggregation honest (sum/union); preserve the `MutationCandidatePlan`
  contract and `candidatePlanId` rerun-comparability from ADR-001.

**Non-Goals**

- Lossless intra-function split for a single function whose mutants alone exceed the budget (deferred;
  this round uses a reason-coded cap/skip).
- Orphan process-group reaping on timeout (separate bug).
- Removing mutmut's internal validation `ast.parse` (negligible once batches are small).
- Parallel batch execution. Any change to CI sampling, suppression, one-per-line selection, opportunity
  collection, or the Python2 `mutmut==1.5.0` lane.

## 2. High-Level Design

Today (full cap profile): select opportunities → apply `maxSelected` cap (drops extras, **lossy**) →
write **one** generation policy → **one** metadata generation (builds the whole schemata module →
`ast.parse` hang) → one candidate plan.

With `batch` strategy:

```
select opportunities (unchanged)
  → PARTITION into function-granular, byte-budgeted batches      [new]
  → for each batch (sequential):                                 [new loop]
        clean generated mutants/ (preserve stats/coverage)
        write batch-scoped generation policy (allowedLines = batch lines)
        run metadata generation (small module → parses in ~1-2s)
        build per-batch candidate plan from .meta
  → AGGREGATE per-batch plans into one MutationCandidatePlan     [new]
        counts = sums, survivors = union, plan-id fingerprint += partition
```

The strategy is chosen by a mode flag; `hard_cap` runs exactly the current path.

The fix lives entirely in the **pre-generation policy adapter** — the only lever over generated-module
size (ADR-002). Per the §13.7 curve, holding each module ≤ 8 MB keeps every parse in the ~1–2 s linear
regime; node2 evidence: K=8 (~10 MB/batch) → ~103 s total vs >2400 s unbatched, same 6071 mutants.

## 3. Intra-System Relationships

| Component | Role under `batch` | Change |
| --- | --- | --- |
| `runner.py::verify_python_target` / modern plan flow (≈604–693) | Orchestrates generation+scoring | Branch on strategy flag; loop batches; aggregate |
| `runner.py::_partition_opportunities_into_batches` | Function-granular byte-budget bin-pack | **New** |
| `runner.py::_write_mutmut_generation_policy` | Emits `allowedLines` policy | Accept a per-batch line subset (already line-scoped) |
| `runner.py::_mutmut_generate_metadata_command` | Metadata-generation command | Invoked once per batch |
| `runner.py::_cleanup_mutation_state` (+ new `_clean_generated_mutants`) | Reset between batches | New lighter variant preserves stats/coverage |
| `mutation_candidates.py::collect_python_mutation_opportunities`, `_symbol_by_line`, `_operator_hints_for_line` | Opportunities + function ranges + per-line operator proxy | Reused for grouping and byte estimate |
| `mutation_candidates.py::build_mutmut3_candidate_plan_from_meta` | mutmut keys → opportunities (per batch) | Called per batch (prefixes unique by whole-function partition) |
| `engine/mutation_candidates.py::candidate_plan_counts`, `make_candidate_plan_id` | Aggregation + fingerprint | Reused; fingerprint extended with partition signature |
| `config.py` | Settings | New strategy flag + budget; `max_selected` unchanged for hard_cap |

## 4. Data Structures And Abstractions

**Batch (internal):**

```
BatchPlan:
  index: int
  function_symbols: tuple[str, ...]      # whole functions assigned to this batch
  allowed_lines: tuple[int, ...]         # union of those functions' selected lines
  estimated_bytes: int
```

**Generated-bytes estimate (a-priori):**

Generation is **one operator per selected line** (verified: `_mutmut_generate_metadata_command` filters by
the policy's `allowedLines` + `operatorByLine` + `opportunityIdByLine`, runner.py:3360-3369), so each
selected line contributes exactly one mutant — a full copy of its containing function.

```
est_mutants(func)  = count of selected (one-per-line) opportunities in func
est_bytes(func)    = (est_mutants(func) + 1) × function_source_bytes
function_source_bytes from AST lineno..end_lineno (existing _symbol_by_line ranges)
```

**Calibration (measured in T8).** The policy emits `operatorByLine = {}`, so mutmut generates *all*
operators per allowed line (UTA scores one-per-line, but generation is multi-operator). The raw
`(lines+1)×unit_bytes` estimate therefore under-counts the real module by ~2× on the node2 reference
target. A configurable `python_mutation_generation_bytes_per_line_factor` (default **2.0**, empirical)
scales `unit_bytes` before bin-packing. With it, the reference target's batches measured 4.4–9.1 MB (vs
8.5–15.2 MB uncalibrated) and total parse **17.1s vs >2400s** for the single 78 MB module. The **actual
generated module size is measured per batch** post-generation and recorded as evidence
(`batchGeneratedBytes` / `overBudget`); the 8 MB budget at half the 16 MB linear edge absorbs residual
estimate error.

## 5. Control Flow (batch mode)

1. Build selected opportunities (unchanged).
2. `_partition_opportunities_into_batches`:
   - group selected opportunities by `symbol` (function); module-level `<module>` is one pseudo-group;
   - compute `est_bytes(func)`;
   - **First-Fit-Decreasing**: sort functions by `(est_bytes desc, symbol asc)`; place each into the first
     batch whose running total + est_bytes ≤ budget, else open a new batch — fully deterministic;
   - a function with `est_bytes > budget` alone → its own batch, flagged `oversized`; apply the deferred
     per-function fallback (cap its selected lines to fit budget, recording `omittedByGeneratedBytesCap`)
     **and emit a user-facing warning** that mutation coverage for that function was reduced.
2a. **Max-batch guard (I2).** If the partition would produce more than `python_mutation_generation_max_batches`
    batches, the target cannot complete within the time budget by batching alone. Deterministically trim
    lowest-priority functions (by existing operator/ROI priority) to fit `max_batches`, recording
    `omittedByMaxBatches` and a user-facing warning — never a silent drop. `max_batches` is sized so that
    `max_batches × python_mutation_adapter_generation_timeout_seconds` stays within the overall command
    timeout (see §8).
3. For each batch in order: clean generated `mutants/` (preserve stats/coverage), write batch policy,
   run metadata generation (per-batch timeout `python_mutation_adapter_generation_timeout_seconds`),
   build per-batch candidate plan.
4. Aggregate (§6). Any batch generation failure/timeout → fail closed for the target with a batch-indexed
   reason code (same fail-closed semantics as today).

## 6. Aggregation And Determinism

- **Opportunities/candidates:** concatenated. Whole-function partitioning makes each function's
  `{module}.x_{symbol}__mutmut_*` keys exist in exactly one batch, so raw tool-keys stay globally unique
  and `opportunity_id`s are disjoint (ADR-002, §13.9).
- **Counts:** `candidate_plan_counts` summed across batches (killed/survived/timeout/no_tests/
  suspicious/skipped/total). **Survivors:** union. Verdict derives from the aggregate.
- **`candidatePlanId`:** `make_candidate_plan_id` fingerprint extended with `strategy="batch"`,
  `batchCount`, `maxGeneratedBytes`, and a **partition signature** = the sorted tuple of each batch's
  sorted `allowed_lines`. Identical inputs ⇒ identical partition ⇒ identical plan-id and verdict.
- **Generation-policy fingerprint:** combine per-batch policy fingerprints (ordered).

## 7. Repo-Local API / Schema (config + evidence)

New settings in `uta/config.py` (names final here):

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `python_mutation_generation_strategy` | `UTA_PYTHON_MUTATION_GENERATION_STRATEGY` | `"hard_cap"` | `hard_cap` (current) or `batch` |
| `python_mutation_generation_max_generated_bytes` | `UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES` | `8_000_000` | Per-batch byte budget (batch mode); from §13.7 curve |
| `python_mutation_generation_max_batches` | `UTA_PYTHON_MUTATION_GENERATION_MAX_BATCHES` | (sized vs overall timeout; e.g. `12`) | Max batches per target (I2 guard); excess trimmed by priority with `omittedByMaxBatches` |

`max_selected` (1000) is unchanged and only governs `hard_cap`. No new CLI command. Evidence additions
(structured JSON, no DB migration — consistent with ADR-001): `generationStrategy`, `batchCount`,
`batches: [{index, functionCount, allowedLineCount, estimatedBytes, batchGeneratedBytes, overBudget}]`,
`omittedByGeneratedBytesCap`, and `omittedByMaxBatches`. `hard_cap` evidence is unchanged.

## 8. Capacity, Reliability, Security

**Performance budget (the cost of K batches):**

| Cost per batch | Behavior | Budget note |
| --- | --- | --- |
| Generation (combine) | ∝ batch mutants | Σ over batches ≈ unchanged total (~131 s for the repro file) |
| `ast.parse` validation | ≤ ~2 s at 8 MB | vs >40 min for one module — the whole point |
| Stats/test-collection | Reused across batches | **Mitigation:** between batches clean only generated source files under `mutants/`, preserve `mutants/mutmut-stats.json`, `.coverage`, and `.mutmut-cache` (source is constant across batches), avoiding K× suite runs. Verify mutmut 3 stats-cache validity (§10). |
| Mutant test execution | ∝ batch mutants | Σ unchanged — same mutants, partitioned |

Net (node2 evidence): K=8 wall ~246 s incl. generation, vs unbounded hang. The added multiplier is
bounded and gated; worst case (stats not reusable) is K× stats collection, still finite.

**Total-time bound (I2).** Total work scales with batch count K. The `max_batches` guard caps K so that
`max_batches × python_mutation_adapter_generation_timeout_seconds` (plus per-batch test time) stays within
the overall command timeout and CI runtime SLO — a target that would need more batches is trimmed by
priority (`omittedByMaxBatches`, reason-coded + warning) rather than risking an overall timeout. The
default `max_batches` is chosen from the per-batch timeout and the verification timeout during
implementation; it must satisfy this inequality.

**Stats-cache reuse is a verified assumption, not a given (I3).** The "preserve
`mutants/mutmut-stats.json` and `.coverage`, clean generated mutant source files" mitigation is the
difference between ~1× and ~K× test-suite stats runs. It is validated on a real target before relying on
it (§10/§11); if mutmut 3 does not reuse stats across batches with a
constant source, the perf budget is revised (the correctness is unaffected — only cost).

**Reliability:** sequential batches with `mutants/` cleanup between them prevent the module-import
collision (§13.9 risk 1); fail-closed on any batch failure preserves current gate honesty. Deferred
oversized-function fallback is reason-coded, never a silent drop.

**Security:** no new external surface; same subprocess/mutmut execution. K transient policy files written
under `mutation_dir`; cleaned with mutation state. No secrets, no network.

## 9. Failure-Mode Handling

| Failure | Handling |
| --- | --- |
| A batch's metadata generation fails/times out | Fail closed for the target; reason `mutation_candidate_plan_failed` with batch index; no partial pass |
| Estimate undershoots → a batch module larger than budget | Still bounded by per-batch timeout; record `batchOverBudget`; if it times out, fail closed (rare at 8 MB w/ 2× headroom) |
| Single function `est_bytes > budget` | Own batch; cap its lines to fit, record `omittedByGeneratedBytesCap` + **user-facing warning** (deferred lossy fallback); gate notes reduced coverage |
| Partition needs > `max_batches` (I2) | Trim lowest-priority functions to fit, record `omittedByMaxBatches` + user-facing warning; bounds total wall time within the overall timeout |
| Flag = `hard_cap` | Rollback to the pre-batch drop-at-`maxSelected` behavior |
| Stats cache not reusable across batches | Correctness unaffected; degrades to K× stats collection (perf only); node2 verification confirmed full stats collection is reused |

## 10. Rollout Plan

1. Land behind `python_mutation_generation_strategy` and verify with `hard_cap` as the initial default.
2. Verify `batch` on node2: the Appendix-A repro target completes within timeout; assert lossless verdict
   parity vs `hard_cap` on a small target (AC2); record per-batch sizes/wall time.
3. Confirm stats-cache reuse across batches (perf assumption) on a real target.
4. Flip the default to `batch` after the above verification.
5. Rollback: set `UTA_PYTHON_MUTATION_GENERATION_STRATEGY=hard_cap`.

## 11. Verification Plan

- **Unit:** deterministic FFD partition (stable order, budget respected); `est_bytes` math; aggregation
  sum/union; `candidatePlanId` stability incl. partition signature; oversized-function reason-coded cap.
- **Integration:** `batch` vs `hard_cap` on a small target → **parity defined as equal set of evaluated
  `opportunity_id`s and equal per-`opportunity_id` exit code, and equal verdict** (I4). Parity is NOT on
  `candidatePlanId` or `tool_candidate_key`s — those intentionally differ between strategies (plan-id
  fingerprint includes strategy; tool-keys are per-generation). Also: synthetic large-function target →
  multiple batches each ≤ budget + correct aggregate; collision check (a function appears in exactly one
  batch; no conflated raw tool-key report fields); `max_batches` trim is deterministic and reason-coded.
- **Real (node2):** Appendix-A target with `batch` enabled completes within timeout; per-batch module
  sizes recorded and compared to the §13.7 curve / K-split data.

## 12. Key Tradeoffs (decisions, with rejected alternatives)

1. **Fix at the pre-generation policy adapter** (not patch mutmut, not post-hoc): it is the only lever over
   generated-module size; downstream cannot help once the module exists. → **ADR-002**.
2. **Whole-function partition granularity** (not per-line): avoids mutmut's per-generation `__mutmut_N`
   key collision across batches (§13.9). Cost: a single over-budget function needs the deferred fallback.
   → **ADR-002**.
3. **Byte budget, not count cap** as the size bound: count does not bound bytes (§13.5); byte budget
   targets the actual quadratic driver. Default 8 MB is **evidence-derived** (§13.7), not guessed.
4. **Verified default `batch`, rollback `hard_cap`**: node2 verification cleared the default-on gate while
   keeping a simple rollback value. Rejected: removing `hard_cap` (no fallback).
5. **One-op-per-line estimate + post-hoc measurement** (not a multi-operator proxy): generation is
   verified one-operator-per-selected-line, so `est_mutants = selected lines in function` is accurate; the
   only slack is per-mutant overhead, covered by budget headroom and measured `batchGeneratedBytes`.
   Rejected: a per-file calibration constant (brittle); a multi-operator proxy (not how generation works).
6. **Preserve stats across batches** (clean only generated `mutants/`): avoids K× full stats runs; source
   is constant across batches. Node2 verification confirmed full mutmut stats collection runs once, while
   `mutmut run` still performs per-batch clean/list-all/forced-fail checks.

## 13. ADRs

- [ADR-002: Function-granular byte-budgeted batched mutation generation](decisions/ADR-002-python-mutation-batched-generation.md)
- Builds on [ADR-001: Shared Python Mutation Candidate Plan](decisions/ADR-001-python-mutation-candidate-plan.md).

## 14. Design Review Notes (2026-06-23)

**First-principles check:**
1. Goal (from spec): make Python3 mutation verification complete for large targets that currently hang,
   without dropping mutants.
2. Simplest right solution: yes — reuses the policy adapter, candidate-plan contract, and aggregation
   primitives; adds only partition + per-batch loop + aggregate. Estimator kept minimal (one-per-line +
   measured evidence), not a calibration model.
3. Production-proof signal: per-batch `batchGeneratedBytes` + total wall time in evidence and an explicit
   "batch mode: K batches, max batch bytes, total wall" log; the node2 Appendix-A repro target completing
   within timeout.
4. Worst case + guard: divergent verdict vs `hard_cap` or blowing the CI time budget. Guards: `hard_cap`
   rollback, fail-closed per batch, AC2 parity test, `max_batches` total-time guard,
   reason-coded lossy fallbacks. Mutation testing is read-only w.r.t. product → no data-corruption /
   irreversible worst case; not Critical given default-off.

**Findings and dispositions (human-decided):**

| # | Axis | Finding | Disposition |
| --- | --- | --- | --- |
| Critical | — | None (default-off flag + fail-closed + read-only mutation testing). | — |
| I1 | Perf/correctness | Estimate basis vs actual mutmut output. | **Fixed** — verified generation is one-operator-per-line; estimate uses selected-lines-per-function. |
| I2 | Capacity/risks | No aggregate wall-time bound across K batches. | **Fixed** — added `max_batches` guard sized vs the overall timeout; excess trimmed by priority (`omittedByMaxBatches`). |
| I3 | Perf/reuse | Stats-cache reuse across batches is unverified. | **Fixed** — recorded as a verification-gated assumption (§8/§10/§11); perf budget revised if invalid. |
| I4 | Verification | AC2 parity needs a precise equality definition. | **Fixed** — parity defined on the set of `opportunity_id`s + per-opportunity exit codes + verdict, not on plan-id/raw keys. |
| NTH | Simplicity/UX | User-facing warning on oversized/trimmed functions; partition-signature in evidence; ADR→Accepted on approval. | **Fixed** (warning + evidence); ADR status flips on final approval. |
