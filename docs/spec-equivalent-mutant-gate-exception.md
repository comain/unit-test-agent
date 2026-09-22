# Spec: Session-scoped equivalent-mutant gate exception

Status: approved
Approved-by: user 2026-09-14
Jira: N/A — personal UTA tool work (no Jira, see memory `feedback_non_blf_project`)
Design doc: `docs/design-equivalent-mutant-gate-exception.md` (v2)
Usage doc: `docs/rdc-api-trigger-usage.md` — "Equivalent-Mutant Override" section

## Objective

Some source code is written so that no test can kill a mutant: the mutated
program behaves exactly like the original (an *equivalent mutant*). Examples
include a boundary change on a value that can never reach the boundary, or a
removed call with no observable effect. When a CI report fails only because of
such mutants, the one-click repair session can never make it green. The
session spends every repair turn, stops on no-progress, and blocks RDC even
though the tests are as good as the code allows.

Deterministic rules cannot detect equivalence reliably. The existing
`likely_equivalent` heuristics (`uta/language/java/scoring/mutation_roi.py`,
Python `mutation_context.py`) only affect prompt ranking and must stay that
way. So judging equivalence goes to an **LLM review session**. Deterministic
code keeps ownership of everything that can be checked mechanically:
completeness, identity, fingerprints, tests, coverage, and idempotency.

The result is an **intentional, visible gate override** that lasts for one fix
session. It does **not** claim the mutants were killed. The next independent CI
run uses the normal gate and may fail again; we accept that. No Maven plugin
release is needed.

**User:** a developer whose RDC `单元测试` gate is blocked after one-click
repair ran out of progress on mutation.

## Behaviour (what, not how)

1. **Trigger — terminal no-progress only.** A fix session reaches its
   terminal failure because mutation repair stopped with
   `mutation_repair_no_progress`, and the final authoritative rerun fails
   **only** the mutation gate. Tests must be green and diff coverage must
   pass. `mutation_repair_attempts_exhausted` and every other terminal
   failure keep today's behaviour and do not trigger a review.
2. **Review every remaining scoring mutant.** The review covers every mutant
   the gate counts against the score. Java NO_COVERAGE mutants count toward
   the score but **cannot** be ruled equivalent: an uncovered mutant is a
   coverage gap, so if any NO_COVERAGE survivor remains, no review runs and
   the session stays failed. If more than `ci_equivalent_mutant_review_max`
   scoring survivors remain (configurable, default **30**), no review runs and
   the session stays failed, recording the reason. An LLM review session
   gives each mutant one verdict: `equivalent`, `killable`, or `uncertain`. Each verdict carries source-backed reasoning: the original
   behaviour, the mutated behaviour, and why no input can tell them apart.
   - The exception applies **only if every mutant is `equivalent`**.
   - Any `killable` or `uncertain` verdict, a missing verdict, a verdict for an
     unknown identity, or output that is malformed or truncated keeps the
     session failed, as today.
   **Verdict contract.** A single passing example is not proof. For each
   mutant, an `equivalent` verdict must state all of the following:
   - (a) the **divergence region**: the complete set of inputs or states where
     the mutated construct behaves differently from the original (for
     example, where the mutated predicate takes a different truth value);
   - (b) why **every** input in that region still produces the same
     observable result: the return value, raised exceptions, side effects,
     and state the tests can see;
   - (c) the source lines that back the argument.
   If a verdict lacks (a) or (b), or argues from one or a few example inputs,
   it is parsed as `uncertain`. Structural checks (fields present, identity
   known) are deterministic. Whether the argument is correct is up to the LLM.

   *Worked example (Python, `or` → `and`):*
   ```python
   def text_similarity(left: str, right: str) -> float:
       a, b = normalize_text(left), normalize_text(right)
       if not a or not b:          # mutant: `not a and not b`
           return 0.0
       sequence = SequenceMatcher(None, a, b).ratio()
       grams_a = {a[i:i + 2] for i in range(max(1, len(a) - 1))}
       grams_b = {b[i:i + 2] for i in range(max(1, len(b) - 1))}
       jaccard = len(grams_a & grams_b) / len(grams_a | grams_b) if grams_a | grams_b else 0.0
       return max(sequence, jaccard)
   ```
   The repair's test `text_similarity("", "ab") == 0.0` is **not** enough on
   its own, because it checks one input. An acceptable argument goes like
   this:
   - The divergence region is *exactly one* of `a`, `b` is empty. When both
     are empty or both are non-empty, the two predicates agree.
   - Take `a == ""` and `b` non-empty. `SequenceMatcher.ratio()` is
     `2*0/len(b) = 0.0`. `grams_a == {""}`, and every element of `grams_b` is
     non-empty, so the intersection is empty while the union is non-empty.
     Jaccard is `0.0`, and the function returns `0.0`.
   - The case with `a` and `b` swapped is symmetric.
   - No exception or side effect differs, so the mutant is equivalent.

   This example becomes a fixture for the review prompt and for the
   verdict-parsing tests.
