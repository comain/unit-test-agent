# Further Token Optimization Strategies for `uta`

## Context

`token_opt.md` already covers 8 strategies (breadth gate, structured plan, call-sig cache, `compile_context.md` repair artifact, Jacoco uncovered-region feeding, bounded planning subagent, provider-limit reporting, report flushing). These mostly target *what is in each prompt*. This plan proposes additional strategies that target three other waste sources surfaced by the code review:

1. **Session/history waste** — all phases share one OpenCode session, so every compile-fix turn re-feeds plan + generation + all prior fixes to the model (uta/graph/nodes.py:2086 creates a separate generation session, but compile/coverage/mutation loops still accumulate inside it).
2. **Prompt-prefix waste** — the 15 KB `generate_test.txt` and 4.9 KB `fix_mutations.txt` are re-sent verbatim each invocation without any provider cache hint.
3. **Tool-call waste inside the agent turn** — run logs (`picking_flash_run5.log`) show the same `grep` for `State`, `VirtualType`, `WmsBizConstants`, etc. repeated 5–10× per session because resolutions are not written back into the session context.

The following strategies are additive to `token_opt.md` and ordered by expected ROI.

## Proposed Strategies (additive to token_opt.md)

### A. Provider prompt-prefix caching
- Verification status (2026-04-22): **Non-effective**
- Current decision: disabled in runtime. Stable/volatile prompt splitting remains available for analysis, but OpenCode messages are sent as a single payload because the explicit prefix boundary did not show a meaningful incremental cache benefit in real runs.
- Emit Anthropic `cache_control: {type: "ephemeral"}` (or OpenAI equivalent) on the stable prefix of `generate_test.txt`, `fix_compile.txt`, `fix_coverage.txt`, `fix_mutations.txt`.
- Split each template into `[stable rules] | [volatile per-class vars]` so only the tail changes.
- Touchpoints: `uta/opencode/client.py:298 send_message`, `uta/prompts/*.txt`.
- Expected win: largest single reduction; benchmark shows cache_read already dominates total tokens, so marking the rule section as cacheable converts more of the 15 KB template into cache hits across fix loops.

### B. Phase-scoped sessions with handoff artifact
- Verification status (2026-04-22): **Effective**
- Today plan→generate share a session (line 1987→2086 spins a fresh generation session but fix loops stay in it for the remainder). Compile-fix / coverage-fix / mutation-fix all stack on the same session, so every turn replays all prior fix output.
- Start a new session per phase (`compile_fix`, `coverage_fix`, `mutation_fix`) seeded only with: current test file, `compile_context.md`, and a 20-line handoff summary of what was tried.
- Touchpoints: `uta/graph/nodes.py` `run_compile_fix_loop:1171`, `run_coverage_fix_loop:1327`, `_run_focused_mutation_fix_round:1574`, `_create_phase_session`.
- Expected win: each fix iteration stops carrying 5–10 previous turns of chatter.

### C. Symbol-resolution write-back (in-session cache)
- Verification status (2026-04-22): **Non-effective**
- Current decision: kept as a narrow compile-fix helper only; do not count it as a validated token optimization. It starts too late, only handles unresolved symbols, and did not materially reduce planning/generation rediscovery in the benchmark runs.
- When compile-fix resolves an unresolved type/overload (grep + file read inside the agent turn), append the result to `.symbols.md` and `compile_context.md` before the next iteration so the model sees it without re-grepping.
- Complements `token_opt.md #3` but specifically closes the *re-grep* loop visible in logs.
- Touchpoints: the Jacoco-style post-turn retrospect already exists in `client.py:628 analyze_session_retrospect()`; extend it to write resolved symbols back to `uta/language/java/context_builder.py`-owned artifacts.

### D. Incremental error deltas for fix loops
- Verification status (2026-04-22): **To-be-checked**
- Current read: still unvalidated in the strongest benchmark because the successful 2026-04-22 pass never entered `compile_fix`. The repair-loop win in that run came from better test-fix / coverage-fix behavior, not from compile-error delta compression.
- Current compile-fix prompt dumps the full `mvn` error output each iteration.
- Replace with a diff: only errors new since the prior attempt, plus a stable "recurring errors" summary line.
- Touchpoints: `run_compile_fix_loop:1171` error-assembly block; add a helper that diffs consecutive error sets by `(file, line, message-hash)`.

