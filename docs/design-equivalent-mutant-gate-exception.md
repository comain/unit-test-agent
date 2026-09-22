# Design: Session-scoped equivalent-mutant gate exception

Status: approved (v2)
Approved-by: user 2026-09-14
Spec: [`spec-equivalent-mutant-gate-exception.md`](spec-equivalent-mutant-gate-exception.md) (approved 2026-09-14)
ADR: [`ADR-016`](decisions/ADR-016-llm-judged-session-scoped-equivalent-mutant-override.md)
Jira: N/A (personal UTA tool work). Single repo, so the detail design is folded in as Part 2.

## Contents

1. Goals and non-goals
2. High-level design
3. Relationships and cooperation
4. Data dependency flow
5. Key process flow
6. Key design tradeoffs
7. Capacity, reliability, security
8. Failure-mode handling
9. Rollout
10. Verification plan
11. Design-review dispositions
12. Part 2 — Detail design (unit-test-agent)
13. Changelog

---

## 1. Goals and non-goals

**Goals** (spec §Behaviour 1–7)
- G1: Inside a CI fix session's repair task, when mutation repair is about to give up with `mutation_repair_no_progress`, run **one** LLM review over every remaining scoring survivor **before** giving up.
- G2: Grant only if every verdict is structurally valid and `equivalent`, nothing else counts against the score, and the session's fresh gate rerun reproduces exactly the reviewed survivors with unchanged source fingerprints.
- G3: Keep the raw evidence and reasoning, show the override, and send exactly one terminal RDC callback.
- G4: The rule and the review step are language-neutral. Java and Python only supply survivor identities.

**Non-goals**
- No change to the Maven plugin, `uta_enforce_core`, PIT or mutmut operators, thresholds, or the local `uta_dev_gate.py`.
- No deterministic equivalence detection. `likely_equivalent` stays prompt ranking only.
- No carry-over to a new CI trigger. No review outside `quality_mode == "ci_incremental"` repair tasks, so `uta run` is excluded.
- No trigger on `attempts_exhausted` or on coverage, compile, or test failures (D1).
- No LLM call in the API/report poll path.
- No feature flag (user decision). Rollback is by reverting the commit.

## 2. High-level design

The review is a new **phase of the neutral generation cycle** (`generation-cycle.yaml`). It runs in the task daemon, like every repair turn. The fix session's existing post-task gate rerun is the fresh **recheck**. In the poll, the session only makes a deterministic comparison.

| # | Piece | Extends | Responsibility |
|---|---|---|---|
| P1 | **Review trigger** in `apply_repair_progress` | `uta/testgen/repair_progress.py` stopping policy | When the decision would be `mutation_repair_no_progress`, the unit is a CI repair, and no review has run yet for this unit, return outcome `review_equivalence` instead of `failed`. |
| P2 | **Review phase** `review_equivalent_mutants` | Existing yaml node kinds: `reconcile_generation_operation`, `agent_turn`, `rehydrate_generation_operation` | Neutral prompt node and neutral interpret node, registered on `cycle_registry`. The turn is durable and resumable through the existing ledger, so a resumed task never pays twice. |
| P3 | **Neutral rule module** `uta/enforcement/equivalent_mutants.py` | `mutation_repair.py`, which holds the shared survivor model | Pure functions: `assess_eligibility`, `render_review_prompt`, `parse_verdicts`, `decide_grant`. |
| P4 | **Survivor extractors** (language-owned) | Java `write_delegated_survivors` command-named PIT folder; Python `diff_survivors` | One extractor per language, `scoring_survivors(gate_result, repo_path)`. It is exposed twice: to the cycle (backend method) and to the session (`CiLanguageHandler`). Both call sites use the same function. |
| P5 | **Review persistence** | `CLASS_TASK_EXTRA_COLUMNS`, `results.py` | `complete_generation` (both languages, through one neutral helper) puts `equivalence_review` into results. It is saved as `class_tasks.equivalence_review_json`. |
| P6 | **Session grant** | `RepairSessionMixin._apply_repair_enforcement_result`, `_report_repair_result_once` | On a failed rerun, read the saved reviews and call `decide_grant` on the fresh result. Granted: session `passed_with_equivalent_mutants`, record `success` + `gateOverride`, one success callback. Otherwise the existing `rerun_failed` path runs unchanged. |

