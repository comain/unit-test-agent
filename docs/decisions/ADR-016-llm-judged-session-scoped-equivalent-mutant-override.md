# ADR-016: LLM-Judged, Session-Scoped Equivalent-Mutant Gate Override

## Status

Accepted

## Date

2026-09-14

## Context

The UTA RDC gate requires diff mutation strength to meet a threshold. Java uses 100% and Python uses 95%. Some mutants are *equivalent*: the mutated program is observably identical to the original, so no test can kill them. When a CI report fails only because of such mutants, the one-click fix session spends its repair turns and stops on `mutation_repair_no_progress`. RDC stays blocked even though the tests are as strong as the code allows.

Deciding equivalence in general is undecidable. The existing `likely_equivalent` heuristics (logger calls, method-name tokens) are too coarse to gate on. Changing the UTA Maven plugin to exempt mutants would need a plugin release and would change the gate for everyone.

## Decision

1. **An LLM review session judges equivalence.** Deterministic code decides whether that judgment may be used. Every scoring survivor gets a structured verdict (`equivalent` / `killable` / `uncertain`). The verdict must name the divergence region and argue that the whole region is indistinguishable, not just an example input.
2. **Fail closed on structure.** The override is granted only if all of the following hold:
   - every survivor is `equivalent`;
   - no other mutant counts against the score (NO_COVERAGE, TIMED_OUT, RUN_ERROR, MEMORY_ERROR, unknown; Python timeout or suspicious);
   - the count is ≤ 30;
   - a fresh gate rerun reproduces exactly the reviewed mutant identities, with unchanged source fingerprints;
   - tests and coverage pass.
3. **Session-scoped and visible.**
   - The review is a neutral generation-cycle phase in the CI repair task. It runs once per unit, just before the repair would stop on no-progress. The session's existing post-task rerun is the recheck. No LLM call happens in the API poll.
   - The fix session becomes `passed_with_equivalent_mutants`. The CI record stays `success`, with a `gate_override` field (no new `CiTaskStatus`, which keeps revert safe).
   - The raw score and reasoning are kept.
   - RDC receives one success callback whose summary names the override.
   - No later CI run inherits the decision.
4. **Neutral ownership.** The rule lives in `uta/enforcement/equivalent_mutants.py` and the repair-session orchestration. Language adapters only provide survivor identities.
5. **Trigger only on `mutation_repair_no_progress`.** There is no runtime feature flag; rollback is by reverting the commit, which keeps the code simple.

## Alternatives Considered

- **Deterministic equivalence detection** (heuristics, TCE-style bytecode or AST comparison). Rejected: TCE finds only a small fraction of equivalent mutants in practice, and the heuristics produce false grants.
- **Exempt mutants in the Maven plugin / enforcement core.** Rejected: it needs a plugin release, it affects every consumer and the local dev gate, and it makes the override permanent and invisible.
- **Human approval button on the report.** Rejected for now: the goal is unattended unblock. The visible status and reasoning still allow a human audit after the fact.
- **Persist the decision per branch/commit so later CI runs pass.** Rejected by the user: an independent CI run must re-gate.
- **Also trigger on attempts-exhausted.** Rejected by the user (D1).

## Consequences

- A wrong LLM judgment can unblock one deployment of one branch. This is mitigated by the fail-closed structure, the review cap, visible reasoning, commit-revert rollback, and normal re-gating on the next CI run.
- Cost per eligible unit: one LLM turn. The recheck reuses the rerun that already happens.
- `CiTaskStatus` is unchanged. Report pages read `gate_override` to show the override badge.
- Repair units persist `equivalence_review_json`, and the generation cycle gains one shared phase.