3. **Recheck before granting — always re-run the gate.** After an
   all-equivalent review, run the authoritative enforcement gate again (Maven
   or mutmut) on the fix-session workspace. Then deterministically confirm on
   **that fresh result**:
   - Tests pass and diff coverage passes.
   - Mutation is still the only failing gate.
   - The fresh result's scoring-survivor identity set **exactly equals** the
     reviewed set, with nothing extra and nothing missing.
   - The fingerprint (content hash) of every mutated source file equals the
     fingerprint recorded at review time, and the fresh run evaluated the same
     commit as the review.
   If any check fails, or the recheck gate cannot run, the session stays
   failed and records the reason. The fresh result becomes the stored
   enforcement evidence.
4. **Grant.** Mark the fix session and its originating CI report as
   `passed_with_equivalent_mutants`. Keep the raw mutation score, gate
   threshold, reviewed identities, fingerprints, per-mutant reasoning, and the
   review session reference. Send **one** idempotent RDC success callback. Its
   summary must say that the equivalent-mutant override was applied and must
   include the raw score.
5. **Session-scoped persistence.** Store the decision on that fix session
   only. A new CI trigger for the branch uses the normal gate and does not
   inherit the decision. A later fix session on a later failed report may run
   its own review.
6. **Neutral ownership.** The rule (trigger, completeness, identity equality,
   fingerprint check, and grant) lives in language-neutral workflow code. The
   Java and Python CI adapters only provide the scoring-survivor identities and
   source fingerprints for a gate result. Core code does not branch on
   language.
7. **Visibility.** The report page and fix-session progress page show the
   distinct status with the raw score and the reasoning. They must not show it
   as a plain "green" result.

## Scope discovery

| Candidate | Found at | Decision | Reason |
| --- | --- | --- | --- |
| Fix-session state machine and terminal reporting | `uta/app/repair/session.py` | **In** | Owns terminal outcomes and `_report_repair_result_once`. The exception is a new terminal transition here, with its own callback key. |
| Fix-session model helpers | `uta/shared/fix_sessions.py` | **In** | Session dict fields and the eligibility rule (`can_create_fix_session` requires `failed`). |
| CI record status | `uta/shared/ci_models.py` `CiTaskStatus` | **In** | Needs a distinct `passed_with_equivalent_mutants` value, not `success`. |
| CI language handler port | `uta/enforcement/ci.py` `CiLanguageHandler` / `BaseCiLanguageHandler` | **In** | New adapter hook that supplies scoring-survivor identities and fingerprints. The default returns none, which means no exception. |
| Java CI adapter and PIT parsing | `uta/language/java/ci.py`, `uta/language/java/maven/pitest.py` | **In** | Survivor identity comes from `mutations.xml` (class, method, line, mutator, description, and index if present). |
| Python CI adapter and mutmut evidence | `uta/language/python/ci.py`, `uta/language/python/enforcement.py` (`diff_survivors`) | **In** | Survivor identity comes from `diff_survivors` (file, line, and mutant name/diff). |
| No-progress signal | `uta/testgen/repair_progress.py` (`<kind>_repair_no_progress`) | **In (read only)** | The trigger reads this reason. The progress policy itself does not change. |
| Shared mutation-repair model | `uta/enforcement/mutation_repair.py` | **In (reuse)** | `MutationRepairGroup.survivors` is the shared survivor representation to reuse for review input. |
| RDC protocol callback | `uta/app/protocols/rdc.py` | **In (minimal)** | Sends `passed=true` with the override summary. The callback body stays in the existing contract. |
| Report and progress pages | `uta/app/reporting.py`, `uta/app/repair/progress.py` | **In** | Show the distinct status, raw score, and reasoning. |
| Usage doc | `docs/rdc-api-trigger-usage.md` | **In** | Result Semantics and repair session sections. |
| `fd_wmonitor_default_store` auto-pass | `uta/app/protocols/rdc.py:426` | Out | Unrelated per-app override. It is a precedent for "keep the real result, override only the callback" but is not changed. |
| `likely_equivalent` heuristics | `uta/language/java/scoring/mutation_roi.py`, Python `mutation_context.py` | Out | Prompt ranking only. It must never decide the gate, because it is not reliable. |
| UTA Maven plugin / `uta_enforce_core` gate | external | Out | The user explicitly wants no plugin release. Gate verdicts stay raw. |
| Local dev hard gate | `plugins/dev-skills/scripts/uta_dev_gate.py` | Out | The override is a UTA CI-server, fix-session decision. Local enforcement must stay strict. AGENTS.md sync is not triggered because gate semantics in the plugin do not change. |
| Delegated quality prompt | `uta/language/java/phases/delegated_quality.py` | Out | It already tells repair turns never to *label* groups equivalent. The review is a separate, post-terminal session. |
| `uta run` CLI generation cycle | `uta/testgen/graph` | Out | It has no CI report or RDC callback, so there is nothing to override. |

## Tech Stack

Python 3.12, Pydantic models (`CiTaskRecord`, `QualityGateResult`), the
existing OpenCode agent-turn infrastructure for the LLM review session, and
pytest.