## 3. Relationships and cooperation

Only UTA changes. The external parties are RDC (callback contract unchanged) and the configured agent harness, reached through the existing `agent_turn` node.

```
RDC ─trigger─▶ CI check ─fail─▶ report ─一键修复─▶ fix session ─creates─▶ repair repo task (daemon)
                                                                              │
             measure_mutation / delegated_quality_gate_verify ─▶ apply_repair_progress
                          │ repair (progress)        │ would stop: no_progress (P1)
                     fix_mutation turn          review_equivalent_mutants (P2, one LLM turn)
                                                     │ verdicts → results.equivalence_review (P5)
                                               complete_generation → commit/push (existing)
                                                                              │ COMPLETED
fix session poll ─▶ existing gate rerun (fresh) ─▶ P6 decide_grant (no LLM)
        ├─ granted  ─▶ session passed_with_equivalent_mutants, record success+gateOverride ─▶ RDC success (once)
        └─ otherwise ─▶ existing rerun_failed ─▶ RDC failure (once, unchanged)
```

## 4. Data dependency flow

| Datum | Produced by | Stored in | Consumed by |
|---|---|---|---|
| Gate result at give-up | Java delegated gate (`quality_gate_result`) / Python `measure_mutation` (`verification`) | `phase_results` evidence | P4 extractor |
| `ScoringSurvivors` (keys, `unreviewed_scoring_failures`, source sha256) | P4 | `phase_results.review_equivalent_mutants` | prompt, interpret |
| Review prompt | P3 `render_review_prompt` | existing prompt artifact path (`generation_prompt`) | `agent_turn` |
| `verdicts.json` | LLM | `<repo>/.uta_cache/equivalence/<unit_id>/verdicts.json` (sha256 recorded) | P3 `parse_verdicts` |
| `equivalence_review` {survivors, verdicts, decision, agent session ref, model} | interpret node | results → **new** `class_tasks.equivalence_review_json` | P6 |
| Fresh rerun `QualityGateResult` | existing session rerun | `session.rerunEnforcement`, `record.enforcement_result` (raw) | P4 extractor → P6 |
| Grant | P6 | `session.status`, `session.equivalenceOverride`, `record.gate_override` | report pages, RDC summary |

## 5. Key process flow

```mermaid
sequenceDiagram
  participant M as measure phase (Java delegated / Python)
  participant P as apply_repair_progress
  participant V as review_equivalent_mutants (prompt→turn→interpret)
  participant C as complete_generation
  participant S as fix session poll
  participant R as EnforcementRunner
  participant X as CI protocol (RDC)
  M->>P: repair requested, score flat
  P->>P: decision no_progress & ci_incremental & no prior review
  P->>V: outcome review_equivalence
  V->>V: extract survivors → eligibility (NO_COVERAGE/unreviewed=0, 1..30)
  alt eligible
    V->>V: LLM turn writes verdicts.json; tracked tree unchanged?; parse (fail closed)
  end
  V->>C: equivalence_review {decision all_equivalent | rejected(reason) | ineligible(reason)}
  C-->>S: task COMPLETED, class rows FAIL + equivalence_review_json
  S->>R: rerun gate (existing)
  R-->>S: failed result
  S->>S: decide_grant(fresh result + fresh survivors vs reviews)
  alt granted
    S->>X: success callback (key passed_with_equivalent_mutants)
  else
    S->>X: failure callback (existing rerun_failed)
  end
```

## 6. Key design tradeoffs

1. **An LLM judges equivalence, and code checks structure** ([ADR-016](decisions/ADR-016-llm-judged-session-scoped-equivalent-mutant-override.md)).
2. **The review is a cycle phase in the repair task, before the task gives up. It is not in the session poll** (user decision).[^inpoll]
3. **The session's existing post-task rerun is the recheck** (D4, always a fresh gate). The session adds only a pure comparison.
4. **Source fingerprints, not the commit, bind the review to the rerun.**[^commit]
5. **Record status stays `success`, with a `gate_override` field.** Only the session gets `passed_with_equivalent_mutants` (user decision, finding I3).[^enum]
6. **One extractor per language serves both the cycle and the session.** The same code measures the survivors that are reviewed and the survivors that are rechecked.
7. **The LLM writes a JSON file.** We do not parse chat text.[^json]

