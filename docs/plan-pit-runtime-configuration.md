# Plan: PIT Runtime Configuration Compatibility

Status: Automatic UTA/local activation published; production original-revision replay verified.
Non-Jira iteration. UTA 65d903d; dev-skills 7150a9e pins that exact artifact.
Base: main. Preserve unrelated uv.lock and runtime artifacts.

## 1. Ordered Implementation

### 1.1. Real Reactor Reproduction

- Add an isolated fixture builder/test for unchanged service and changed biz, using
  Java 8, Maven 3.9.10, PIT 1.15.0 and released test-enforcer 1.0.15.
- Set a local git base ref and stale plugin/execution skip/target configuration.
- Record RED: service PIT aborts before biz. Historical 1.0.13 is optional additional
  reproduction only; it cannot be used for GREEN or bypass current minimum checks.
- Verify with `UTA_PIT_COMPAT_REAL=1 .venv/bin/python -m pytest
  tests/test_pit_runtime_compat_reactor.py -q`; fixture failures must not be skipped.
- Checkpoint: establish that latest released enforcer still exhibits the defect before
  adding an extension. If it already resolves it, stop and revise scope.

### 1.2. In-Memory Normalization And Resolved-Parameter Guard

- Add the Java 8 extension source/POM/Plexus registration in the Java-owned package.
- RED/GREEN Java tests for plugin/execution normalization, preservation, explicit intent,
  reserved session properties and unsupported versions.
- Implement afterProjectsRead normalization and post-configuration mojo inspection.
- Verify `mvn -f uta/language/java/maven_compat/pom.xml test` with Java 8.
- Real reactor must show service skips PIT and biz runs real mutants; an intentionally
  weak biz suite must still fail. Do not proceed on mock-only success.

### 1.3. Per-Invocation Completion Evidence

- Add bounded evidence writer/parser and private module/execution report paths.
- RED/GREEN cases: earlier green module cannot mask missing obligated module; stale
  reports, empty reports, unexpected class scope, absent filter-diff and reordered goals.
- Keep baseline and threshold failures distinct from successful execution missing data.
- Verify both Java tests and real reactor tests, including identical-output reruns.

### 1.4. Shared Launcher And Distribution

- Add stdlib launcher, deterministic artifact build/manifest and setuptools package data.
- RED/GREEN launcher tests: missing/corrupt artifact, classpath preservation/conflicts,
  fresh invocation IDs, signal/exit propagation, unsupported tools and bounded evidence.
- Verify wheel/sdist contents and standalone sparse-checkout invocation without UTA imports.
- Commands: `.venv/bin/python -m pytest tests/test_pit_runtime_compat_launcher.py -q`
  and `.venv/bin/python -m build`. Build dependency availability is verified first.

### 1.5. UTA Invocation And Verdict Integration

- Route only Java diff execution through the shared launcher; leave diagnostics,
  Python, direct class PIT and shared workflow unchanged.
- Validate compatibility before success classification and before baseline retry policy.
- Preserve real effective commands and per-invocation evidence across every retry.
- Verify new cross-layer tests plus `tests/test_api_trigger_enforcement.py`,
  `tests/test_maven_parsers.py`, `tests/test_java_ci_full_run.py`, and
  `tests/test_java_enforcement_binding_promotion.py`.

### 1.6. Local-Dev Parity And Guidance

- Commit the verified UTA artifact before recording its immutable commit/digest in the
  dev-skills lock. Use the existing sparse-install conventions; no copied Java logic.
- Add local-gate tests requiring shared completion validation for applicable Java commands.
- Update English/Chinese test-enforce usage and UTA report guidance, including version
  mismatch and completion errors. No claim of released installation before the pin exists.
- Test installed pinned launcher against the same real reactor as UTA.

### 1.7. Review And Pre-Deployment Gates

- Run Java compile/unit/reactor tests, Python regression suite, packaging and local
  enforcement; record commands/results. Perform simplify and independent five-axis review.
