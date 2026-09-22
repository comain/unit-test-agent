# Coverage ROI Integration Plan

## Context

The coverage-roi doc (`~/wms/docs/coverate-roi.md`) defines a method-level effort scoring model to prioritize test generation by ROI: cheap methods first, expensive methods last. Currently UTA selects candidates by git change frequency and processes them in-order with no awareness of which methods are cheap vs expensive to cover.

**Goal**: Integrate coverage ROI scoring into UTA so the LLM can:
1. Read a per-class method ROI scores file from `.uta_cache/context/`
2. Prioritize cheap methods first to meet the coverage gate faster
3. Judge whether to lower the gate when remaining methods are all expensive

**Decisions**:
- Use tree-sitter only for complexity (no SonarQube/PMD dependency)
- Method-level ranking with a filter: cover ranked methods first to meet coverage gate
- Score files cached in `.uta_cache/context/` so later runs reuse them directly

---

## Step 1: Add method-level complexity extraction to tree-sitter parser

**File**: `uta/language/java/parse/java_parser.py`

The parser currently extracts method signatures, params, return types, annotations, and calls. It does NOT extract method body complexity. We need to count control-flow nodes within each method body.

**Add a new method** `_extract_method_complexity(self, body_node: Node) -> dict`:
- Walk the method body AST and count:
  - `if_statement` → branch count
  - `for_statement`, `while_statement`, `do_statement`, `enhanced_for_statement` → loop count
  - `catch_clause` → catch count
  - `try_statement` → try count
  - `throw_statement` → throw count
  - `ternary_expression` → ternary count
  - `switch_expression`, `switch_block_statement_group` → switch case count
- Compute `cyclomatic_approx = 1 + branches + loops + catches + ternaries + switch_cases`
- Count total lines: `body_node.end_point[0] - body_node.start_point[0] + 1`
- Return `{"cyclomatic_approx": int, "body_lines": int, "branches": int, "loops": int, "catches": int, "throws": int}`

**Modify** `_traverse()` method (line 151-174): when processing a method_declaration, call `_extract_method_complexity(body)` and store the result alongside the existing call extraction. Add a `complexity` field to `ParsedSymbol`.

**Model change** in `uta/language/java/parse/models.py`: add an optional `complexity: Optional[Dict] = None` field to `ParsedSymbol`.

---

## Step 2: Create the ROI scorer module

**New file**: `uta/language/java/scoring/coverage_roi.py`

This module computes method-level effort scores following the coverage-roi doc's scoring model, adapted for tree-sitter data.

### `compute_method_effort(method_node, graph, class_fqn) -> dict`

Input: a GraphNode for a method, the CodeGraph, and the parent class FQN.

Scoring (adapted from the doc, tree-sitter approximations):

1. **Control Flow Score** (0-4):
   - `cyclomatic_approx <= 3` → 0
   - `4-7` → 1, `8-12` → 2, `>12` → 3
   - Cap at 4

2. **Dependency Score** (0-4):
   - Count outgoing CALLS edges from this method to methods on OTHER classes (collaborator calls)
   - `0-1` → 0, `2-3` → 1, `4-5` → 2, `6+` → 3
   - +1 if calls cross domain boundaries (detect via field types: Mapper/Dao = storage, Client/Remote/Rpc = remote, Producer/Sender = messaging)

3. **Non-Determinism Score** (0-4):
   - Scan call names for: `currentTimeMillis`, `nanoTime`, `random`, `UUID`, `sleep`, `submit`, `execute`, `await`, `CompletableFuture`, `CountDownLatch`
   - Scan for static method calls (receiver is a capitalized identifier with no field match)
   - +1 per signal, cap at 4

4. **Setup Score** (0-3):
   - Estimate from param count + collaborator count:
     - `params <= 2 and collaborators <= 1` → 0
     - `params <= 4 and collaborators <= 3` → 1
     - else → 2
   - +1 if class has >6 injected fields (fields with @Autowired/@Resource/@Inject)

