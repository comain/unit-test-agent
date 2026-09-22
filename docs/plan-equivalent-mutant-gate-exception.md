# Implementation Plan: Session-scoped equivalent-mutant gate exception

Jira: N/A (personal UTA tool work; no release approval release approval)
Spec: `docs/spec-equivalent-mutant-gate-exception.md` (approved)
Design: `docs/design-equivalent-mutant-gate-exception.md` (approved v2)
ADR: `docs/decisions/ADR-016-llm-judged-session-scoped-equivalent-mutant-override.md` (accepted)
Usage: `docs/rdc-api-trigger-usage.md` (existing doc, updated in Task 9)
Task list: `docs/todo-equivalent-mutant-gate-exception.md`
Branch: `main` (repo convention, no feature branch)

## Overview

This plan adds an LLM review step to CI repair tasks.

- **Review phase:** a new neutral generation-cycle phase, `review_equivalent_mutants`, runs when mutation repair would otherwise give up on `mutation_repair_no_progress`. It saves a structured verdict set per unit.
- **Grant:** the fix session's existing post-task gate rerun is the recheck. A deterministic `decide_grant` then marks the session `passed_with_equivalent_mutants`, gives the CI record `success` + `gate_override`, and sends one RDC success callback.
- **Language adapters:** Java and Python supply only survivor identities, through one extractor per language.

## Architecture Decisions (from design v2)

- An LLM judges; code checks structure and fails closed (ADR-016).
- The review runs as a cycle phase in the task daemon, never in the API poll.
- The recheck is the existing session rerun. The binding is source fingerprints, not the commit.
- `CiTaskStatus` stays unchanged; a `gate_override` field is added. There is no feature flag.
- Java survivors come from the command-named `.uta_cache/pit-compat/<nonce>` PIT reports. Both totals must match and the source must be `diff`.
- A grant requires `unreviewed_scoring_failures == 0` on both the reviewed result and the fresh result.

## Dependency Graph

```
T1 neutral rule module (P3) ──┬── T2 Java extractor (P4) ──┐
                              ├── T3 Python extractor (P4) ─┤
                              └── T4 review persistence (P5)┤
                                                            ├── T5 cycle phase + trigger, Python path (P1/P2)
                                                            │        └── T6 Java delegated path in cycle
                                                            └── T7 session grant + callback (P6)
                                                                     └── T8 report/progress visibility
T9 docs ── after T7/T8;  T10 production verification ── last
```

The riskiest unknown is whether Java can rebuild the survivor set from a real CI rerun, so T2 runs early. It includes a read-only check of a real production workspace.

## Task List

### Phase 1: Foundations (pure and adapter code, no behaviour change)
- [ ] **T1** Neutral rule module `uta/enforcement/equivalent_mutants.py` (S)
- [ ] **T2** Java survivor extractor, plus a production command/report check (M)
- [ ] **T3** Python survivor extractor (S)

### Checkpoint A (after T1–T3)
- [ ] `.venv/bin/pytest -q tests/test_equivalent_mutants.py tests/test_java_equivalence_extractor.py tests/test_python_equivalence_extractor.py` passes
- [ ] `.venv/bin/pytest -q` full suite is green (nothing is wired yet, so there should be no regressions)
- [ ] The T2 production check confirms that `-Duta.pit.compat.evidence=` and the compat reports exist on a real Java CI rerun. If not, **stop and review with the user**.

### Phase 2: Review inside the repair task
- [ ] **T4** Save `equivalence_review` through completion to `class_tasks` (M)
- [ ] **T5** Trigger + `review_equivalent_mutants` phase, end to end on the Python path (M)
- [ ] **T6** Java delegated-gate path through the review phase (S)

### Checkpoint B (after T4–T6)
- [ ] Cycle tests pass for both languages with a fake harness: all-equivalent, rejected, and ineligible each complete the unit with a saved review; a resumed run does not re-run the turn
- [ ] Non-CI (`class_batch`) and attempts-exhausted units still end `failed` with no review
- [ ] Full suite green

### Phase 3: Session grant and visibility
- [ ] **T7** Session `decide_grant`, `gate_override`, and one success callback (M)
- [ ] **T8** Report, status, recent-jobs, and progress pages show the override (M)

