# Usage: Python Mutation Batched Generation

Implements [`docs/design-python-mutation-batched-generation.md`](design-python-mutation-batched-generation.md)
(Appendix A §13.8). This is an internal behavior of the Python3 mutation verifier — there is **no new CLI
command**. Behavior is selected by settings/env.

## What it does

mutmut 3 packs all mutants of a source file into one module; CPython's tokenizer is super-linear in the
size of a single `ast.parse`/`compile` call, so a large target can hang for tens of minutes (see
`design-python-mutation-scalability.md` Appendix A). The `batch` strategy partitions the selected
mutation opportunities into function-granular, byte-budgeted batches, generates and scores each batch
sequentially (each generated module stays in the tokenizer's linear regime), and aggregates the results
into one verdict — **lossless** (all mutants are still tested).

## Settings

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `python_mutation_generation_strategy` | `UTA_PYTHON_MUTATION_GENERATION_STRATEGY` | `batch` | `batch` = byte-budgeted batched generation; `hard_cap` = rollback behavior that drops selected mutants beyond `max_selected`. |
| `python_mutation_generation_max_generated_bytes` | `UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES` | `8000000` | Per-batch generated-module byte budget (batch mode only). 8 MB keeps each parse ~1–2s. |
| `python_mutation_generation_max_batches` | `UTA_PYTHON_MUTATION_GENERATION_MAX_BATCHES` | `12` | Max batches per target; bounds total wall time. Excess is trimmed by priority. |

`python_mutation_generation_max_selected` (1000) is unchanged and governs only `hard_cap`.

## Default and rollback

- **Default is `batch`** after node2 verification showed real targets complete in bounded time and full
  mutmut stats collection is reused across batches.
- To force the old behavior for diagnosis or emergency rollback:

  ```bash
  UTA_PYTHON_MUTATION_GENERATION_STRATEGY=hard_cap \
  .venv/bin/uta run --class-fqn <target>
  ```

- Normal production runs should leave the setting unset so the code default `batch` is used.
- Batch mode currently applies to the **full** cap profile only; the CI cap-profile sampling path is
  unchanged (when CI sampling is active, batching is not engaged).

## Evidence

In `batch` mode the candidate-plan evidence adds:

- `generationStrategy: "batch"`, `batchCount`, `partitionSignature`, `maxGeneratedBytes`.
- `batches: [{index, functionCount, allowedLineCount, estimatedBytes, batchGeneratedBytes, overBudget}]`.
- `omittedByGeneratedBytesCap` / `omittedByMaxBatches` — lines dropped by the (lossy) fallbacks, with a
  `mutationBatchWarnings` list and an `artifacts["mutation_batch_warnings"]` string. These appear only
  when a single function exceeds the byte budget or the partition exceeds `max_batches`; normal runs are
  lossless and emit neither.
- `artifacts["mutation_generation_strategy"]` records the strategy used for every run (both modes).

The aggregate `candidatePlanId` incorporates the strategy, batch count, byte budget, and partition
signature, so reruns with identical inputs remain comparable; it intentionally differs from the
`hard_cap` plan id for the same target.

## Operational notes

- Batches run **sequentially** with generated mutant source files cleaned between them while preserving
  `mutants/mutmut-stats.json`, `.coverage`, and `.mutmut-cache`. Node2 verification confirmed full
  mutmut stats collection runs once across batches, though `mutmut run` still performs per-batch clean
  test/list-all-tests/forced-fail checks. Do not run batches in parallel in one workspace.
- Known limitation (deferred): a single function whose mutants alone exceed the byte budget is capped
  with a reason code rather than split losslessly across batches.