[^inpoll]: v1 ran the review inline in `_refresh_repair_sessions`. Rejected: it put up to 90 minutes of LLM turn plus rerun inside HTTP request threads (`service.get`, `recent_records`). A stale-bound race could send both failure and success callbacks. Re-polling a rejected session re-ran the whole review (C1). It also needed a separate lock story. In the cycle, the review reuses durability, resume, cancellation, and turn accounting for free.
[^commit]: The repair task commits and pushes its test changes after the review, so HEAD moves by design. Production sources cannot change, because repair rejects production-code diffs. The sha256 of each mutated source file is therefore the stable binding. Keeping both checks would be redundant (design-review nice-to-have).
[^enum]: A new `CiTaskStatus` value breaks revert (`store.py:149` parses the enum). It also leaks into RDC `data.status` and every status template.
[^json]: Chat text gets truncated and wrapped in prose. A file is validated strictly and hashed for audit. If it is missing or invalid, the review is rejected.

## 7. Capacity, reliability, security

- **Volume and cost.** A review runs at most once per CI repair unit, only on mutation no-progress with 1..30 reviewable survivors. Expect far less than one per day. Each review is **one agent turn** (timeout `ci_equivalent_mutant_review_timeout_seconds`, default 1800 s), run in the daemon worker that already owns the unit.
  - **No extra gate run:** the recheck is the rerun the session already does.
  - **Extractor cost:** reading PIT XML from one invocation folder, or in-memory mutmut evidence; at most 30 sha256 hashes; `git diff --quiet` twice. Negligible.
- **Reliability.**
  - The durable ledger reconciles and rehydrates the turn and interpret results on resume, so there is no double payment.
  - "One review per unit" is keyed on `phase_results` containing `review_equivalent_mutants`.
  - The session callback is keyed `passed_with_equivalent_mutants` through `_report_repair_result_once`.
  - A terminal session is skipped on every later poll (C1).
- **Security and integrity.**
  - The LLM's only accepted output is `verdicts.json`.
  - If the review turn changes a tracked file (`git diff --quiet HEAD` plus index before and after), the review is rejected with `review_modified_workspace`.
  - Untracked build output (`target/`, `mutants/`, `opencode.json`, `.uta_cache`) is ignored (I4).
  - The raw score is always kept and shown.

## 8. Failure-mode handling

| Failure | Detection | Containment / recovery | Blast radius |
|---|---|---|---|
| Extractor can't prove a complete set (no compat folder, totals mismatch, `source != diff`, missing mutmut id, sampled run) | extractor → `None` | `equivalence_review.decision=ineligible(survivors_unproven)` → unit fails as today; log `ci_equivalence_ineligible reason` for hit-rate visibility | None |
| NO_COVERAGE / timeout / suspicious / unknown status present, or >30 | `unreviewed_scoring_failures > 0` / cap | ineligible, reason recorded | None |
| LLM timeout, error, invalid or incomplete JSON, `killable` or `uncertain` verdict | turn result, `parse_verdicts` | `rejected(reason)` → unit fails → normal failure callback | That session |
| LLM edits tracked files | tracked-tree diff | `rejected(review_modified_workspace)`; task delivery still guards prod-code diffs | That session |
| Daemon crash mid-review | durable ledger | resume reconciles (re-runs the turn only if it had no effect, per existing policy) | None |
| Rerun survivors differ, fingerprints changed, tests or coverage fail, other scoring failures appear (including a SURVIVED↔TIMED_OUT flake) | `decide_grant` | not granted → existing `rerun_failed` + failure callback. The flake is an accepted limitation | That session |
| Wrong grant (LLM misjudged) | report shows override badge, reasoning, raw score | next CI run re-gates; revert commit to remove feature | One deployment of that branch |
| Summary refresh overwrites override | guard + test | `_refresh_repair_session_summaries` skips `passed_with_equivalent_mutants` sessions | — |