### Checkpoint C (after T7–T8)
- [ ] `tests/test_ci_repair_ack_flow.py` (including the unchanged `:164-168` failure-callback assertion), `tests/test_api_trigger_fix_sessions.py`, `tests/test_api_trigger_report.py`, and `tests/test_fix_session_progress_sse.py` pass
- [ ] Full suite green; `ruff check uta tests` clean
- [ ] Review the diff with the user before docs and deploy

### Phase 4: Docs and production proof
- [ ] **T9** Usage doc, spec/design status, and memory (XS)
- [ ] **T10** Deploy and verify on production (manual)

### Checkpoint D (complete)
- [ ] Every row of the Requirement Coverage table below is satisfied
- [ ] The first real eligible session has been inspected, or its absence is explained by the `ci_equivalence_ineligible` reason breakdown

## Task Details

### T1: Neutral rule module
**Description:** Create the models (`MutantIdentity`, `ScoringSurvivors`, `MutantVerdict`, `ReviewDecision`, `GrantDecision`) and the pure functions `assess_eligibility`, `render_review_prompt`, `parse_verdicts`, `decide_grant`, and `review_payload_for_results`. Add config `ci_equivalent_mutant_review_max=30` and `ci_equivalent_mutant_review_timeout_seconds=1800`, plus the prompt template.

**Acceptance criteria:**
- [ ] Eligibility follows design §12.4: survivors unknown → `survivors_unproven`; unreviewed > 0; cap (30); zero survivors.
- [ ] `parse_verdicts` fails closed on every case in §12.4. The `text_similarity` fixture parses as `equivalent`; a single-example argument with an empty divergence region parses as `uncertain`.
- [ ] `decide_grant` grants only when all §12.2 conditions hold, and returns each specific rejection reason otherwise.

**Verification:** `.venv/bin/pytest -q tests/test_equivalent_mutants.py`; `.venv/bin/ruff check uta/enforcement/equivalent_mutants.py`

**Dependencies:** None

**Files:** `uta/enforcement/equivalent_mutants.py`, `uta/shared/config.py`, `uta/testgen/prompts/<existing prompt dir>/equivalent_mutant_review.md`, `tests/test_equivalent_mutants.py`

**Scope:** S

### T2: Java survivor extractor
**Description:**
- Move the compat-folder lookup out of `write_delegated_survivors` into `pit_compat_reports(repo_path, gate_result)`, and reuse it there.
- Add `parse_pitest_mutations(xml)` covering all statuses.
- Add `uta/language/java/equivalence.py::java_scoring_survivors(gate_result, repo_path, base_ref)`: diff-line filter, detected-and-total check, `source == "diff"`, and status classification (SURVIVED is reviewable; NO_COVERAGE, TIMED_OUT, RUN_ERROR, MEMORY_ERROR, or an unknown status is unreviewed; NON_VIABLE is excluded).
- Add `CiLanguageHandler.scoring_survivors` (base returns `None`) and the Java handler implementation.

**Acceptance criteria:**
- [ ] A multi-module fixture compat folder plus a command gives stable keys (sha1 identity) and source sha256 values.
- [ ] A detected or total mismatch returns `None`; so do `source=pit_scoped`, a missing `-Duta.pit.compat.evidence=`, and a path outside `.uta_cache/pit-compat`.
- [ ] `write_delegated_survivors` behaviour is unchanged (its existing tests pass).
- [ ] **Production check (read-only):** on the production node, find a recent Java CI rerun's `record.enforcement_result.command` and confirm the `-Duta.pit.compat.evidence=` arg and the `mutations.xml` files exist. Record the finding in design §12.9.

**Verification:** `.venv/bin/pytest -q tests/test_java_equivalence_extractor.py tests/test_java_generation_cycle_backend.py`; production check notes

**Dependencies:** T1

**Files:** `uta/language/java/generation/mutation_context.py`, `uta/language/java/maven/pitest.py`, `uta/language/java/equivalence.py`, `uta/enforcement/ci.py`, `uta/language/java/ci.py`, `tests/test_java_equivalence_extractor.py`

**Scope:** M (6 files including the test; the two `ci.py` edits are a few lines each)

### T3: Python survivor extractor
**Description:** Add `uta/language/python/equivalence.py::python_scoring_survivors(mutation, repo_path)` and the `PythonCiLanguageHandler.scoring_survivors` implementation.

