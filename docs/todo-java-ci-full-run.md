# Todo — Java CI enforcement full mode

Tracks `docs/plan-java-ci-full-run.md`. Checkpoints are production
verifications, not local test runs.

## Phase 1 — enforcement mode + guards
- [x] T1.1 `full_run` on `MavenEnforcementRunner`: no preflight, no target scoping, keep `-pl`/`-am`
- [x] T1.2 tooling checks run in every mode (vacuous-pass guard — must land with T1.1)
- [x] T1.3 `[test-enforcer]` marker assertion ⇒ `missing_evidence`
- [x] T1.4 version message names artifactId, declared, required; floor from one constant
- [x] T1.5 post-run evidence: `filteredTargetClasses`, `filteredChangedProductionFiles`, `targetTests`
- [x] T1.6 `service_composition` uses `full_run=True`; binding path unchanged
- [x] CHECKPOINT 1 — deployed `f182cdf`; `w_idss_ice_ddp` verified against a pinned
      base (`fc781c99a`, the commit before the TASK-40997 work) because every
      live branch had merged and a replay would have been a vacuous green.
      One Maven call, no preflight, no `-DtargetTests`, `-pl ddp-model,ddp-service -am`;
      plugin reported `pitest.targets=3` and UTA's `filteredTargetClasses` is
      exactly those three; the 6 Bo/Vo beans and `ImsShopRemoteAdapter` were
      excluded by the plugin, not by UTA. Gate failed honestly on ddp-service
      diff coverage 81.43% < 95%. Wall time 148s.
      (The plan predicted 5 targets from the earlier head; this head carries
      later commits, so the count differs. The invariant -- UTA's scope equals
      the plugin's -- is what held.)

## Phase 2 — repair target sourcing
- [x] T2.1 repair reads the plugin's filtered list (empty ≠ missing)
- [x] T2.2 repair runs targeted per target, no preflight
- [x] CHECKPOINT 2 — deployed `846aef7`; `_repair_class_fqns` on the deployed
      code, fed Checkpoint 1's real plugin evidence, returns the plugin's 3
      classes out of 10 changed and zero beans/wrappers. Note: this exercises
      repair target selection end-to-end on real evidence, not a full fix
      session — no unmerged branch exists to run one against.

## Phase 3 — deletion
- [x] T3.1 delete preflight functions + `excludedNonExecutableSources` plumbing
- [x] T3.2 delete tests pinning the deleted behaviour
- [x] T3.3 keep `_with_target_tests`, `_with_changed_modules`, `_maven_modules_for_changed_files`, `_pitest_target_tests`
      — plus `_with_target_sources`, which the plan listed for deletion. It is
      still live: it writes the pinned `-Dtest.enforcement.targetSources` for a
      targeted repair run, so deleting it would *widen* repair's enforcement
      scope to every changed file. Deviation from the plan, recorded here
      rather than resolved silently.
      `_is_pure_java_interface` was deleted alongside `_exclude_pure_java_interfaces`
      (it had no other caller); the plan named only the latter.
- [x] CHECKPOINT 3 — deployed `569ba06`; same pinned base, result identical to
      Checkpoint 1 on status, summary, command, target patterns, filtered
      classes, filtered files, target tests and modules. (25s vs 148s: warm
      Maven repo.)

## Phase 4 — docs + deferred check
- [x] T4.1 update `remote-deployment.md`
- [x] T4.2 (I3) confirmed on deployed code: a repo with no test-enforcer wired,
      with Maven exiting 0, reports `missing_evidence` and `passed=False`.

## Ship

- [x] Code review over Phases 1–3 — one finding, fixed in `4232dfd`: full mode
      stopped publishing `testQuality` because it is derived from `targetTests`,
      which the plugin only names after the run. Nothing failed; the report
      panel would just have gone blank.
- [x] Simplify — done as Phase 3: 701 lines deleted, 31 added.
- [x] Shipped to production: `main` at `4232dfd`, deployed to
      `agent1.ai.ops.bj1`, API pid 1588545, daemon pid 1588504, health `ok`.
      Post-deploy re-run on the pinned base is identical to Checkpoint 1 and
      publishes `testQuality`.

### Verification fixture left on the host

`/opt/app/uta-ckpt1/ice-ddp` is a copy of the ice-ddp workspace with
`refs/remotes/origin/master` pinned to `fc781c99a`, plus `result.json` and
`stdout.log`. It exists because every ice-ddp branch had merged, so a live
replay reports "no changed production Java files" and proves nothing. Keep it
to re-verify, or delete it — nothing depends on it.