### F. Mutation-fix scoped to top-ROI survivors
- Verification status (2026-04-22): **Effective**
- Current read: validated in the 2026-04-22 passing benchmark. Mutation attempt 1 landed at `68.2%`, then one focused `mutation_fix` round raised the final score to `79.5%` with only about `71.6k` non-cache tokens in the mutation-fix assistant session.
- `fix_mutations.txt` (4.9 KB) currently carries the full surviving-mutant list. Combine with the existing ROI scorer (`uta/language/java/scoring/`) to send only top-N mutants whose kill is likely to raise coverage gate; drop the long tail.
- Touchpoints: `_run_focused_mutation_fix_round:1574`, `uta/language/java/scoring/mutation_roi.py`.

### G. Hopeless-loop early termination
- Verification status (2026-04-22): **To-be-checked**
- Current read: still not proven by benchmark data. The passing 2026-04-22 run never hit a repeated compile-error signature loop, so there is no live evidence yet that this guard materially saves tokens in real runs.
- If two consecutive compile-fix attempts produce identical error signatures, stop further iterations and escalate (or trigger one structured replan) instead of spending another full turn.
- Touchpoints: same error-delta helper as strategy D; track a small ring buffer in `run_compile_fix_loop`.

### H. Tiered-model routing for deterministic sub-tasks
- Verification status (2026-04-22): **Non-effective**
- Current decision: keep low-tier routing disabled for all quality-sensitive phases. Real runs showed clear regression when `coverage_fix` used the lower tier. Only bounded compile-fix use remains potentially acceptable.
- Symbol-candidate resolution and unresolved-import guessing are cheap, deterministic, and currently done by the main (Gemini Pro) model inside the fix loop.
- Route just those sub-requests to a cheaper model. **For initial testing, use local `qwen3.6-35b-a3b-nvfp4` via Ollama** (already wired into the benchmark suite — see `benchmark/runs/pickingbizimpl_ollama_qwen3.6-35b-a3b-nvfp4_*`). Zero per-token cost during validation; later swap in Haiku/Flash for production.
- Main model stays on generation and repair reasoning.
- Touchpoints: `OpenCodeClient.send_message` already takes `model_id` (client.py:298). Add a thin resolver wrapper that calls the configured cheap-tier model and merges results into `compile_context.md`. Add a settings field `opencode_cheap_model` defaulting to the Ollama qwen tag.

### I. Plan-text compression into generation prompt
- Verification status (2026-04-22): **Non-effective**
- Current read: the latest pass did not validate this as a win. Planning still spent more non-cache tokens than the strongest pre-phase2 near-success benchmark (`58,377 -> 78,683`), and the plan still came out under-broad (`31/72` methods) despite the compressed structured handoff.
- `plan_text` is emitted verbatim into the generation session (captured at `nodes.py:2025`). When planning is verbose (replan path at 2051 asks for extra sections) this can be several KB.
- Before handoff to the generation session, compress to a structured table (method → branch family → wave → test name) — this also enables the breadth gate from `token_opt.md #1/#2` with zero extra parsing.

### J. Static stub catalog (few-shot-on-demand)
- Verification status (2026-04-22): **Non-effective**
- Current decision: disabled by default. Always-on stub injection increased prompt bulk without reducing repo exploration in real runs.
- Maintain a small catalog of Mockito stub patterns for the 10–20 highest-frequency collaborator types (inferred from `dependency_map.md`).
- Inject only the catalog entries whose types appear in the current class `.context.md`. Replaces ad-hoc re-derivation by the model across classes.

## K. Replace deterministic LLM work with Python (high impact)

Verification status (2026-04-22), by sub-strategy:
- Jacoco parser: **Effective**
- PIT parser / green-suite failure detection: **Effective**
- Compile error classifier: **Effective**
- Symbol resolver: **To-be-checked**
- Current read: still not validated by the strongest benchmark because the passing 2026-04-22 run never entered `compile_fix`, which is where the symbol resolver pays off. Repeated source reads remained high in test-fix / coverage / mutation phases, so there is no evidence yet that it reduces real-session churn.
- Deterministic test skeleton injection: **Non-effective** in always-on prompt form; disabled by default
- Plan breadth validator: **Effective**
- Current read: validated by the 2026-04-22 run. The earlier false `OVER` signal is gone; the planner now emitted a plausible `UNDER` warning (`31/72 methods`) instead of the bogus `179/48` count from the broken parser.
- Wave assigner: **Non-effective**
- Current read: pre-computed wave data did not translate into a clearly better first-pass plan. The latest pass still produced an under-broad plan and spent most extra work later in repair loops, so this has not shown a real benchmark win.