**Acceptance criteria:**
- [ ] `diff_survivors` with ids gives keys equal to the mutmut ids; `timeout + suspicious` is counted as unreviewed.
- [ ] Returns `None` when `sampled` is true, when any survivor is missing an id, file, or line, or when `len(diff_survivors) != survived`.
- [ ] Works on both the CI evidence `mutation` shape and the cycle `measure_mutation` `verification.mutation` shape (fixtures for both).

**Verification:** `.venv/bin/pytest -q tests/test_python_equivalence_extractor.py`

**Dependencies:** T1

**Files:** `uta/language/python/equivalence.py`, `uta/language/python/ci.py`, `tests/test_python_equivalence_extractor.py`

**Scope:** S

### T4: Save the review through completion
**Description:** Add a `class_tasks.equivalence_review_json` column. The neutral `review_payload_for_results(state)` is called from Java `complete_generation` (both the delegated and per-class branches) and from Python `complete_generation`. `results.py` saves the value (update the storage signature and allowlist), and `build_status_payload` exposes it as parsed JSON.

**Acceptance criteria:**
- [ ] With `phase_results.review_equivalent_mutants` present, every result dict carries `equivalence_review`, and the row stores it. When the phase is absent, the value is `None`.
- [ ] A DB without the column migrates on init (existing `_ensure_columns`).

**Verification:** `.venv/bin/pytest -q tests/test_java_generation_cycle_backend.py tests/test_python_generation_cycle_backend.py -k complete`, plus a new storage test

**Dependencies:** T1

**Files:** `uta/tasks/storage/base.py`, `uta/tasks/accounting/results.py` (+ class-task update method), `uta/language/java/phases/completion.py`, `uta/language/python/phases.py`, `uta/tasks/render.py`

**Scope:** M

### T5: Trigger and review phase, Python path end to end
**Description:**
- **P1:** in `apply_repair_progress`, return `review_equivalence` when the conditions hold (`mutation_repair_no_progress`, `ci_incremental`, no prior review).
- **P2:** add the yaml nodes `review_equivalent_mutants_*` and the routes from `measure_mutation_run/rehydrate`.
- Register neutral prompt and interpret handling for the phase: prompt calls `backend.scoring_survivors(state)` → eligibility → render, or `skip_turn`; interpret checks the tracked-tree snapshot, runs `parse_verdicts`, and saves the `ReviewDecision`; the outcome is always `failed` → completion.
- Add `TestGenerationBackend.scoring_survivors` with a default of `None`, and the Python backend implementation.
- Add log lines `ci_equivalence_ineligible`, `ci_equivalence_review_rejected`, and `ci_equivalence_review_all_equivalent`.

**Acceptance criteria:**
- [ ] Policy: no_progress + ci_incremental + no prior review → `review_equivalence`. A second visit, `class_batch`, or attempts_exhausted → `failed`, as today.
- [ ] Python cycle with a fake harness writing `verdicts.json`: all-equivalent, rejected (a killable verdict, or an edit to a tracked file), and ineligible (sampled) each complete with a saved review. Untracked `target/`/`mutants/`/`opencode.json` does not reject the review.
- [ ] Resume after the turn completed rehydrates without calling the harness again.

**Verification:** `.venv/bin/pytest -q tests/test_repair_progress_policy.py tests/test_python_generation_cycle_backend.py tests/test_repair_invalidates_verification.py`

**Dependencies:** T1, T3, T4

**Files:** `uta/testgen/repair_progress.py`, `uta/testgen/graph/generation-cycle.yaml`, `uta/testgen/graph/cycle.py`, `uta/testgen/backend.py` (+ Python backend method in `uta/language/python/generation_backend.py`)

**Scope:** M (5 files)

### T6: Java delegated path through the review phase
**Description:** Add routes `review_equivalence` from `delegated_quality_gate_verify_run/rehydrate`. `JavaGenerationCycleBackend.scoring_survivors` reads `phase_results.delegated_quality_gate_verify.evidence.quality_gate_result` together with `base_ref` from state.

**Acceptance criteria:**
- [ ] A Java delegated cycle test with the fixture compat folder and a fake harness produces an all-equivalent review saved in results. Missing reports lead to an ineligible review.
- [ ] Existing delegated-gate tests are unchanged.

**Verification:** `.venv/bin/pytest -q tests/test_java_generation_cycle_backend.py tests/test_java_repair_respects_enforcer_filter.py`

**Dependencies:** T2, T5

