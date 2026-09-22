# Design — Java CI enforcement runs the plugin in full mode

Derived from `docs/spec-java-ci-full-run.md`. Single repo, so the detail design
is folded in rather than split into `-<repo>.md`.

## Goals

- CI Java enforcement takes the plugin's verdict instead of re-deriving scope.
- A run that cannot prove enforcement happened never reports `passed`.
- A version shortfall names the required *and* the declared version.
- Repair targets come from the plugin's filtered list.

## Non-goals

- Changing `test-enforcer` (separate rollout via `example-parent-generic`/`example-root`).
- Changing the test-generation targeted path.
- Changing the meaning of any evidence key other components read.

## Design

### One invocation, and the plugin owns scope

CI enforcement runs `mvn … verify` **once**, with no preflight and no
`-DtargetTests` / `-Dtest.enforcement.targetSources`. It keeps `-pl <changed
modules> -am`, which is derived from the **git diff** and never needed the
preflight.[^scope] `test-enforcer`
already decides scope per module and publishes it as
`pitest.targets=N [class*, …]`, applying rules UTA cannot see —
`isPitestTargetCandidate` drops testability-hint suffixes (`Constants`, `Enum`,
`Config`, `Wrapper`, `Adapter`, `Proxy`, …), entry-wrapper annotations, and
sources whose changed methods are all accessors, i.e. beans.

Mode is selected by the caller, not inferred:

| Caller | Mode | Why |
| --- | --- | --- |
| `service_composition` (CI gate) | full | enforces everything the diff touched |
| `generation/quality.py` (test-gen) | targeted | works one batch at a time |
| repair | targeted per target | target already known |

### Evidence in full mode

| Key | Source before | Source after |
| --- | --- | --- |
| `changedJavaFiles`, `changedProductionFiles`, `changedClasses` | git diff | unchanged |
| `filteredTargetPatterns`, `filteredTargetClasses` | preflight `filtered.diff` | plugin output, via existing `_with_filtered_target_evidence` |
| `filteredChangedProductionFiles` | preflight | production files whose class is in `filteredTargetClasses` |
| `targetTests` | `_pitest_target_tests` over preflight output | same function, over the plugin's filtered list |
| `excludedNonExecutableSources` | UTA's interface filter | **removed** — see below |

`_pitest_target_tests` is kept and re-pointed: `app/context.py` and
`ci_evidence.py` read `targetTests`, and dropping it would degrade the repair
prompt and the report. Keeping `filteredChangedProductionFiles` populated keeps
repair's existing selection path working unchanged.

Ordering changes: the filtered list is known only *after* the run, so evidence
is completed post-run rather than pre-run. No consumer depends on it earlier.

### Vacuous-pass guard

Two checks, deliberately different in kind:

1. **Declared version** (pre-run, pom-only, free): below the floor ⇒
   `missing_evidence` naming artifactId, declared version, required version.
2. **Marker assertion** (post-run): the output must contain a `[test-enforcer]`
   line. Absent ⇒ `missing_evidence`, never `passed`.

The second is the one that matters. A pre-probe proves the plugin is *declared*;
the marker proves it *ran*. Without it, full mode's failure mode is a silent
green on a repo with no enforcement — worse than today's noisy wrong answer.

### Repair targets

On gate failure, repair takes **all** of `filteredTargetClasses`. Repair's
`precheck` already returns "proceed, delegated repair, or a completed
existing-test result", so targets that already satisfy the gate close out
without work. This is what lets us leave the plugin alone: we do not need it to
emit a per-target failure list, because repairing the full filtered set is
equivalent once precheck filters it.

Rejected: parsing per-target failures out of the plugin. `CheckCoverageMojo`
reports only a module aggregate (`diff line coverage 47.67% … (215/451)`), and
the plugin emits no mutation reporting at all — PIT fails the build in its own
format. Getting a real failure list means changing the plugin, which is
out of scope.

## Tradeoffs

**Wall time stays bounded.** Design review (C1) caught that "no preflight" and
"no scoping" are different things: module selection comes from the git diff, so
dropping the preflight does not require running the whole reactor. Measured on
`ice-ddp`: `-pl ddp-common,ddp-model,ddp-service -am` builds 5 projects instead
of 7, and `-am` keeps the root in the reactor — which matters because only the
root emits `pitest.targets`.