## Commands

- Focused: `.venv/bin/pytest -q tests/test_ci_repair_ack_flow.py tests/test_api_trigger_fix_sessions.py tests/test_api_trigger_rdc_callback.py tests/test_equivalent_mutant_review.py`
- Adapters: `.venv/bin/pytest -q tests/test_java_generation_cycle_backend.py tests/test_python_mutation_repair_context.py`
- Full: `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check uta tests`

## Project Structure

- `uta/enforcement/` — neutral rule and data model (new module, e.g. `equivalent_mutants.py`)
- `uta/app/repair/session.py` — terminal transition wiring
- `uta/language/{java,python}/ci.py` — survivor-identity and fingerprint providers
- `tests/test_equivalent_mutant_review.py` — new rule tests; existing repair/callback suites are extended
- `docs/` — spec, design, ADR (`docs/decisions/ADR-016-…`), usage update

## Code Style

Match the existing modules: frozen dataclasses for neutral models, protocol
hooks with inert defaults on `BaseCiLanguageHandler`, docstrings that explain
*why*, and session state as camelCase dict keys.

```python
@dataclass(frozen=True)
class MutantIdentity:
    """Language-neutral key for one scoring survivor; adapters build it."""
    source_path: str
    line: int
    operator: str
    detail: str
    source_sha256: str
```

## Testing Strategy (TDD)

Write the rule tests first, as pure unit tests with no LLM:
- All `equivalent` + identities equal + fingerprints equal + tests/coverage green → grant.
- Any `killable` / `uncertain` / missing / extra / malformed verdict → no grant.
- Rerun survivor set differs from reviewed set (added, removed, or changed) → no grant.
- Source fingerprint changed → no grant.
- Coverage or tests failing, or a non-mutation terminal failure → review never triggered.
- Callback sent exactly once across repeated `_refresh_repair_sessions` polls. Status is `passed_with_equivalent_mutants`, and the raw score is kept.
- A new CI trigger on the same branch uses the normal gate.
- The Java and Python adapters each produce stable identities from fixture PIT XML / mutmut evidence.
- The LLM review session is faked at its port. One smoke check on a real repo is optional, after merge.

## Boundaries

- **Always:** keep raw scores and evidence; show the override; key the callback idempotently; fail closed on any doubt.
- **Ask first:** extending the trigger beyond mutation no-progress (for example attempts-exhausted or coverage); letting the decision survive into a new CI run; changing the callback contract fields.
- **Never:** change gate thresholds, PIT/mutmut operators, or exclusions; mark mutants as killed; grant for mutants not in the reviewed set; let heuristics grant without an LLM verdict; change the Maven plugin or local dev gate.

## Success Criteria

1. A failed CI report whose fix session ends with mutation no-progress, with only equivalent survivors left, ends as `passed_with_equivalent_mutants`. RDC receives exactly one success callback that names the override and the raw score.
2. One non-equivalent or uncertain verdict, or any identity or fingerprint mismatch, leaves the session failed and records the reason.
3. The report shows the per-mutant reasoning and the raw score.
4. A new CI trigger afterwards runs the normal gate.
5. No core module imports Java or Python specifics; the adapter hook returns nothing by default.
6. The focused suites and the full suite pass.

## Assumptions

1. The review runs as a new LLM agent turn after the terminal failure, within the same fix session, using the repair task's workspace. It is not a human review.
2. "Scoring mutants" means mutants counted as not killed in the gate's rate: Java SURVIVED + NO_COVERAGE (NO_COVERAGE blocks), and Python survived within the gate scope (changed lines when scoped).
3. The RDC callback uses the existing `passed=true` body. The override is shown through the summary text, not a new wire field.

## Decisions (2026-09-14, user)

- **D1 — Trigger:** `mutation_repair_no_progress` only. `attempts_exhausted` does not trigger a review.
- **D2 — NO_COVERAGE:** not reviewable. Any NO_COVERAGE survivor blocks the exception.
- **D3 — Review cap:** configurable, default 30 scoring survivors. Above the cap, no review runs and the session fails.
- **D4 — Recheck:** always re-run the full enforcement gate after an all-equivalent review, then compare identities and fingerprints against that fresh result.

## Open Questions

None blocking. Design will settle how the review turn is launched, its prompt and output schema, and the concrete identity keys per language.

## Changelog

- 2026-09-14 — Design-review clarifications (user):
  - The review runs **inside the fix session's repair task, before it gives up** on `mutation_repair_no_progress`, and never from the report poll. The session's existing post-task gate rerun is the fresh recheck (§Behaviour 3).
  - §Behaviour 4 is refined: the **fix session** gets status `passed_with_equivalent_mutants`. The originating CI record keeps status `success`, with a visible `gate_override` field and the raw evidence, so it can be read after a revert.
  - The recheck binds to the source file fingerprints (HEAD moves when the repair commits its tests).
  - A grant also requires that no other failure counting against the mutation score remains: Java NO_COVERAGE, TIMED_OUT, or RUN_ERROR; Python timeout or suspicious.
  - No feature flag.