## 9. Rollout

There is no feature flag (user decision). The feature is live once deployed.
1. Merge to `main`, then deploy to the production node (memory `project_remote_deploy`).
2. Watch `ci_equivalence_ineligible` (reason breakdown), `ci_equivalence_review_rejected`, and `ci_equivalence_granted`. Check the first grant by hand: `verdicts.json` against the source.
3. Rollback: revert the commit and redeploy. Records stay loadable because `CiTaskStatus` is unchanged, and old code ignores the extra `gate_override` field and the extra session keys. The `equivalence_review_json` column is additive.

## 10. Verification plan

- **P3 (pure, table-driven).**
  - Eligibility matrix.
  - `parse_verdicts`: valid, duplicate, unknown, missing, empty-argument → `uncertain`, non-equivalent.
  - `decide_grant`: grant; each rejection reason; the `unreviewed_scoring_failures` rule on both sides (C2).
  - The `text_similarity` fixture accepted as `equivalent`; a single-example argument parsed as `uncertain`.
- **P4 Java.**
  - A fixture compat folder (`.uta_cache/pit-compat/<nonce>/…/mutations.xml`, multi-module) plus a command with `-Duta.pit.compat.evidence=` gives stable keys.
  - A detected/total mismatch returns `None`. So does `source=pit_scoped`.
  - TIMED_OUT, RUN_ERROR, or MEMORY_ERROR on a diff line counts as unreviewed.
- **P4 Python.** Evidence with ids gives keys. A missing id, `sampled=true`, or a count mismatch returns `None`. Timeout and suspicious count as unreviewed.
- **P1 + P2 cycle.**
  - Extend `tests/test_repair_progress_policy.py`: no_progress + ci_incremental → `review_equivalence`; `class_batch`, a second visit, or attempts_exhausted → `failed`.
  - Cycle tests (`test_java_generation_cycle_backend.py`, `test_python_generation_cycle_backend.py`) with a fake harness writing `verdicts.json`: all-equivalent, rejected, and ineligible each complete the unit with `equivalence_review`; resume does not re-run the turn.
- **P5.** `results.py` saves the column; `build_status_payload` exposes it.
- **P6 flow** (`tests/test_ci_repair_ack_flow.py`):
  - granted → one success callback across repeated polls, record `success` + `gate_override`, raw result kept;
  - survivor-set change → existing failure path;
  - no review saved → existing assertions unchanged (including `:164-168`);
  - a new trigger → normal gate.
- **Suites.** `.venv/bin/pytest -q`; `.venv/bin/ruff check uta tests`.
- **Post-deploy.** First real grant: `verdicts.json`, rerun survivors, and `callback_history` show one `state=0` for that session.

## 11. Design-review dispositions (2026-09-14, user)

| Finding | Disposition | Where addressed |
|---|---|---|
| C1 re-poll loop | Fix | Review moved into the cycle (no LLM in poll); terminal session statuses skipped; one-review-per-unit guard (§7, §12.4) |
| C2 unreviewed scoring failures | Fix | `unreviewed_scoring_failures == 0` on reviewed and fresh results; Java unknown statuses fail closed (§12.2) |
| C3 Java PIT source + weak count | Fix | Command-named `.uta_cache/pit-compat` folder; detected **and** total must match; `source == "diff"` (§12.2.1) |
| I1 Python analogue / sampled | Fix | timeout+suspicious rule; sampled → ineligible; `survivors_unproven` logged |
| I2 execution in HTTP poll | Superseded by user direction | Review runs inside the fix session's repair task before giving up |
| I3 new CiTaskStatus value | Fix (field) | record `success` + `gate_override`; session-only status |
| I4 workspace check too strict | Fix | tracked files only; record resolved model |
| I5 lock-held start | Not selected | Moot: no LLM/gate work added to the poll |
| Nice: fingerprints vs commit redundant | Applied | Fingerprints only (HEAD moves by design) |
| Nice: `results.py` storage signature | Applied | Listed in §12.1 |
| Nice: SURVIVED↔TIMED_OUT flake | Applied | Accepted limitation (§8) |