- Review Java code against Alibaba Mandatory rules and configured static checks.
- No schema migration; preserve source/POM hashes around success/failure/timeout.
- Commit and push only scoped files, verify remote HEAD. Non-Jira production replay was
  authorized; never force a passing callback or weaken a gate.

### 1.8. Production Replay

- Read current remote-deployment.md and access skill. Confirm idle before Git-pull deploy
  or restart; do not use old node2 or interrupt other running work.
- Verify artifact identity, Java 8, new process IDs and API health.
- Replay dms-order-core report c3a75efa07774cc4a404f0f44014c3f5 at recorded base/head.
- If effective enforcer is below 1.0.15, retain version mismatch and obtain owner upgrade;
  do not modify application POM or treat blocked replay as verification passed.
- Acceptance: authoritative service no-target skip; biz real PIT execution/verdict;
  no target-file edits. Also run known-good Java and genuine-negative regression lanes.
- Record results and report URLs. Roll back matched code/artifact if scope is incorrect.

## 2. Requirement Coverage

| Requirement | Tasks |
| --- | --- |
| Spec 3.1 module scope authority / no source edits | 1.1, 1.2, 1.3, 1.8 |
| Spec 3.2 parameter ownership / exclusions / thresholds | 1.2, 1.3, 1.5 |
| Spec 3.3 evidence and cross-path parity | 1.3, 1.5, 1.6 |
| Design resolved-parameter and reserved-property guards | 1.2, 1.3 |
| Design fresh XML and baseline retry distinction | 1.3, 1.5 |
| Latest released enforcer / no old-version bypass | 1.1, 1.2, 1.8 |
| Packaging, immutable distribution, rollback | 1.4, 1.6, 1.8 |
| No API/DB changes; Java-owned boundary | 1.5, 1.7 |
| Verification, security, performance and source integrity | 1.1-1.8 |
| Usage/report docs and dev-skills synchronization | 1.6, 1.7 |

## 3. Progress

2026-09-08 checkpoint: real Java 8/Maven 3.9.10/PIT 1.15.0 fixture passes with
both enforcer 1.0.13 and 1.0.15, without any extension. Both runs skip service mutation
and kill 2/2 biz mutants. Thus the generic "effective POM literals freeze runtime
properties" diagnosis is not established. The fixture does not yet reproduce the
production failure; it is counterevidence, not a successful bug fix. Command:
`UTA_TEST_MAVEN=/home/user/software/apache-maven-3.9.10/bin/mvn .venv/bin/python
-m pytest tests/test_pit_runtime_compat_reactor.py -q` (2 passed in 16.11s).

Production read-only check also confirmed the separate version-guard hole:
`declared_test_enforcement_tooling_status` returns "No resolved test-enforcer Maven
plugin was found" for profile-defined 1.0.13; `has_declared_tooling_version_mismatch`
returns false. Effective-POM resolution is not required on every execution path.
Do not infer that an upgrade alone fixes PIT, or ship the extension without RED proof.

- [x] 1.1 Real reactor RED
- [x] 1.2 Normalization and parameter guard
- [x] 1.3 Completion evidence
- [x] 1.4 Launcher and distribution
- [x] 1.5 UTA integration
- [x] 1.6 Dev-skills and guidance
- [ ] 1.7 Review and pre-deployment gates
- [x] 1.8 Production replay

## 4. Changelog

