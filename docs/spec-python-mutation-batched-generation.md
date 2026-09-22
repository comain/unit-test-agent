# Spec: Python Mutation Batched Generation

## 0. Tracking And Provenance

- **Tracking mode: Non-Jira (uta personal project).** Per project convention, uta is a personal tool;
  Jira / release-approval and UTA `doc/` paths are intentionally skipped. Artifacts use stable
  `docs/<topic>.md` paths.
- **Origin:** implements the chosen mitigation in
  [`docs/design-python-mutation-scalability.md` Appendix A §13.8](design-python-mutation-scalability.md)
  (GitLab:
  `https://git.example.com/example-org/unittest-generaor/blob/main/docs/design-python-mutation-scalability.md#13-appendix-a-mutmut-generated-module-parse-bottleneck-investigation-2026-06-22`).
- **Companion docs:** `docs/design-python-mutation-batched-generation.md` (Phase 2),
  `docs/usage-python-mutation-batched-generation.md` (if user-facing behavior changes).
- **Branch policy:** commit to `main` (no feature branches), per repo `AGENTS.md`.

## 1. Objective

Make Python3 mutation verification complete for large targets that currently hang, without weakening
the gate.

Root cause (Appendix A, profiled and reproduced): mutmut 3's schemata model packs all mutants of a
source file into one module; CPython's tokenizer is **super-linear (~O(n²·⁴)) in the size of a single
`ast.parse`/`compile` call**. The real target (117 KB / 3100-line source → 6071 mutants → ~82 MB module)
takes >40 min to parse and times out, orphaning a worker at 100% CPU. An interpreter upgrade does not fix
it (3.12/3.13/3.14 all time out). The cost is memory-cheap and purely single-core CPU.