**First-principles check.**
1. **Goal:** a fix session that fails only on mutants the LLM proves equivalent becomes a visible, session-scoped pass with one RDC success callback.
2. **Simplest right solution:** one neutral cycle phase, reusing agent-turn durability; one extractor per language, used twice; one column; a pure grant check in the existing rerun branch. No new status enum, no extra gate run, no flag.
3. **Production proof:** a `ci_equivalence_granted` log line, plus `verdicts.json`, rerun survivors equal to reviewed keys, and one `state=0` callback. The `ci_equivalence_ineligible` reason breakdown shows whether the feature ever fires.
4. **Worst case:** a wrong LLM grant unblocks one branch deployment. Guarded by fail-closed structure, the cap, no unreviewed scoring failures, a fresh-rerun identity match, a visible override, and a normal re-gate on the next CI run.

---

# 12. Part 2 — Detail design (unit-test-agent)

## 12.1 Changes in this repo

| File | Change |
|---|---|
| `uta/testgen/repair_progress.py` | In `apply_repair_progress`: if `failure_reason == "mutation_repair_no_progress"` and `state.quality_mode == "ci_incremental"` and `"review_equivalent_mutants" not in phase_results`, set `phase_outcome = "review_equivalence"` (evidence keeps `failure_reason`). |
| `uta/testgen/graph/generation-cycle.yaml` | New nodes `review_equivalent_mutants_{turn_reconcile,prompt,turn,turn_rehydrate,interpret_reconcile,interpret,result_rehydrate}` (same shape as `fix_mutation_*`). New route `review_equivalence: review_equivalent_mutants_turn_reconcile` from `measure_mutation_run/rehydrate` and `delegated_quality_gate_verify_run/rehydrate`. All interpret outcomes route to `complete_generation_reconcile`. |
| `uta/testgen/graph/cycle.py` (+ selector) | Selectors `mutation_outcome` / `delegated_quality_outcome` pass `review_equivalence` through. Neutral prompt and interpret for phase `review_equivalent_mutants` are dispatched before the backend: the prompt calls `backend.scoring_survivors(state)` then P3. |
| `uta/testgen/backend.py` | Protocol method `scoring_survivors(state) -> Optional[ScoringSurvivors]`, default `None`. |
| `uta/enforcement/equivalent_mutants.py` | **New.** Models + `assess_eligibility`, `render_review_prompt`, `parse_verdicts`, `decide_grant`, `review_payload_for_results(state)`. |
| `uta/language/java/generation/mutation_context.py` | Refactor the compat-folder lookup out of `write_delegated_survivors` into `pit_compat_reports(repo_path, gate_result)` (reuse). |
| `uta/language/java/maven/pitest.py` | `parse_pitest_mutations(xml)` returns all `<mutation>` rows with status and identity fields. |
| `uta/language/java/equivalence.py` | **New.** `java_scoring_survivors(gate_result, repo_path, base_ref)`: compat reports, diff-line filter, totals check, status classification. Used by `JavaGenerationCycleBackend.scoring_survivors` (reads `phase_results.delegated_quality_gate_verify.evidence.quality_gate_result`) and `JavaCiLanguageHandler.scoring_survivors`. |
| `uta/language/python/equivalence.py` | **New.** `python_scoring_survivors(mutation_evidence, repo_path)`. Used by the Python backend (from `measure_mutation` verification) and `PythonCiLanguageHandler` (from CI evidence `mutation`). |
| `uta/enforcement/ci.py` | `CiLanguageHandler.scoring_survivors(*, record, result, repo_path) -> Optional[ScoringSurvivors]`; base returns `None`. |
| `uta/language/java/phases/completion.py`, `uta/language/python/phases.py` | `complete_generation` adds `equivalence_review=review_payload_for_results(state)` to each result (neutral helper, `None` when absent). |
| `uta/tasks/storage/base.py`, `uta/tasks/accounting/results.py` (+ class-task update signature/whitelist) | Column `equivalence_review_json TEXT`; save it. |
| `uta/app/repair/session.py` | In `_apply_repair_enforcement_result`, when the result failed: `_equivalence_grant(record, session, result, repo_task)` → if granted, the grant path, else the existing path. Add `passed_with_equivalent_mutants` to the success skip sets in `_refresh_repair_sessions` and `_refresh_repair_session_summaries`, and to `create_fix_session` `alreadyGreen`. `_report_repair_result_once` accepts an explicit `callback_key`. The repo task is threaded through from both call sites. |
| `uta/shared/ci_models.py` | `CiTaskRecord.gate_override: Optional[Dict[str, Any]] = None`. `CiTaskStatus` unchanged. |
| `uta/shared/config.py` | `ci_equivalent_mutant_review_max: int = 30`, `ci_equivalent_mutant_review_timeout_seconds: int = 1800`. |
| `uta/app/reporting.py`, `uta/app/repair/progress.py`, templates `status.html`, `report.html`, `repair_progress.html`, `recent_jobs.html` | Override badge from `gate_override` or the session status; raw rate and gate; per-mutant reasoning table. The progress rerun stage shows "passed (override)". |
| `uta/testgen/prompts/…/equivalent_mutant_review.md` | **New** template (divergence-region contract, worked example, JSON schema, "do not edit files"). |
| `docs/rdc-api-trigger-usage.md` | Result Semantics + Repair Session. |