**Files:** `uta/testgen/graph/generation-cycle.yaml`, `uta/language/java/generation_backend.py`, `tests/test_java_generation_cycle_backend.py`

**Scope:** S

### T7: Session grant
**Description:**
- In `_apply_repair_enforcement_result`, when the result failed, `_equivalence_grant(record, session, result, repo_task)` reads the non-PASS class rows' reviews, calls `handler.scoring_survivors`, then `decide_grant`.
- **Granted:** set the fields from design §12.2.2 and call `_report_repair_result_once(..., callback_key="passed_with_equivalent_mutants")`.
- **Not granted:** the existing path, plus a `ci_equivalence_not_granted` log line.
- Add `CiTaskRecord.gate_override`.
- Skip `passed_with_equivalent_mutants` as terminal success in `_refresh_repair_sessions`, `_refresh_repair_session_summaries`, and `create_fix_session`.
- Thread `repo_task` through both call sites.

**Acceptance criteria:**
- [ ] Granted flow: exactly one success callback across 3+ polls. Record `success` + `gate_override`; `enforcement_result.passed == false` is kept. Session status and `equivalenceOverride` are saved. A compact summary refresh does not overwrite the grant.
- [ ] Survivor-set change, fingerprint change, a new timeout, or tests or coverage failing → the existing `rerun_failed` path with one failure callback. No saved review → the legacy assertions pass unchanged.
- [ ] A new CI trigger on the same branch runs the normal gate (no inherited override).

**Verification:** `.venv/bin/pytest -q tests/test_ci_repair_ack_flow.py tests/test_api_trigger_fix_sessions.py tests/test_api_trigger_rdc_callback.py`

**Dependencies:** T2, T3, T4

**Files:** `uta/app/repair/session.py`, `uta/shared/ci_models.py`, `uta/enforcement/ci.py` (signature only, if needed), `tests/test_ci_repair_ack_flow.py`

**Scope:** M

### T8: Visibility
**Description:**
- An override badge on `status.html`, `report.html`, `repair_progress.html`, and `recent_jobs.html`, driven by `gate_override` / the session status. The report shows the raw mutation rate, the gate, and a per-mutant reasoning table from `equivalenceOverride.reviews`.
- `progress.py` renders the rerun stage as "passed (override)", and adds an "Equivalent-mutant review" stage from task events or rows.
- `reporting.py` detail/summary payloads include `gateOverride`.

**Acceptance criteria:**
- [ ] A report for a granted record shows the badge, the raw rate below the gate, and every mutant with its verdict reasoning. It is not rendered as a plain green pass.
- [ ] The progress page shows the review stage and does not show the rerun stage as "failed".
- [ ] Records without the field render exactly as before (existing report tests pass).

**Verification:** `.venv/bin/pytest -q tests/test_api_trigger_report.py tests/test_fix_session_progress_sse.py`; manual render of a fixture record through the local API

**Dependencies:** T7

**Files:** `uta/app/reporting.py`, `uta/app/repair/progress.py`, `uta/app/templates/{status,report,repair_progress,recent_jobs}.html`

**Scope:** M (templates are small edits)

### T9: Docs
**Description:**
- Update `docs/rdc-api-trigger-usage.md` (Result Semantics: the override meaning, the raw score kept, next CI run re-gates; Repair Session: the review stage and ineligible reasons).
- Set the spec "Usage doc" line to done.
- Update the design changelog and §12.9 with the T2 production finding.
- Update the memory note.
- No `uta_dev_gate.py` sync: the plugin gate semantics are unchanged (AGENTS.md rule not triggered), and the spec/design record why.

**Acceptance criteria:**
- [ ] The docs describe the behaviour that was actually built, with no stale v1 wording (no flag, no poll-time review, no new status enum).

**Verification:** `grep -n "ci_equivalent_mutant_review_enabled\|passed_with_equivalent_mutants" docs/` shows only intended references

**Dependencies:** T7, T8

**Files:** `docs/rdc-api-trigger-usage.md`, `docs/spec-equivalent-mutant-gate-exception.md`, `docs/design-equivalent-mutant-gate-exception.md`

**Scope:** XS

### T10: Production verification
**Description:** Commit on `main` (after user review at Checkpoint C) and deploy to the production node per memory `project_remote_deploy`. Confirm the column migration, then watch the `ci_equivalence_*` logs.