- 2026-09-08 (enabled both): UTA 65d903d and dev-skills 7150a9e published and
  remote refs verified. Dev-skills tests: 68 passed. A fresh temporary sparse
  install fetched the immutable UTA commit, verified all three hashes, and ran the
  real reactor successfully (service skipped, biz completed, fresh ledger).
  Production updated via Git pull while idle; API PID 2454706, daemon 2454721,
  healthz 200. Three historical rerun_running records dated Aug 31 were confirmed
  stale, with no live Maven/mutmut processes; they were not modified.
  Automatic MavenEnforcementRunner replay of original d6714db93, released enforcer
  1.0.15 override, unchanged tracked POM/source: 66.73 seconds, service skip=true,
  biz skip=false, 334 generated / 41 killed / 289 no-coverage / 4 survivors.
  Actual scored strength 41/45 = 91.11%, below 100: correctly failed, not a false
  success. No report or RDC callback overwritten. Evidence:
  production runner/dms-original-enabled-replay.json. Step 1.7 remains unchecked
  for the broader proposed matrix/static checks not run; the release used the
  recorded focused tests, nine real reactor cases, packaging, and independent review.

- 2026-09-08 (automatic activation): canonical stdlib launcher, packaged Java-8
  JAR/digest, UTA verify integration and sparse dev-skills bootstrap implemented.
  Full invocation defaults to the actual PIT suite; explicit repair/baseline test
  selection is preserved. Fresh per-module completion is required even after a
  nonzero build with otherwise green metrics. Compatibility setup/runtime failures
  are terminal and not application-repairable. Local cancellation terminates the
  Maven process group. Focused regression plus nine real Maven cases: 123 passed.
  Wheel and sdist built successfully. Publication/pinned-install and production
  results are recorded separately; historical task gates are not overridden.

- 2026-09-08 (production-host adapter proof): built opt-in adapter `9f71a7c`
  using production Java 8u121/Maven 3.9.10 after Git pull, without restarting
  services or enabling it for normal jobs. On the same historical checkout,
  service receives verified skip=true and biz receives skip=false. First run
  exited zero in 51.67s but produced 334 no-coverage mutants because the application
  POM pins unrelated targetTests; this is **not accepted as gate verification**.
  Repeating with the original report's two recorded targetTests explicitly supplied
  produces coverage 44/45 (97.78%), 334 generated mutants, 41 killed, 289 no-coverage,
  and 4 survivors: test strength 41/45 (91.11%, PIT displays 91), below 100.
  Exit 1 in 65.03s is the real mutation verdict, not the former service tooling
  failure. Tracked source/POM diff remained empty. Logs are in production runner
  `dms-original-1.0.13.log`, `dms-original-1.0.15.log`,
  `dms-original-compat.log`, and `dms-original-compat-selected.log`.
  No report/RDC callback was overwritten and no target repair was performed.
- 2026-09-08 (verification): seven real Maven integration cases passed in 38.56s,
  including stock RED, adapter GREEN, and weak-test mutation failure. Fixture now
  includes stale profile targetTests plus an explicit selected-test override.
  Full-run integration must supply deliberate PIT test scope; preserving stale
  POM targetTests is not sufficient. Remaining rollout tasks above are not complete.

- 2026-09-08 (original-revision replay): isolated checkout at `d6714db93`,
  base `60fdb67e4efb9559f694cbdccea11ce0da215322`, reproduces service PIT
  "No mutations found" with both 1.0.13 (36.44s) and 1.0.15 (26.66s).
  Reused the recorded command, including `maven.test.failure.ignore=true`.
  The earlier replay omitted that flag and failed before PIT, so it was not
  comparable. The live workspace had advanced to `445bd2f14`; its result is
  not evidence about the historical failure.
- 2026-09-08 (fixture correction): the earlier small fixture had no service test
  directory; PIT skipped independently of skipPitest. Adding a service unit test
  establishes RED on released 1.0.15. Runtime trace confirms injected skip=false
  with the enforcer's __no_changes__ target sentinel. An opt-in lifecycle adapter
  restores late binding and validates actual injected skip/targets; the real fixture
  reaches biz. This is the implementation proof, not completion of packaging,
  full evidence guards, default UTA integration or dev-skills rollout.

- 2026-09-08: Drafted ordered test-first plan from the twice-revised design. No
  implementation or production verification is claimed complete.
