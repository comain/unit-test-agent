# Mutation ROI Prioritization

## Context

Commit `7dacfbe` introduced coverage ROI scoring — methods are ranked by `uncovered_lines / effort_score` so the LLM tackles cheap high-return methods first to hit the JaCoCo gate. Mutation testing still uses only a killability heuristic (`uta/maven/pitest.py:113-133`) that scores *likelihood to kill* per family but ignores *effort to write the killing test*. The result: the LLM often spends iterations on costly survivors (e.g. side-effect mutants on methods with 6+ collaborators or heavy async) before exhausting easy wins like boundary/conditional flips on pure methods.

Goal: extend the ROI idea into the mutation fix loop so `fix_mutations.txt` receives survivors ranked by `(killability × count) / effort`, mirroring what coverage already does. This should raise mutation pass-rate within the existing 2-round fix budget (`nodes.py:2588-2628`).

Best-practice alignment (Trail of Bits 2026, Kaufman et al. ICSE 2022): prioritize high-severity mutants, deprioritize equivalent/no-op mutants, and concentrate limited test-writing budget on mutants with the best kill-per-effort ratio.

## Design

### 1. New module: `uta/language/java/scoring/mutation_roi.py`

One public entry: `compute_mutation_roi(families, method_efforts) -> list[ScoredFamily]`.

- **No re-scoring.** Base effort is pulled straight from the `MethodEffort` objects already produced by `compute_class_roi()` for the coverage ROI file — that call is memoized by source+JaCoCo mtime (`coverage_roi.py:502`, `context_builder.py:400-430`), so it's free on the second read. The mutation scorer only layers O(1) per-family deltas on top; it never re-walks the graph or re-parses the class.
- Effort per family:
  - Base = `method_effort.effort_score` (reused — captures cc, collaborators, non-determinism, setup).
  - Family adjustment:
    - `boundary`, `conditional`, `return_value` → +0 (value-tweak tests)
    - `math`, `negation` → +0
    - `side_effect` → +2 (needs mock/verify or state assertion)
    - `other` → +1
  - Detail adjustment: `"removed call"` on void methods → +1 (observability hard).
- Killability reused from `_killability_score()` (`pitest.py:113`).
- Equivalent-mutant filter: extend `_is_metric_side_effect()` (`pitest.py:106`) with a `_likely_equivalent()` check for known patterns (e.g. `side_effect` on getters, `removed call` to logging). Flagged families get `roi = 0` and `deprioritized = True`.
- Score: `roi = (count * killability_score) / max(effort, 1)`.
- Return sort key: `(deprioritized, -roi, -killability, method, lines[0])` — replacing the current key at `pitest.py:172-181` when the flag is on.

### 2. Integrate into `summarize_surviving_mutants`

`uta/maven/pitest.py:136-181` — add optional `method_efforts=None` param. When provided and `mutation_roi_enabled`, use the new sort key and attach `roi`, `effort_band`, `effort_score` to each family dict.

### 3. Extend markdown output

`format_mutation_families_markdown()` (`pitest.py:184-209`) — when ROI data present, add columns: `killability | effort | roi` and an ordering caption ("ranked by kill-per-effort"). Existing consumers (`nodes.py:1554-1560` writing `.mutation_families.md`) keep working.

### 4. Wire into the fix round

`uta/graph/nodes.py:1531-1607` (`_run_focused_mutation_fix_round`):
- Before calling `summarize_surviving_mutants`, invoke `compute_class_roi()` (already cached) and extract `method_efforts`.
- Pass `method_efforts` into `summarize_surviving_mutants`.
- When flag off: path is unchanged.

### 5. Prompt update

`uta/prompts/fix_mutations.txt` — add a conditional `{% if mutation_roi_enabled %}` block (same Jinja pattern as `plan_tests.txt:36-43`) instructing the model: families are ranked by kill-per-effort, start with the top entry, skip `effort_band=expensive` rows unless the gate otherwise unreachable, never write tests for rows marked `likely_equivalent`.

### 6. Config

`uta/config.py` — add:

```python
mutation_roi_enabled: bool = True
mutation_roi_skip_expensive: bool = False
```

Mirrors the existing `roi_enabled` / `roi_skip_all_expensive` pair (`config.py:32-34`) so operators have a single mental model.

### 7. Tests

New `tests/test_mutation_roi.py`:
- Unit test `compute_mutation_roi` on synthetic families + method efforts → asserts boundary/pure-method family ranks above side_effect/heavy-deps family.
- Regression test: when flag off, ranking matches the current killability-only order (snapshot).
- Equivalent-mutant test: a `side_effect` survivor on a getter gets `deprioritized=True`.

Extend `tests/test_coverage_roi.py` only if `MethodEffort` needs a new exposed field.

## Files to touch

| File | Change |
|------|--------|
| `uta/language/java/scoring/mutation_roi.py` | **new**: scoring module |
| `uta/maven/pitest.py:136-209` | accept `method_efforts`, new sort key, richer markdown |
| `uta/maven/pitest.py:106` | extend equivalent-mutant heuristic |
| `uta/graph/nodes.py:1531-1607` | pass method efforts through |
| `uta/prompts/fix_mutations.txt` | ROI guidance block |
| `uta/config.py:32-34` | two new flags |
| `tests/test_mutation_roi.py` | **new** |

## Verification

1. `pytest tests/test_mutation_roi.py tests/test_coverage_roi.py -q` — new + regression tests green.
2. `pytest -q` — full suite, ensure nodes.py wiring did not break `tests/test_opencode_config.py` or graph tests.
3. End-to-end: run `./run_uta.sh` on a sample Java module that previously failed the mutation gate. Capture the written `.mutation_families.md` — confirm it shows a `roi` column and the top entry is a high-killability / low-effort family.
4. Diff mutation score across runs with flag on vs off on the same class; expected: equal or higher score in the same 2-round budget.

## Out of scope
- Changing the Pitest mutator set or runtime flags.
- A learned model for effort (stick to heuristics, consistent with `coverage_roi`).
- Cross-class mutation ranking (current flow is per-class).