**Acceptance criteria:**
- [ ] The service is healthy after deploy, and new repair tasks have the `equivalence_review_json` column.
- [ ] For the first eligible session: `verdicts.json` has been reviewed against the source, the rerun survivor keys equal the reviewed keys, and `callback_history` has exactly one `state=0`. If there is no eligible session within the observation window, report the `ci_equivalence_ineligible` reason breakdown to the user.

**Verification:** manual, using the `log-query` skill and the report page

**Dependencies:** T9

**Files:** none

**Scope:** manual

## Requirement Coverage

| Source | Requirement / criterion / decision | Task(s) | Notes |
|---|---|---|---|
| spec §B1 / D1 | Trigger only on `mutation_repair_no_progress`, only in CI repair, not attempts_exhausted | T5, T6 | Policy tests |
| spec §B2 | Review every remaining scoring mutant; verdict equivalent/killable/uncertain; all must be equivalent | T1, T5, T6 | |
| spec §B2 / D2 | NO_COVERAGE blocks; with C2, any other scoring failure blocks | T1, T2, T3, T7 | Checked on both reviewed and fresh results |
| spec §B2 / D3 | Cap 30, configurable | T1 | |
| spec §B2 | Verdict contract (divergence region, all inputs, source lines); single example → uncertain; worked example fixture | T1 | Prompt + parser |
| spec §B2 | Malformed, truncated, missing, or unknown verdict → fail | T1, T5 | |
| spec §B3 / D4 | Always re-run the gate; tests and coverage pass; mutation-only; identical survivor set; fingerprints unchanged | T7 (existing rerun + `decide_grant`), T1 | Commit binding replaced by fingerprints (design v2) |
| spec §B4 | Session `passed_with_equivalent_mutants`; record visible override; raw score, reasoning, and session ref kept; one idempotent success callback naming the override | T7, T8 | Record = `success` + `gate_override` (design-review I3) |
| spec §B5 | Decision stored with the fix session only; new CI run uses the normal gate | T7 | Test for a new trigger |
| spec §B6 | Neutral rule; adapters supply identities only | T1, T2, T3, T5 | No language branching in core |
| spec §B7 | Report/progress pages show the distinct status, raw score, and reasoning; not plain green | T8 | |
| spec §Success 6 / design §10 | Focused + full suites pass | Checkpoints A–C | |
| design P4 / C3 | Java command-named compat reports, two-total check, `source == diff` | T2 | + production check |
| design I1 | Python sampled → ineligible; timeout/suspicious unreviewed; `survivors_unproven` logged | T3, T5 | |
| design I4 | Workspace check on tracked files only; record resolved model | T5 | |
| design C1 | No LLM in poll; terminal statuses skipped; one review per unit | T5, T7 | |
| design §12.5 | Additive `class_tasks.equivalence_review_json`; `CiTaskRecord.gate_override`; `CiTaskStatus` unchanged; RDC body unchanged | T4, T7 | |
| design §9 | No flag; rollback = revert; records loadable after revert | T7 (no enum change), T10 | |
| design §8 / §12.8 | Log lines for ineligible, rejected, all-equivalent, granted, not-granted | T5, T7 | |
| design §10 post-deploy | Production proof signal | T10 | |
| usage | `docs/rdc-api-trigger-usage.md` update | T9 | |
| AGENTS.md sync rule | `uta_dev_gate.py` not changed; rationale recorded | T9 | N/A by design |
| UTA: Jira, Java Alibaba guidelines | N/A | — | Non-UTA tool work; changed code is Python only |

There are no orphan tasks: every task maps to at least one row above.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Java CI rerun lacks the compat evidence arg, so Java can never be granted | High (feature inert for Java) | T2 production check before Phase 2; stop and ask if absent |
| Yaml selector / reconciliation rejects a new outcome or phase | Med | T5 copies the `fix_mutation_*` node shape exactly; resume test |
| Python CI evidence and cycle evidence shapes differ | Med | T3 fixtures for both shapes |
| Deferred-review state leaks into non-CI `uta run` | Med | `ci_incremental` guard + test |
| Summary refresh overwrites the grant | Med | T7 explicit guard + test |
| LLM misjudges equivalence | Med (one branch deploy) | Fail-closed structure, cap, visible reasoning, next CI re-gates, revert |

## Open Questions

None. All decisions are recorded in the spec (D1–D4, changelog) and the design (§11).
