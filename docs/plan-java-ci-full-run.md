# Plan — Java CI enforcement runs the plugin in full mode

From `docs/spec-java-ci-full-run.md` and `docs/design-java-ci-full-run.md`.
Non-Jira tool work: no release approval release approval, no Jira-keyed artifacts.
UTA is Python, so the Alibaba Java coding-guideline task does not apply; the
change alters how Java tooling is *invoked*, not Java source.

Main branch, deployed to production to verify, as agreed.

## Dependency graph

```
P1 enforcement mode ─┬─> P2 repair target sourcing ──> P3 deletion pass
                     └─> guards (marker + version message) ship with P1
```

P2 depends on P1 because repair reads the filtered list that P1 makes the
plugin produce. P3 depends on both being proven in production: the deleted
code is the fallback we would need if P1 or P2 were wrong.

## Phase 1 — Enforcement runs the plugin, with guards

Vertical slice: one CI enforcement, end to end, from invocation to evidence.

**T1.1** Add `full_run` to `MavenEnforcementRunner`; when set, skip the
filter-diff preflight and the `targetTests`/`targetSources` scoping. Keep
`-pl <changed modules> -am` (git-derived) and `_with_ci_gate_properties`.

**T1.2** Move both tooling checks out of the removed block so they run in
every mode. *This is the vacuous-pass guard and must land with T1.1, never
after.*

**T1.3** Add the `[test-enforcer]` marker assertion: after the run, if the
output carries no `[test-enforcer]` line, return `missing_evidence`. Never
`passed`.

**T1.4** Make the version message dynamic — artifactId, declared version,
required version — and source the floor from one constant.

**T1.5** Populate evidence post-run: `filteredTargetClasses` from the plugin
output (existing `_with_filtered_target_evidence`), `filteredChangedProductionFiles`
as the production files whose class is in it, `targetTests` from
`_pitest_target_tests` over that list.

**T1.6** `service_composition` constructs the CI runner with `full_run=True`.
`enforcement.py` (binding path) keeps the default.

*Acceptance*: unit tests show one Maven call, no `-N`, no `-DtargetTests`,
no `-Dtest.enforcement.targetSources`, `-pl`/`-am` present; missing or
below-floor plugin yields `missing_evidence` naming both versions; absent
marker yields `missing_evidence`; test-gen path byte-unchanged.

**CHECKPOINT 1** — deploy; re-run `w_idss_ice_ddp`; assert evidence shows the
5 plugin targets and the 5 exclusions (three beans, `MonitorConstants`,
`ImsShopRemoteAdapter`). Do not start P2 until this holds.

## Phase 2 — Repair targets come from the plugin

**T2.1** Repair target selection reads the plugin's filtered list, with the
empty-vs-missing distinction preserved: present-but-empty means no target;
absent means fall back.

**T2.2** Confirm repair runs targeted per target with no preflight.

*Acceptance*: a fix session on `ice-ddp` targets the 5 services and no beans.

**CHECKPOINT 2** — deploy; create a fix session; confirm the repair task's
class list is the 5 services. Do not start P3 until this holds.

## Phase 3 — Deletion

**T3.1** Delete `_filtered_changed_production_java_files`,
`_filter_diff_preflight_command`, `_production_java_files_from_diff`,
`_with_target_sources`, `_exclude_pure_java_interfaces`, and the
`excludedNonExecutableSources` plumbing.

**T3.2** Delete the tests that pin the deleted behaviour
(`test_java_filter_diff_scope.py` and the `excludedNonExecutableSources`
fixtures in `test_java_repair_respects_enforcer_filter.py`).

**T3.3** Keep `_with_target_tests`, `_with_changed_modules`,
`_maven_modules_for_changed_files`, `_pitest_target_tests` — test-gen and
evidence still use them.

*Acceptance*: full suite green; no references to the deleted symbols remain.

**CHECKPOINT 3** — deploy; re-run `w_idss_ice_ddp` and confirm the result is
identical to Checkpoint 1.

## Phase 4 — Documentation and post-deploy check

**T4.1** Update `remote-deployment.md`: the CI gate now runs the plugin over
the changed modules and trusts its verdict.

**T4.2** (I3, deferred from design review) Post-deploy: confirm a Java repo
without the plugin wired reports `missing_evidence`, not `passed`.

## Requirement coverage

| Spec AC | Design decision | Task |
| --- | --- | --- |
| 1 no preflight, keep `-pl`/`-am` | one invocation, git-derived scope | T1.1, T1.6 |
| 2 `ice-ddp` shows 5 targets | plugin owns scope | Checkpoint 1 |
| 3 missing/old plugin ⇒ `missing_evidence` | two guards | T1.2, T1.3, T4.2 |
| 4 version message names both | dynamic message | T1.4 |
| 5 repair = all filtered targets | precheck skips passing | T2.1, Checkpoint 2 |
| 6 repair targeted, no preflight | targeted mode retained | T2.2 |
| 7 test-gen unchanged | out of scope by design | T1.1 regression test, T3.3 |
| — | evidence keys keep meaning | T1.5 |
| — | interface filter and its key removed | T3.1 |

No orphan tasks: every task above maps to an AC or a named design decision.

## Risks

- **P1 wrong ⇒ every Java gate wrong.** Contained by checkpoints: P3 deletes
  the fallback only after P1 and P2 are proven in production.
- **No kill switch** (I2, waived): rollback is a redeploy of the previous
  commit.
- **I3 deferred**: until T4.2 runs, "missing plugin ⇒ not passed" is proven by
  unit test only, not on real traffic.