5. **Purity Bonus** (-2 to 0):
   - If method has 0 collaborator calls and cyclomatic <= 3 → -2
   - If method has 1 collaborator call and cyclomatic <= 5 → -1

6. **Final**: `effort_score = max(0, sum of above)`, then band:
   - `0-2` → "cheap"
   - `3-5` → "medium"
   - `6+` → "expensive"

### `compute_class_roi(class_fqn, graph, jacoco_xml_path=None) -> dict`

For each public method in the class:
- Compute effort score
- If JaCoCo XML is available, get per-method missed lines/branches
- Compute `roi_score = missed_lines / max(effort_score, 1)`
- Sort methods by roi_score descending (highest ROI first)

Return:
```python
{
    "class_fqn": str,
    "methods": [
        {
            "name": str,
            "signature": str,
            "start_line": int,
            "effort_score": int,
            "effort_band": str,  # "cheap" | "medium" | "expensive"
            "effort_reasons": [str],
            "missed_lines": int,  # from JaCoCo, 0 if no data
            "missed_branches": int,
            "roi_score": float,
        }
    ],
    "summary": {
        "total_methods": int,
        "cheap_count": int,
        "medium_count": int,
        "expensive_count": int,
        "estimated_cheap_coverage_lines": int,
    }
}
```

---

## Step 3: Export ROI scores to cached context file

**File**: `uta/language/java/context_builder.py`

**Add method** `export_roi_scores(self, class_fqn, roi_data, *, jacoco_xml_path=None) -> str`:
- Write to `.uta_cache/context/{ClassName}.roi.md`
- Cache key: hash of source file mtime + JaCoCo xml mtime (if present)
- If cache file exists and key matches, skip recomputation → **reuse across runs**
- Returns the absolute path to the file

**File format** (markdown, LLM-readable):
```markdown
# ROI Scores: {ClassName}

## Summary
- Total public methods: N
- Cheap (0-2): N methods, ~X uncovered lines
- Medium (3-5): N methods, ~X uncovered lines  
- Expensive (6+): N methods, ~X uncovered lines

## Method Rankings (highest ROI first)

| # | Method | Effort | Band | Missed Lines | ROI | Reasons |
|---|--------|--------|------|-------------|-----|---------|
| 1 | methodA | 1 | cheap | 25 | 25.0 | pure-calc |
| 2 | methodB | 3 | medium | 18 | 6.0 | 3-collaborators |
| ...

## Coverage Strategy Guidance
- Cover methods #1-#N (cheap band) first → estimated ~X lines covered
- If gate is met, stop. If not, continue to medium band.
- Expensive methods should only be attempted if gate cannot be met otherwise.
- If all remaining methods are expensive, consider lowering the gate to {realistic_gate}%.
```

---

## Step 4: Integrate into the workflow

### 4a. Wire ROI scoring into `parse_context` node

**File**: `uta/graph/nodes.py` — `parse_context()` (line 1001)

After building the graph and filtering candidates, for each candidate class:
1. Call `compute_class_roi(class_fqn, graph)` (no JaCoCo at this point — first run has no baseline)
2. Call `ctx_builder.export_roi_scores(class_fqn, roi_data)`
3. Store ROI path in `target_context_paths[class_fqn]["roi_abs"]`

### 4b. Add baseline JaCoCo collection (optional, for repos with existing tests)

**File**: `uta/graph/nodes.py` — new node or within `baseline_compile`

After baseline compile succeeds:
- Check if existing test classes exist (glob `**/src/test/java/**/*Test.java`)
- If yes, run `mvn test org.jacoco:jacoco-maven-plugin:0.8.12:report` once
- Parse the resulting JaCoCo XML to get per-class baseline coverage
- Pass this to `compute_class_roi()` for more accurate missed-lines data
- **Cache the baseline report path** so ROI scorer can reference it

This is optional — if no existing tests, all methods have `missed_lines = body_lines` (estimated from tree-sitter).

### 4c. Pass ROI file path to LLM sessions