## 12.2 Key data structures

```python
# uta/enforcement/equivalent_mutants.py
@dataclass(frozen=True)
class MutantIdentity:
    key: str            # language-built, stable (12.2.1)
    source_path: str    # repo-relative
    line: int
    operator: str
    description: str

@dataclass(frozen=True)
class ScoringSurvivors:
    language: str
    mutants: tuple[MutantIdentity, ...]       # reviewable: SURVIVED only
    unreviewed_scoring_failures: int          # NO_COVERAGE, TIMED_OUT, RUN_ERROR, MEMORY_ERROR,
                                              # unknown status (Java); timeout, suspicious (Python)
    source_fingerprints: Mapping[str, str]    # source_path -> sha256
    mutation_rate: float
    mutation_gate: float

@dataclass(frozen=True)
class MutantVerdict:
    key: str
    verdict: Literal["equivalent", "killable", "uncertain"]
    divergence_region: str
    why_indistinguishable: str
    source_lines: tuple[int, ...]

@dataclass(frozen=True)
class ReviewDecision:          # saved per unit as equivalence_review
    outcome: Literal["all_equivalent", "rejected", "ineligible"]
    reason: str                # ineligible: survivors_unproven | unreviewed_scoring_failures |
                               #   over_cap | no_survivors
                               # rejected: turn_failed | verdicts_invalid | verdicts_incomplete |
                               #   verdict_not_equivalent | review_modified_workspace
    survivors: ScoringSurvivors | None
    verdicts: tuple[MutantVerdict, ...]
    verdicts_sha256: str
    agent_session_ref: Mapping[str, str]
    model_id: str

@dataclass(frozen=True)
class GrantDecision:
    granted: bool
    reason: str   # "" | no_review | review_not_all_equivalent | tests_failed | coverage_failed |
                  # not_mutation_only | survivors_unproven | unreviewed_scoring_failures |
                  # survivor_set_changed | fingerprint_changed
```

`decide_grant(reviews, fresh_result_flags, fresh_survivors)` requires all of these:
- every failing unit has a review with outcome `all_equivalent`;
- tests pass and coverage passes;
- mutation is the only failing gate;
- `fresh_survivors` is not `None` and its `unreviewed_scoring_failures == 0`;
- the fresh key set equals the union of reviewed keys;
- the fresh fingerprints equal the reviewed fingerprints.

### 12.2.1 Identity keys and completeness

**Java**
- **Reports:** `pit_compat_reports(repo, gate_result)`, the folder named by `-Duta.pit.compat.evidence=` (must lie under `.uta_cache/pit-compat`), using `rglob("mutations.xml")`. There is no newest-report search.
- **Scope:** `mutation.source == "diff"`. Rows are filtered to diff lines from `git diff <base_ref>...HEAD -U0` on each `sourceFile`, resolved against the module's `src/main/java`.
- **Totals check:** the filtered `KILLED` count must equal the gate's `detected`, and the filtered scored count must equal the gate's `total`. Scored means every status except NON_VIABLE, and must match the plugin's denominator.
- **Key:** `sha1(sourceFile|mutatedClass|mutatedMethod|methodDescription|lineNumber|mutator|indexes|blocks)`.