**Solution:** at the pre-generation policy adapter, partition selected mutation opportunities into
function-granular, byte-budgeted **batches**, generate + score each batch sequentially (each module stays
in the tokenizer's linear regime), and aggregate results into one verdict. Empirically lossless and
fast: same 6071 mutants, total parse drops from >2400 s (K=1) to ~103 s (K=8).

**Target users:** uta CI/report enforcement and repair-session verification for Python3 repos.

## 2. Scope (This Round)

In scope (confirmed):

1. **Function-granular batched generation (core).** Partition selected opportunities into batches under a
   estimated-generated-bytes budget; one generation-policy + metadata-generation + scoring pass per batch,
   run sequentially with state cleanup between batches; deterministic aggregation.
2. **Batch processing as the verified default for the full-profile path.** A **mode flag** chooses `batch`
   or rollback value `hard-cap` (the old drop-at-`maxSelected` behavior). In `batch` mode the full-profile
   drop-and-cap logic is replaced by byte-budget batching: all selected mutants are generated across
   batches, none dropped, and the byte budget is the only per-module-size bound. (Any residual
   `maxSelected` in batch mode would be an extreme safety bound only — a design decision.)

**Explicitly untouched (minimal-change principle):**

- **CI cap-profile sampling logic stays as-is.** The CI representative sampling (`ci_max_selected`, etc.)
  is **not** modified. CI may sample as it does today; the sampled set is then generated through the same
  batched path (so CI samples against either the capped set or the batched set — its choice is unchanged).
  Only the **full-profile** drop-at-cap behavior is replaced.
- Opportunity collection, suppression, one-per-line selection, and the candidate-plan contract are
  unchanged except for adding partition/aggregation around them.

Out of scope (this round; recorded for later):

- **Intra-function split + tool-key namespacing** for a single function whose mutants alone exceed the
  budget. This round **defers** that lossless handling: such a function falls back to a **reason-coded
  cap/skip** (lossy, surfaced in evidence). Lossless intra-function split is a follow-up.
- **Orphan process-group reaping on timeout** (the compounding leak). Separate bug; not required for the
  hang fix. (Note: `_terminate_process_tree` already uses `os.killpg`; the multiprocessing worker leak is
  a distinct gap to be scoped separately.)
- **Skipping mutmut's redundant validation `ast.parse`.** Becomes negligible once batches are small;
  optional micro-optimization, not pursued now.
- **Parallel batch execution.** Explicitly excluded (collision risk — see §6).

## 3. Scope-Discovery Record

Production-traffic table is **N/A**: uta is an internal CLI tool with no production HTTP endpoints; this
is a behavior change inside the Python mutation verifier, not an endpoint change. Candidate modules from
codebase exploration:

| Module / symbol | Role | Decision | Reason |
| --- | --- | --- | --- |
| `uta/language/python/verification/runner.py::_write_mutmut_generation_policy` | Emits `allowedLines`/`selectedLines` driving what mutmut materializes | **In scope** | The single lever over generated-module size; where the hang originates. Becomes per-batch. |
| `runner.py::_generation_policy_caps` / `_cap_generation_policy_selected` / `_representative_generation_policy_selected` | Apply count/source caps | **In scope** | Add byte-budget partitioning; demote `maxSelected` to safety cap. |
| `runner.py::_modern_mutmut3_candidate_plan` + modern flow (≈ lines 604–693) | Generates metadata once, builds candidate plan | **In scope** | Becomes a per-batch loop + aggregation. |
| `runner.py::_mutmut_generate_metadata_command` | Builds the metadata-generation command | **In scope** | Invoked once per batch with that batch's policy. |
| `runner.py::_clean_generated_mutants` | Removes generated source files under `mutants/` while preserving `mutants/mutmut-stats.json`, `.mutmut-cache`, and `.coverage` | **In scope** | Called between sequential batches. |
| `uta/language/python/mutation_candidates.py::collect_python_mutation_opportunities`, `_symbol_by_line`, `select_one_opportunity_per_line` | Builds opportunities; maps line→function symbol | **In scope** | `symbol` + line/end_lineno give whole-function grouping and function size for byte estimate. |
| `mutation_candidates.py::build_mutmut3_candidate_plan_from_meta`, `_mutmut_key_prefixes` | Maps mutmut `__mutmut_N` keys → opportunities by `{module}.x_{symbol}__mutmut_` prefix | **In scope** | Per-batch mapping; whole-function batching keeps prefixes unique per batch. |
| `uta/engine/mutation_candidates.py::make_opportunity_id`, `make_candidate_id`, `make_candidate_plan_id`, `candidate_plan_counts` | Global identity + plan aggregation primitives | **In scope (reuse)** | `opportunity_id` is generation-independent → safe aggregation key; plan-id fingerprint extended with batch scheme. |
| `uta/config.py` (`python_mutation_generation_*`) | Cap/timeout settings | **In scope** | Add generated-bytes budget + batching/flag settings; keep `max_selected` as safety bound. |
| `uta/language/python/batch.py`, `ci.py`, `enforcement.py` | Callers of verification (repair / CI / gate) | **In scope (verify only)** | Must continue to receive one aggregated verdict; confirm no caller assumes single-generation internals. |
| `runner.py::_terminate_process_tree` (`os.killpg`) | Process-group termination on timeout | **Out of scope** | Orphan-worker leak is a separate bug; not needed for the hang fix. |
| Python2 legacy lane (`mutmut==1.5.0`) | Legacy mutation path | **Out of scope** | Pre-mutmut-3; no schemata/parse bottleneck; unchanged. |

## 4. Commands / Interfaces

No new top-level CLI command. The change is internal to Python3 mutation verification, reached via the
existing entrypoint (e.g. `.venv/bin/uta run --class-fqn ...`) and the CI/repair flows. New behavior is
controlled by configuration/env (final names decided in design):

- A **mode flag** selecting the generation strategy: `batch` (byte-budgeted batched generation) or
  `hard-cap` (rollback drop-at-`maxSelected` behavior). **Default = `batch`** after node2 verification on
  real targets.
- A **generated-bytes budget** setting (e.g. `python_mutation_generation_max_generated_bytes`, env
  `UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES`), used only in `batch` mode.
- A **max-batch guard** (e.g. `python_mutation_generation_max_batches`) sized so
  `max_batches × adapter_generation_timeout` stays within the overall verification timeout.

**Budget default is derived from the §13.7/§13.8 performance test, not guessed.** From the measured
real-module parse curve (Python 3.12.10): 4 MB → 0.6 s, **8 MB → 1.7 s**, 16 MB → 7 s, 32 MB → 51 s; and
the node2 batch run where ~9–12 MB/batch (K=8) gave ~13 s/batch and ~103 s total. To keep each batch in
the linear/cheap regime (~1–2 s parse), the **default budget = 8 MB** (16 MB is the practical upper edge
of the linear regime if fewer/larger batches are preferred to cut per-batch overhead). Design picks the
final value and the estimate calibration within this evidence-bounded range.

## 5. Acceptance Criteria

1. **Hang fixed.** For the Appendix-A reproduction target (~6071 mutants), Python3 mutation verification
   completes within `python_mutation_adapter_generation_timeout_seconds` with no single generated module
   exceeding the configured byte budget, and no >40 min parse.
2. **Lossless vs. unbatched.** For inputs where an unbatched run also completes, batching is at parity:
   **equal set of evaluated `opportunity_id`s, equal per-`opportunity_id` exit code, and equal verdict**
   (parity is defined on opportunity identity/outcome, not on `candidatePlanId` or raw mutmut tool-keys,
   which intentionally differ by strategy). Batching changes performance, not outcomes.
3. **Honest aggregation.** The aggregated candidate plan reports counts as sums and survivors as the
   union across batches; verdict derives from the aggregate.
4. **Determinism.** Identical inputs ⇒ identical partition, `candidatePlanId`, and verdict across reruns.
   The plan fingerprint includes the partition scheme, batch count, and byte budget.
5. **No collisions.** Each function's mutants live in exactly one batch (whole-function partitioning);
   batches run sequentially with `_cleanup_mutation_state` between them; aggregation keys on global
   `opportunity_id`, never raw mutmut `__mutmut_N` keys.
6. **Batching replaces the full-profile hardcap; CI sampling unchanged.** The full cap profile no longer
   drops mutants at `maxSelected`; it generates all of them across byte-budgeted batches. The CI cap
   profile's representative sampling logic is byte-for-byte unchanged. The byte-budget default traces to
   the §13.7/§13.8 performance data.
7. **Over-budget single function** (deferred case) is handled by a reason-coded cap/skip recorded in
   evidence (e.g. `omittedByGeneratedBytesCap`) plus a **user-facing warning**, never a silent drop.
   Likewise, a partition exceeding the **max-batch guard** trims lowest-priority functions with
   `omittedByMaxBatches` + warning, bounding total wall time within the overall timeout.
8. **Verified default-on rollout.** The mode flag defaults to `batch` after real node2 verification
   confirmed bounded runtime and full mutmut stats-cache reuse across batches. `hard-cap` remains the
   rollback value and preserves the old drop-at-`maxSelected` behavior.

## 6. Boundaries

**Always:**
- Keep mutation selection and partitioning deterministic; aggregate counts/survivors honestly.
- Run batches **sequentially** and clean only generated mutant source files between them, preserving
  `mutants/mutmut-stats.json`, `.mutmut-cache`, and `.coverage` for stats/coverage reuse.
- Partition **whole functions** into batches; size by estimated generated bytes
  (≈ `(selected mutants in function + 1) × function_source_bytes`).
- Surface any cap/skip with an explicit reason code in evidence.
- Use the global `opportunity_id` (line/symbol/operator/source) as the aggregation identity.

**Ask first:**
- Changing default cap values or the byte-budget default.
- Any parallel batch execution design (requires per-batch isolated cwd/mutants-root).
- Patching mutmut itself (e.g. removing its validation `ast.parse`).
- Expanding scope to the orphan-reaping or intra-function-split items.

**Never:**
- Parallelize batches within one workspace (clobbers shared `mutants/`, `.meta`, cache).
- Silently drop mutants without reason-coded evidence (no un-audited gate weakening).
- Treat raw mutmut `__mutmut_N` keys as globally unique across batches.
- Modify the CI cap-profile sampling logic, opportunity collection, suppression, or one-per-line
  selection — this round only swaps the full-profile hardcap for batching and adds aggregation around it.
- Touch the Python2 `mutmut==1.5.0` legacy lane.

## 7. Project Structure / Code Style

- Changes are confined to `uta/language/python/verification/runner.py`,
  `uta/language/python/mutation_candidates.py`, `uta/engine/mutation_candidates.py`, and `uta/config.py`;
  match existing patterns (dataclasses, reason-coded evidence dicts, settings via `getattr(settings, ...)`).
- New code follows surrounding conventions: language-neutral `mutationTool*` evidence naming, candidate-plan
  fingerprinting, and the existing CI/full cap-profile split.

## 8. Testing Strategy

- **Unit:** deterministic partitioning (stable function order, byte-budget bin-pack); aggregation
  (sum/union) across synthetic per-batch results; fingerprint stability; over-budget single-function
  reason-coded fallback.
- **Integration:** batched vs. single-generation on a small Python target produce identical evaluated
  mutants and verdict (AC2); a synthetic large-function target produces multiple batches each under
  budget and a correct aggregate.
- **Real verification (node2):** the Appendix-A target completes within timeout with batched generation
  enabled; record per-batch module sizes and total wall time as evidence (compare to the K=4/K=8 data).

## 9. Open Questions For Design Phase

1. Byte-budget default is set from the §13.7/§13.8 data (8 MB; 16 MB upper edge). Remaining: the
   estimate's **calibration factor** (mutants-per-opportunity multiplier), since the a-priori estimate
   precedes generation, plus whether to validate against the measured per-batch module size post-generation.
2. Whether to keep any residual `maxSelected` as an extreme safety bound, or remove it entirely now that
   batching is lossless.
3. Resolved: mode flag is `python_mutation_generation_strategy`; default is `batch`, with `hard_cap` as
   the rollback value.
4. Evidence/report field names for batch scheme, per-batch sizes, and the over-budget reason code.