An unscoped full-reactor run was considered and rejected: it would have put an
unbudgeted cost on every Java CI enforcement, with a single 21s datum from a
repo whose suite is trivial.

**TASK-40978 does not recur.** It was an `initialize`-only artefact: `verify`
packages each module, so siblings resolve from the reactor. Measured — the same
diff fails resolution under `initialize` and builds cleanly under `verify`.

## Failure modes

| Condition | Result |
| --- | --- |
| plugin not declared / below floor | `missing_evidence`, both versions named |
| plugin declared but never ran (no marker) | `missing_evidence` |
| module does not compile | `failed`, existing `_looks_build_broken` summary |
| no changed production Java | `passed` (git-derived, pre-run, unchanged) |
| Maven timeout | `timeout`, unchanged |

## Code removed

| Symbol | Lines | Reason |
| --- | --- | --- |
| `_filtered_changed_production_java_files` | 113 | the preflight |
| `_filter_diff_preflight_command` | 42 | builds `initialize -N` |
| `_production_java_files_from_diff` | 11 | parses `filtered.diff` |
| `_with_target_sources` | 9 | CI-only scoping |
| `_exclude_pure_java_interfaces` | 8 | see below |
| `excludedNonExecutableSources` plumbing | ~6 | nothing consumes it |

~183 lines, plus the scoping block and the `no related targetTests` refusal in
`__init__.py`. `_with_target_tests`, `_with_changed_modules` and
`_maven_modules_for_changed_files` stay — test-gen uses them.

This also removes `filterDiffScope`, `filterDiffRawReturncode` and the
`pitest.targets` narrowing added earlier today (`65db131`, `2d90a64`,
`2295a1b`). Those were patches to a component that should not exist; they
served to locate the root cause and are correctly deleted with it.

### Why the interface filter goes rather than changes

`_exclude_pure_java_interfaces` exists for one stated reason: "keep pure Java
interfaces out of `targetSources` so declaration-only DAO changes cannot force
a module-local JaCoCo report that can never contain executable lines". It is a
sanitiser for `targetSources`, which full mode no longer passes — so its reason
disappears with it. The plugin already handles the case in the owning module:
`CheckCoverageMojo` short-circuits with `no coverable changed lines for
<module>` when `coverable == 0`.

`excludedNonExecutableSources` is dropped with it. Every reference is a
producer, a test of the deleted code, or prose: nothing reads it — not
`context.py`, `ci_evidence.py`, the report template, or repair. Deleting the
key satisfies the "don't change the meaning of keys others read" non-goal
outright, rather than keeping a contract alive for no reader.

## Design review dispositions

| # | Finding | Disposition |
| --- | --- | --- |
| C1 | full-reactor wall time unbudgeted | **fixed** — keep git-derived `-pl … -am`; only the preflight goes |
| I1 | `excludedNonExecutableSources` meaning would change | **fixed by deletion** — no consumer exists |
| I2 | no kill switch for the mode | **waived** by the requester: keep the code simple |
| I3 | production verification proves only the happy path | **deferred** to a post-deploy check: confirm a repo without the plugin reports `missing_evidence` |

[^scope]: Also considered: unscoped full-reactor run. Rejected — unbudgeted cost
on every enforcement, and unnecessary once module selection is recognised as
git-derived.

## Rollout

Main branch, deploy to production, verify there — as agreed. Sequenced so each
step is falsifiable:

1. Full mode + guards + version message; deploy; re-run `w_idss_ice_ddp` and
   assert 5 targets with the five exclusions.
2. Repair target sourcing; verify a fix session targets 5 services, no beans.
3. Deletion pass once 1 and 2 are proven in production.

Rollback is the previous commit; no schema or config change is involved.

## Verification plan

- Unit: one Maven call with no scoping flags; missing/old plugin ⇒
  `missing_evidence` with both versions; marker absent ⇒ not `passed`;
  repair targets equal the plugin's filtered list; test-gen path unchanged.
- Production: `w_idss_ice_ddp` shows 5 targets and the 5 exclusions; a fix
  session generates for services only.