**Python**
- **Ineligible (returns `None`)** when `sampled` is true or any survivor lacks an `id`, file, or line.
- **Count check:** `len(diff_survivors)` must equal `survived`.
- **Key:** the mutmut id.
- **Unreviewed:** `timeout + suspicious`.

### 12.2.2 Session and record fields

- `session.status = "passed_with_equivalent_mutants"`.
- `session.equivalenceOverride = {decision, reviews: [{unitId, verdictsPath, verdictsSha256, agentSessionRef, modelId, verdicts}], freshSurvivorKeys, rawMutationRate, mutationGate, grantedAt}`.
- `record.status = success`. `record.enforcement_result` is the **raw** rerun result with `passed: false`.
- `record.gate_override = {"kind": "equivalent_mutants", "sessionId", "rawMutationRate", "mutationGate", "mutants": n}`.
- `record.summary = "Passed with equivalent-mutant override (fix session <id>): raw mutation <r>% < gate <g>%; <n> mutants reviewed equivalent — not killed; next CI run uses the normal gate."`

## 12.3 Intra-repo process flow

**Task daemon (per unit)**
1. The measure phase (Java delegated verify or Python `measure_mutation`) returns `repair`. `apply_repair_progress` would fail on no_progress, and instead returns `review_equivalence` (P1).
2. `review_equivalent_mutants_prompt` (neutral): `backend.scoring_survivors(state)` → `assess_eligibility`.
   - **Ineligible:** save `ReviewDecision(ineligible)` and emit outcome `skip_turn`. The interpret node routes to `complete_generation_reconcile` without a turn.
   - **Eligible:** snapshot `git diff --quiet HEAD` and the index tree, then write the prompt through P3.
3. `review_equivalent_mutants_turn` runs the existing `agent_turn` node, with `timeout_seconds = ci_equivalent_mutant_review_timeout_seconds`.
4. `review_equivalent_mutants_interpret` (neutral): re-check the tracked tree, run `parse_verdicts`, and save a `ReviewDecision`. The phase outcome is `failed` for every decision: the unit has not met the gate, and only the session may grant.
5. `complete_generation` → results `status: FAIL`, `equivalence_review: {...}` → the task delivers the test commit as today.

**API poll (existing structure)**
1. The existing gate rerun returns a failed result. `_apply_repair_enforcement_result` calls `_equivalence_grant`.
2. `_equivalence_grant` reads the saved reviews for every non-PASS class row of `repo_task`, calls `handler.scoring_survivors(record, result, repo_path)`, then `decide_grant`.
3. **Granted:** set the session and record fields (§12.2.2), then `_report_repair_result_once(..., True, summary, callback_key="passed_with_equivalent_mutants")`, then save.
4. **Not granted:** existing `rerun_failed` + failure callback. Log `ci_equivalence_not_granted reason` when any review existed.

## 12.4 Key control flow (guards)

- **Trigger guard (P1):** exact `failure_reason`; `ci_incremental`; no prior `review_equivalent_mutants` in `phase_results`. Any other stop keeps today's `failed`.
- **Eligibility order:** survivors known → `unreviewed_scoring_failures == 0` → `1 ≤ n ≤ cap`.
- **`parse_verdicts` fails closed:**
  - missing or invalid file → `verdicts_invalid`;
  - duplicate or unknown key → `verdicts_invalid`;
  - a missing key → `verdicts_incomplete`;
  - an `equivalent` verdict with an empty `divergence_region` or `why_indistinguishable` → `uncertain`;
  - any non-`equivalent` verdict → `verdict_not_equivalent`.
- **Poll:** `passed_with_equivalent_mutants` is terminal success in `_refresh_repair_sessions`, `_refresh_repair_session_summaries`, and `create_fix_session`. The poll never calls an LLM.
- **Prompt:** requires a divergence-region argument over all inputs and forbids arguing from examples or editing files. It embeds the spec's worked example and the JSON schema.

## 12.5 API and schema changes

