# Bounded Python Test Shutdown

Status: approved (non-Jira, main; 2026-09-09)

## Contract And Design

The voice-robot CI run `54f5135401214cf5a17d1cf9df51a660` timed out
after its selected test completed: a QMQ reactor kept a non-daemon thread alive.
UTA must not change the target tests, stop an application-specific reactor, or
interpret printed pytest summaries as authoritative results.

Use one process-completion helper in the standalone Python enforcement package.
Standalone pytest calls finish all pytest cleanup by waiting for `pytest.main`
to return, explicitly stop/save coverage when enabled, and only then arm a
five-second daemon watchdog. Normal exit remains unchanged. If interpreter
shutdown hangs, the watchdog dumps thread stacks and exits with the recorded
exit code. A completion JSON record is written atomically before arming cleanup.
Failures in pytest cleanup or coverage finalization cannot create passing evidence.
No watchdog is armed while a test, teardown, session hook, or adapter is running.

Mutmut 1/2 use the same standalone pytest entrypoint. Mutmut 3 must retain its
in-process pytest collector and exit-code protocol: arm completion only when the
whole adapter command returns, never after one internal pytest phase.
This fixes interpreter thread shutdown, not hanging subprocesses holding pipes;
existing outer resource/process-tree timeouts remain the safety boundary.

## Review

- Correctness: preserve every exit code, including collection/usage errors.
- Architecture: Python-owned behavior; CI, local dev, repair reuse the package.
- Security: no source rewrites, gate changes, or log-text success detection.
- Performance: no delay on ordinary shutdown; five seconds only for a leak.
- Verification: real subprocess tests, coverage persistence, negative hooks and
  save failures, then unchanged production checkout reproduction and fresh CI.

## Plan

- [x] Specify and review completion boundary.
- [x] RED subprocess regression tests.
- [x] Implement shared completion helper and entrypoints.
- [ ] Regression tests, simplification, five-axis code review.
- [ ] Commit/push; idle-safe Git deployment.
- [ ] Replay the production report and record real gate results.

## Changelog

- 2026-09-09: Initial approved incident iteration; clarified mutmut 3 boundary.

## Verification And Release Notes

Focused regression: 114 passed. Expanded process lifecycle tests: 14 passed,
including reproducing the old pytest shutdown hang, real saved coverage, and
exit codes 0 through 5.
Changed production modules pass Ruff; the existing verification test file has
two pre-existing unused-name findings. No dependency, schema, model-policy, or
target-repository changes. Rollback: restore UTA `01bd930` through Git and restart
only when idle. The prior workspace-rule fix is also pending production rollout.

The full suite initially reported 2381 passed, 11 failed, 16 skipped. Five stale
command/budget expectations were updated and reverified (9 staged/budget tests
passed, 4 opt-in tests skipped). All six remaining failures reproduced in a
detached checkout of `01bd930`: CLI model-policy/bootstrap and Java command
expectation tests. These are not waived as a passing full suite.

Self-enforcement against `01bd930` FAILED: aggregate changed-line coverage 2/84,
two surviving mutants. Several changed modules have no strict matched test file;
the real-subprocess tests do not contribute parent-process coverage. Evidence:
`/tmp/uta-shutdown-enforcement.json`. This is a release blocker, not a green gate.

Five-axis review: completion is armed only after pytest cleanup and coverage save;
mutmut internal phase state remains intact; no language-specific behavior moved
to the engine. Shared helper avoids divergent CI/repair/local shutdown semantics.
Completion records use unique atomic filenames and carry no credentials. Timers
are bounded and daemonized. General subprocess leaks and Python 2 execution are
not newly proven here (the helper is syntax-compatible, but no Python 2 runtime
was exercised). No unrelated process is killed. Release status: blocked on gate.

Production-host canary (not service deployment): Git worktree `9b5eeff`, same
failed voice-robot workspace and selected orchestrator test. 14 passed in 1.54s;
whole process exited 0 in 7.62s. The watchdog logged the QMQ reactor and Python
`threading._shutdown` stacks. Coverage XML generation exited 0. Target `git diff`
was empty. No service restart or replacement CI report was triggered.

### Approved Incident Exception

2026-09-09: User approved deployment despite failed local self-enforcement and
six reproduced baseline failures. This is an incident exception, not a green gate.
Commits `9b5eeff` and `91b8ca2` are pushed. Deployment remains pending while
repair tasks 168/169 and CI `49cf6da528844c93b1e6b5b1ce6e4d2c` run.
Production remains at `0329aa6`. API is Supervisor-owned (`uta_api`); daemon
PID 2484240 is standalone. Restart only after both workload types drain, using
Git pull and preserving configuration. Verify new PIDs and health, then replay
`54f5135401214cf5a17d1cf9df51a660` with fresh manual invocation identifiers.

### Production Replay Result

User subsequently authorized stopping the affected CI invocation immediately and
restarting CI independently of repair. Stopped CI `49cf6da528844c93b1e6b5b1ce6e4d2c`,
pulled `7adf355` through Git, and restarted only Supervisor `uta_api`.
API PID changed from 2484224 to 2520034, health HTTP 200. Standalone repair daemon
2484240 and repair tasks 168/169 were not stopped. No configuration changes.

Replacement `5f640fea443642148ba2d37afc63c87b` reused the interrupted RDC invocation
identity (not manual IDs). Completed 2026-09-09 09:10:24Z to 09:13:59Z, about 215s;
callback succeeded. Persisted commands contain `pytest_shutdown_cleanup` with
exitCode 0 and coverage XML was produced. The former shutdown timeout is resolved
on this replay. The repository gate remains FAILED: wmq_consumer coverage 2/3
(66.67%), orchestrator reported selected-candidate coverage 2/8 (25%), API 1/1.
This does not establish successful mutation execution; failing coverage prevents
mutation for those selected candidates. No target source/test edits were made.
Report: https://ci.example.com/unit-test/reports/5f640fea443642148ba2d37afc63c87b/index.html