**File**: `uta/graph/nodes.py` — `generate_and_validate()` (line ~1800)

When building `target_context_paths`, include the `roi_abs` path. The planning and generation prompts already reference `target_context_files` — add the ROI file there.

Modify `plan_target_context` construction (line 1828-1831) to include:
```python
f"  - roi scores: `{target_context_paths[class_fqn]['roi_abs']}`"
```

### 4d. Update planning prompt to reference ROI scores

**File**: `uta/prompts/plan_tests.txt`

Add a section:
```
### ROI SCORES
Read the ROI scores file for each target class before planning.
- Prioritize cheap-band methods first — they give the most coverage per effort.
- Plan medium-band methods only if cheap methods alone cannot meet the gate.
- Skip expensive-band methods unless they are business-critical or the gate cannot be met otherwise.
- If the gate cannot realistically be met (all remaining methods are expensive), note a realistic achievable gate in COVERAGE RISKS.
```

### 4e. Update coverage-fix prompt to reference ROI scores

**File**: `uta/prompts/fix_coverage.txt`

Add after the uncovered clusters section:
```
### METHOD ROI SCORES
Consult the ROI scores file at `{{ roi_abs }}` to decide which uncovered methods to target.
- Focus on cheap and medium methods that are still uncovered.
- Do not chase expensive methods for marginal coverage gains.
- If remaining uncovered methods are all expensive, report that the current coverage is the realistic maximum.
```

---

## Step 5: Candidate class re-ranking by aggregate ROI

**File**: `uta/graph/nodes.py` — after `parse_context()` computes ROI for all candidates

Re-sort `final_candidates` by aggregate class ROI:
```python
class_roi = sum(m["roi_score"] for m in roi_data["methods"])
```

Classes with more cheap uncovered methods get processed first. This ensures the overall run maximizes coverage gain early.

---

## Step 6: Config additions

**File**: `uta/config.py`

```python
# Enable ROI-based method prioritization in prompts
roi_enabled: bool = True

# Skip classes where all public methods are in the "expensive" band
roi_skip_all_expensive: bool = False
```

No `--roi-threshold` for now — keep it simple. The LLM judges based on the scores file.

---

## Files to modify

| File | Change |
|------|--------|
| `uta/language/java/parse/models.py` | Add `complexity` field to `ParsedSymbol` |
| `uta/language/java/parse/java_parser.py` | Add `_extract_method_complexity()`, wire into `_traverse()` |
| `uta/language/java/scoring/__init__.py` | New package |
| `uta/language/java/scoring/coverage_roi.py` | New: `compute_method_effort()`, `compute_class_roi()` |
| `uta/language/java/context_builder.py` | Add `export_roi_scores()` with cache-key check |
| `uta/graph/nodes.py` | Wire ROI into `parse_context()`, pass to prompts |
| `uta/prompts/plan_tests.txt` | Add ROI scores section |
| `uta/prompts/fix_coverage.txt` | Add ROI reference |
| `uta/config.py` | Add `roi_enabled`, `roi_skip_all_expensive` |

## Files to create

| File | Purpose |
|------|---------|
| `uta/language/java/scoring/__init__.py` | Package init |
| `uta/language/java/scoring/coverage_roi.py` | Core scoring logic |
| `tests/test_coverage_roi.py` | Unit tests for scoring |

---

## Verification

1. **Unit tests**: Test `compute_method_effort()` with mock GraphNodes of varying complexity. Test `compute_class_roi()` with and without JaCoCo data.
2. **Integration test**: Run `uta parse` on a test repo, check that `.uta_cache/context/{Class}.roi.md` is created with valid scores.
3. **Cache reuse**: Run `uta parse` twice on the same repo — second run should skip recomputation.
4. **E2E**: Run `uta run` on a target class and verify:
   - ROI file is created and referenced in the plan
   - LLM plan prioritizes cheap methods first
   - Coverage-fix prompt references ROI scores
   - Final coverage is comparable or better than without ROI
