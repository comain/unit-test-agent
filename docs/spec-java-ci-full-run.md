# Spec — Java CI enforcement runs the plugin in full mode

Non-Jira tool work (confirmed with the requester). UTA is our own tooling and
its docs are topic-named; no release approval release approval applies.

## Objective

Java CI enforcement should run the Maven `test-enforcer` plugin over the whole
reactor and take its verdict, instead of running a `filter-diff` preflight and
re-deriving the enforcement scope in Python.

Targeted (narrowed) runs remain, but only where they belong: test generation
and repair, which work one target at a time and must stay in that scope.

## Why

Three stacked defects, each hidden by the one before it, all traced to the
preflight. Observed on production task `a6d0aadb` (`w_idss_ice_ddp`,
`TASK-40997-20260826`):

1. The preflight ran root-only (`-N`), so the plugin could not see the modules
   and reported every changed file as coverable — 10 of 10.
2. It gated on Maven's exit code, discarding a correct result because a later
   module failed.
3. It read `target/filtered.diff` — which lists *every* changed file — instead
   of the plugin's own `pitest.targets`.

A recursive `verify` of the same diff reports `pitest.targets=5`, excluding
three beans, a constants class and a remote wrapper, and says so per module:
`no coverable changed lines for ddp-common`, `no changed Java source lines for
ddp-model`.

Consequences: the fix session generated tests for beans; and enforcement
declared `targetTests: 0` over a scope five classes too wide, then refused to
run, while `MonitorConstantsTest` exists and passes 9 tests.

### The preflight failure is an artefact of `initialize`

`-N` was added for TASK-40978, where a recursive run made a module resolve a
sibling jar from Nexus before the reactor packaged it. That is real — but only
for `initialize`, which packages nothing. Measured on the same repository:

| module | recursive `initialize` (preflight) | recursive `verify` (real run) |
| --- | --- | --- |
| ddp-common | SUCCESS | SUCCESS, jar built |
| ddp-model | FAILURE, cannot resolve `ddp-common:jar:1.0` | SUCCESS, jar built |
| ddp-dao | SKIPPED | SUCCESS |
| dependency-resolution errors | 1 | 0 |

A full run packages each module, so the reactor satisfies sibling dependencies.
TASK-40978 cannot recur in full mode, which removes the reason `-N` exists.

## Scope discovery

Searched for every module and evidence key that produces or consumes Java
enforcement scope.

| Component | Role | Decision | Reason |
| --- | --- | --- | --- |
| `enforcement_runner/__init__.py` | CI run orchestration | **in scope** | owns the preflight and scoping block |
| `enforcement_runner/planning.py` | preflight + scoping helpers | **in scope** | source of the three defects |
| `app/service_composition.py` | constructs the CI runner | **in scope** | must select full mode |
| `language/java/ci.py` | repair target selection | **in scope** | must read the plugin's filtered list |
| `language/java/generation/quality.py` | test-gen delegated gate | **out of scope** | targeted mode is correct here; unchanged |
| `language/java/enforcement.py` | second runner construction | **out of scope** | used by the binding, keeps current default |
| `language/java/maven_project.py` | tooling/version status | **in scope** | version message must name found vs required |
| `app/context.py`, `ci_evidence.py` | render `targetTests`, `filteredTargetClasses` | **out of scope** | read-only consumers; keys must keep their meaning |
| `~/fd/maven-plugins/test-enforcer` | the plugin | **out of scope** | changing it needs its own rollout (`example-parent-generic` / `example-root`) |

## Acceptance criteria

1. CI Java enforcement runs no `filter-diff` preflight: one Maven invocation,
   no `-N`, no `-DtargetTests`, no `-Dtest.enforcement.targetSources`.
   `-pl <changed modules> -am` is kept — it is derived from the git diff and
   never needed the preflight (design review C1). `-am` keeps the root in the
   reactor, which is required because only the root emits `pitest.targets`.
2. On `w_idss_ice_ddp` the evidence shows the plugin's 5 targets, with the
   three beans, the constants class and the wrapper excluded.
3. A repository without the plugin wired, or below the required version, is
   reported as `missing_evidence` — **never** `passed`. Verified by asserting
   the `[test-enforcer]` marker in the run output, not only by a pre-probe.
4. The version failure names **both** the required version and the version the
   project actually declares.
5. On gate failure, repair targets are all of the plugin's filtered targets.
   Repair's `precheck` closes out any that already satisfy the gate.
6. Repair runs targeted per target, with no preflight.
7. Test generation's targeted path is behaviourally unchanged.

## Boundaries

- **Always**: keep the vacuous-pass guard; a run that cannot prove enforcement
  happened fails closed.
- **Ask first**: any change to the Maven plugin, or to the meaning of an
  evidence key other consumers read.
- **Never**: silently widen or narrow enforcement scope without it being
  visible in evidence.

## Testing strategy

- Unit: full mode issues one Maven call with no scoping flags; missing/old
  plugin yields `missing_evidence` with both versions named; repair targets
  come from the plugin's filtered list.
- Regression: test-gen targeted path unchanged.
- Production: re-run `w_idss_ice_ddp` and confirm 5 targets and the exclusions;
  confirm a repo without the plugin does not report `passed`.

## Documentation

- `docs/design-java-ci-full-run.md` — design and the code removed.
- `remote-deployment.md` — note the CI gate now runs the full reactor.