Today the model is asked — inside its session, billing tokens — to do work that has no judgement component. Move each to Python and feed the result as a precomputed artifact:

| Today (LLM-driven) | Replace with (Python) | Where |
|---|---|---|
| Greps for unresolved symbol → file | Tree-sitter import resolver + classpath scan; emit candidates list | extend `uta/language/java/context_builder.py`; consume in `compile_context.md` |
| Reads Jacoco HTML/XML and reports `% coverage` | Parse `target/site/jacoco/jacoco.xml`; emit `(method, line-range, branch-count)` table | new `uta/coverage/jacoco_parser.py`; feed to `fix_coverage.txt` |
| Reads PIT mutation HTML and lists survivors | Parse `target/pit-reports/*/mutations.xml`; rank by ROI | extend `uta/language/java/scoring/mutation_roi.py`; feed to `fix_mutations.txt` |
| Writes test class skeleton (package, imports, `@ExtendWith(MockitoExtension.class)`, ctor, `@Mock` fields) | Python template from `.context.md` fields + dependencies | new `uta/templates/test_skeleton.py`; preseed file before generate prompt |
| Categorizes `mvn` compile errors (missing import / wrong type / unresolved symbol / syntax) | Regex over `mvn` output → typed buckets per file:line | new `uta/compile/error_classifier.py`; feeds error-delta helper (D) |
| Assigns plan items to "Wave 1 / Wave 2" by importance | Shared callable prioritizer fed by language-specific context metadata | extend `uta/engine/wave_assigner.py`; feed structured plan (token_opt.md #2) |
| Validates plan breadth (counts `@Test`, planned method families) | Pure Python AST count vs structured plan | new `uta/validate/plan_breadth.py`; the breadth gate from token_opt.md #1 |
| Picks Mockito stub patterns for common collaborators | Static lookup table keyed by collaborator FQN | new `uta/templates/stub_catalog.py`; feeds J |
| Detects hopeless compile loop (same error twice) | Hash error signature, ring-buffer | inline in `run_compile_fix_loop` (G) |
| Decides "session looks rate-limited" / "provider quota hit" | Already partly in `client.py`; finish typed surfacing | extends token_opt.md #7 |

Net effect: each of these saves both *prompt* tokens (less context needed) and *completion* tokens (no LLM reasoning emitted). The compile-fix loop is the largest beneficiary because it currently spends turns re-deriving the same facts.

## L. Self-improving retrospect (cross-iteration learning)

Today `analyze_session_retrospect()` (uta/opencode/client.py:628) runs once per generation session and writes `compile_facts.md`. Enhance it to be the *learning loop* of the workflow:

### L1. Per-run inefficiency log
- Verification status (2026-04-22): **Effective**
- Current read: validated as instrumentation. The 2026-04-22 run wrote per-phase token records and repeated-tool observations into `.uta_cache/learning/com.example.sample.outbound.core.biz.impl.PickingBizImpl.jsonl`, which directly exposed the remaining `read` / `glob` churn.
At end of each run, write `.uta_cache/learning/<class_fqn>.jsonl` with one record per inefficiency observed:
- repeated tool calls (same grep ≥ 3× → record query + correct answer)
- compile errors that recurred ≥ 2 fix turns (record error signature + final fix)
- symbols the model asked about that were already in `.symbols.md` (record coverage gap)
- plan items the model wrote that compiled but were dropped at coverage stage (over-spec)
- token spend per phase (`plan`, `generate`, `compile_fix`, `coverage_fix`, `mutation_fix`)

This is built by the same Python parsers as strategy K, so it costs 0 LLM tokens.

### L2. Anti-pattern catalog → next-run preseeding
- Verification status (2026-04-22): **Non-effective**
- Current decision: replay hints are no longer injected into first-pass planning/generation. Broad replay guidance made the model more cautious and search-heavy instead of reducing churn.
On the *next* run for the same class (or the same project), the planner reads `<class_fqn>.jsonl` and:
- **Preseeds `compile_context.md`** with the symbol resolutions the previous run had to discover
- **Preseeds the generation prompt** with a "previously you wasted N turns on X — do Y instead" line
- **Adjusts plan breadth gate** based on prior over-spec / under-spec ratio
- **Picks a model tier** (H) based on prior cost-vs-success

### L3. Project-level rollup
- Verification status (2026-04-22): **To-be-checked**
- Current read: the rollup file is generated, but it is not validated as an optimizer yet. The 2026-04-22 run produced `.uta_cache/learning/project_summary.json`, however it only contains one class/run worth of medians and did not feed any demonstrated tuning decision in the benchmark itself.
Aggregate per-class jsonl into `.uta_cache/learning/project_summary.json`:
- top 20 collaborator types by re-grep frequency → seed the static stub catalog (J) automatically
- top compile error signatures across project → emit project-wide rule additions to `test_generation_guidance.md`
- per-phase median token cost → tune timeouts and bounded-planning trigger thresholds (token_opt.md #6)

### L4. Regression detection
- Verification status (2026-04-22): **Effective**
On every run, compare phase token cost vs the rolling median in `project_summary.json`. If a phase exceeds 2× median, log a `RETROSPECT_REGRESSION` warning into the final report so the user sees drift early instead of after a bad benchmark.

## Post-plan follow-up: Tree-sitter index CLI

- Verification status (2026-04-22): **Non-effective**
- Current read: the tree-sitter-backed `uta-query-index` CLI did **not** contribute to the 2026-04-22 passing benchmark.
- In the live run, both planning and generation reported that `bin/uta-query-index` was not present in the target checkout and fell back to `read` / `grep` / `glob`.
- The wrapper currently lives in the UTA repo and resolves `REPO_ROOT` from its own script path, so even an absolute-path invocation would still point at the UTA repo rather than the target repo checkout.
- Conclusion: do not count the current tree-sitter CLI wiring as a validated optimization until the command is callable from the target repo and indexes the target repo rather than the UTA repo.

### L5. Touchpoints
- `uta/opencode/client.py:628 analyze_session_retrospect()` — extend to call new Python collectors instead of LLM analysis
- new `uta/learning/` package — `recorder.py`, `replayer.py`, `summary.py`
- `uta/language/java/context_builder.py` — read `.uta_cache/learning/*.jsonl` during `parse_context` to preseed
- `uta/graph/nodes.py` — wire `RETROSPECT_REGRESSION` into final report flushing (overlaps with token_opt.md #8)

## Recommended implementation order

1. **A. Prompt-prefix cache markers** — smallest code change, largest expected reduction given observed cache_read dominance.
2. **K (subset: Jacoco parser, error classifier, symbol resolver)** — unlocks D, F, and the L1 collectors with the same code.
3. **C. Symbol write-back** — directly cancels the re-grep pattern seen in logs (uses the K resolver).
4. **D + G. Error-delta + hopeless-loop guard** — share one helper, cut repair-loop cost in half on bad cases.
5. **L1 + L2. Inefficiency recorder + next-run preseeding** — converts each run into improvement for the next; cheap once K is in place.
6. **B. Phase-scoped sessions** — requires workflow changes; highest structural leverage once sessions are isolated.
7. **I. Plan compression** — unlocks breadth gate (`token_opt.md #1`) cleanly.
8. **F. Mutation ROI filter** — meaningful for classes that reach mutation phase.
9. **L3 + L4. Project rollup + regression detection** — needs ≥ a few runs of L1 data to be useful.
10. **K (remaining: skeleton, plan-breadth validator, wave assigner)** — pure Python, can land any time.
11. **H. Tiered-model routing** — highest payoff but biggest integration cost (provider config, attribution in reports).
12. **J. Static stub catalog** — fed automatically by L3, so land after L3.

## Critical files to modify

- `uta/graph/nodes.py` — session lifecycle (1171, 1327, 1574, 1957, 2086), error delta helper, plan compression, regression warnings
- `uta/opencode/client.py:298, 628` — cache_control, tiered model routing, retrospect → Python collectors
- `uta/prompts/*.txt` — split stable vs volatile regions
- `uta/language/java/context_builder.py` — symbol/compile-facts write-back; preseed from learning jsonl
- `uta/language/java/scoring/mutation_roi.py` + `fix_mutations.txt` — ROI-filtered mutant list
- new `uta/coverage/jacoco_parser.py`, `uta/compile/error_classifier.py`, `uta/templates/test_skeleton.py`, `uta/templates/stub_catalog.py`, `uta/validate/plan_breadth.py`, `uta/learning/{recorder,replayer,summary}.py`

## Verification

- Re-run the `pickingbizimpl_ollama_qwen3.6-35b-a3b-nvfp4` and Gemini baseline on the same class; compare `benchmark/comparison.md` totals for `input`, `output`, `cache_read`, `cache_write`.
- Expect: cache_read share rises after (A); total non-cache input drops after (B)+(C)+(D).
- Run `tests/test_workflow.py` to confirm phase-scoped sessions don't regress existing flow.

## Test Plan

Each strategy ships with a unit/integration test before the benchmark gate.

### Unit tests (fast, no LLM)
- `tests/test_prompt_prefix_stable.py` (A) — render `generate_test.txt` and `fix_compile.txt` with two different var sets; assert the prefix slice (up to a `{# CACHE_BOUNDARY #}` marker) is byte-identical.
- `tests/test_jacoco_parser.py` (K) — feed a fixture `jacoco.xml`; assert returned `(method, line-range, branch-count)` rows match expected.
- `tests/test_error_classifier.py` (K) — feed captured `mvn` output snippets from `picking_flash_run*.log`; assert each line classified into the right bucket.
- `tests/test_symbol_resolver.py` (C, K) — given a synthetic mini-repo, resolve an unresolved type to its candidate file(s); assert ranking.
- `tests/test_test_skeleton.py` (K) — given a `.context.md`, generate a test skeleton; assert it compiles standalone (`mvn test-compile` in a tiny fixture module).
- `tests/test_plan_breadth_validator.py` (K, I) — given structured plan + generated test file AST, assert breadth gate verdict for under-spec / over-spec / pass cases.
- `tests/test_error_delta.py` (D) — feed two consecutive error sets; assert only the new errors are returned and recurring summary is correct.
- `tests/test_hopeless_loop_guard.py` (G) — simulate identical error twice; assert loop short-circuits.
- `tests/test_mutation_roi_filter.py` (F) — feed PIT XML + ROI scores; assert only top-N survivors are kept.
- `tests/test_learning_recorder.py` (L1) — simulate a fake session transcript with repeated greps; assert jsonl record has correct anti-pattern entries and per-phase token totals.
- `tests/test_learning_replayer.py` (L2) — given a prior jsonl, assert preseeded `compile_context.md` contains expected resolutions and the prompt carries the "previously wasted N turns on X" line.
- `tests/test_learning_summary.py` (L3, L4) — aggregate fixture jsonls; assert top-K collaborator types, regression flag fires when phase exceeds 2× rolling median.

### Integration tests (real OpenCode, cheap model)
Use the local `ollama/qwen3.6-35b-a3b-nvfp4` tier — no API spend.
- `tests/test_phase_session_isolation.py` (B) — run plan→generate→one compile-fix; assert the compile-fix session does not contain plan or generation messages.
- `tests/test_tiered_routing.py` (H) — invoke the symbol-resolver wrapper; assert it dispatches to `opencode_cheap_model`, returns within budget, and merges back into `compile_context.md`.
- `tests/test_retrospect_endtoend.py` (L1+L2) — run a small target class twice; assert second run's first compile-fix iteration succeeds at least one turn faster than the first run, and `RETROSPECT_REGRESSION` is *not* raised.

### Benchmark gates (final verification, on the `PickingBizImpl` rig)
Compared against the current `benchmark/comparison.md` baseline:
- After (A): `cache_read / total_input` ratio increases ≥ 20% absolute.
- After (B)+(C)+(D)+(K-subset): non-cache `input` tokens for compile-fix phase drop ≥ 30%.
- After (L2) on second run of the same class: total tokens drop ≥ 15% vs the first run with the rest of the changes already in.
- Final report `.uta_reports/*.json` must always be flushed even when run hits provider limit (overlaps token_opt.md #8 — covered by existing `tests/test_workflow.py` extensions).