- DB: additive `class_tasks.equivalence_review_json TEXT`.
- `CiTaskRecord.gate_override` is an optional field; old readers ignore it. `CiTaskStatus` is unchanged.
- RDC callback body is unchanged (`state=0`, `passed="true"`); only the summary text differs. Trigger response `data.status` is unchanged.
- Cycle spec gains one phase and one outcome, which both languages share.

## 12.6 Repo-local tradeoffs

- Java survivors come from the invocation's PIT XML plus a diff-line filter, not from Maven text (text has counts only). The two-total check makes filter drift fail closed.[^plugin]
- The review phase has no retry. A rejected review lets the unit fail normally, and the user can open another fix session (existing rate limit).
- The prompt and interpret nodes are neutral cycle nodes, not per-backend prompt branches, so neither language re-implements the review.

[^plugin]: We also considered having the plugin emit survivor identities. The spec rejected it (no plugin release).

## 12.7 Capacity, reliability, security (repo-local)

See §7. The review turn is bounded by its timeout inside the unit's existing budget accounting (`turn_accounting`). The poll adds one XML parse or evidence read plus at most 30 hashes on a failed rerun.

## 12.8 Failure-mode handling (repo-local)

Log lines:
- `ci_equivalence_ineligible unit reason`
- `ci_equivalence_review_rejected unit reason`
- `ci_equivalence_review_all_equivalent unit n`
- `ci_equivalence_granted task session raw_rate gate n`
- `ci_equivalence_not_granted task session reason`

## 12.9 Repo-local risks and verification

- **Risk: the Java CI rerun command lacks `-Duta.pit.compat.evidence=`.** Effect: `None`, so never granted. Verify on a real production workspace before calling the feature done.
  - *Status 2026-09-14:* the read-only check on node2 could not run (SSH timed out). In code, `uta/language/java/maven_compat/launcher.py` always appends `-Duta.pit.compat.evidence=<.uta_cache/pit-compat/<nonce>/completion.xml>` to diff-enforcement commands, and the extractor fails closed without it. The production confirmation is still open; it moves to the deploy step (T10).
- **Risk: a Python CI rerun evidence shape differs from cycle `measure_mutation`.** Mitigation: the extractor takes the mutation dict both shapes share. Tested with both fixtures.
- **Risk: a unit resumed after P1 ships, from a checkpoint made before it.** Effect: no review phase in its state, so it follows the normal path.
- **Tests:** see §10.

## 13. Changelog

- 2026-09-14 — Code review and simplify follow-ups:
  - The review trigger lives entirely in `uta/testgen/equivalence_review.prepare_review`. `apply_repair_progress` is back to its original, language-neutral stopping rule, and it knows nothing of the review. This supersedes P1 as written in §2 and §12.1.
  - `review_payload_for_results` moved out of the pure rule module.
  - The grant is guarded: any error falls back to the normal failure.
  - The raw score comes from the fresh rerun, and `gate_override` is cleared by any later non-excused result.
  - Python flags check every target's `testsPass`.
  - Verdict files must be regular, in-repo files of at most 1 MB.

- 2026-09-14 — Implementation (T1–T8). Changes from the plan:
  - The review prompt lives in `uta/testgen/equivalence_review.py`, not in `uta/enforcement`, so the package-dependency check does not see a cycle.
  - Eligibility is decided in `generation_operation`, before routing, because the topology hard-wires prompt → turn.
  - Each language supplies `gate_failure_flags` (tests / coverage / mutation-only), because Java evidence carries no pass flags.
  - Java survivor counting accepts only a reading that reproduces both the detected and total counts the gate printed.

- 2026-09-14 — v1 — initial design.
- 2026-09-14 — Removed feature flag (user). Sections affected: §8, §9, §12.
- 2026-09-14 — v2 — design review applied (§11). The review moved from the session poll into a neutral generation-cycle phase that runs before the repair gives up (user direction). The record keeps `success` + `gate_override` instead of a new `CiTaskStatus`. Java survivors come from the command-named PIT compat folder with a two-total check. The `unreviewed_scoring_failures` rule was added. Commit binding was replaced by fingerprints. The `repair_stop_reason` column was replaced by `equivalence_review_json`. Sections affected: all.
