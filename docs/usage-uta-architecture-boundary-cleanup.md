# Usage: UTA Architecture Boundary and Package Cleanup

Status: approved design-stage usage contract. Implementation remains gated by
the approved implementation plan. Environment-variable names change only as
specified in "Configuration Migration" below.

## Developer Architecture Rules

- Application code composes testgen, task persistence, generation bindings,
  and enforcement bindings/proxies.
- Testgen uses agent-core for agent execution, generation-language bindings for
  generation domain behavior, and `uta_enforce_core` for enforcement.
- Enforcement callers never import a Java/Python enforcement implementation
  directly. They call `uta_enforce_core.enforce()` with an
  application-composed registry.
- Full UTA reaches Python enforcement through its thin proxy. The local CLI
  registers `uta_py_enforce` directly.
- Task persistence imports neither testgen nor agent-core.
- Agent-core may contain OpenCode/Pi harness adapters but no Java/Python product
  binding.

## Planned Verification Commands

From the UTA repository:

```bash
.venv312/bin/python scripts/check_package_dependencies.py
.venv312/bin/python -m pytest tests -q
.venv312/bin/python -m compileall -q uta tools/python-enforcement
.venv312/bin/python -m ruff check uta tools/python-enforcement tests
git diff --check
```

From an isolated sparse checkout containing only `tools/python-enforcement`:

```bash
PYTHONPATH=tools/python-enforcement python3 \
  tools/python-enforcement/uta_python_test_enforce.py --help
PYTHONPATH=tools/python-enforcement python3 -c \
  'import uta_enforce_core; import uta_py_enforce'
```

From agent-core:

```bash
python -m pytest tests -q
python -m compileall -q src/agent_core
python -m ruff check src tests
git diff --check
```

## Configuration Migration

This is the only operator-facing change. No CLI flag, evidence field, exit code,
or report changes.

`UTA_OPENCODE_*` is replaced. Of the 41 settings, 33 were never interpreted by
UTA and become opaque options forwarded to the harness; 8 are renamed to what
they actually mean.

| Old | New |
| --- | --- |
| `UTA_OPENCODE_MODEL`, `UTA_OPENCODE_PROVIDER` | removed from UTA; the harness reports its own model/provider |
| `UTA_OPENCODE_PLANNING_TIMEOUT_SECONDS` | `UTA_PLANNING_TURN_TIMEOUT_SECONDS` |
| `UTA_OPENCODE_REPAIR_TIMEOUT_SECONDS` | `UTA_REPAIR_TURN_TIMEOUT_SECONDS` |
| `UTA_OPENCODE_AUTH_PROBE_ENABLED` | `UTA_HARNESS_READINESS_PROBE_ENABLED` |
| `UTA_OPENCODE_INIT_SLASH_ENABLED` | `UTA_PROJECT_BOOTSTRAP_ENABLED` |
| `UTA_OPENCODE_INIT_SLASH_TIMEOUT` | `UTA_PROJECT_BOOTSTRAP_TIMEOUT_SECONDS` |
| `UTA_OPENCODE_INIT_COMMAND` | `UTA_PROJECT_BOOTSTRAP_FALLBACK_COMMAND` |
| all others (`UTA_OPENCODE_PORT`, `_SPAWN_CMD`, `_PROVIDER_TOKENS`, `_VARIANT`, `_TURN_LOG_*`, …) | keys inside `UTA_HARNESS_OPTIONS`, a JSON object forwarded to the harness |

Example:

```bash
# before
export UTA_OPENCODE_PORT=4096
export UTA_OPENCODE_PROVIDER_FALLBACK_ENABLED=true
export UTA_OPENCODE_PLANNING_TIMEOUT_SECONDS=900

# after
export UTA_HARNESS_OPTIONS='{"port": 4096, "provider_fallback_enabled": true}'
export UTA_PLANNING_TURN_TIMEOUT_SECONDS=900
```

For one release — the one following this iteration — every old name still works
and logs one deprecation warning per run naming its replacement. Setting both names logs a conflict and uses the
new one. The reader is deleted on the release recorded in the dependency-policy
allowlist.

`uta assess` is unchanged and remains OpenCode-specific: it reads OpenCode's own
database to cross-check the token numbers UTA records. If you switch harnesses,
that command stops applying; nothing else does.

## Local Python Enforcement Behaviour Change

One improvement reaches the local lane. As Python enforcement behaviour is
promoted into `uta_py_enforce`, the local client gains capabilities full UTA
already had — most visibly the runtime-incompatibility precheck, which reports
a Python 2 source file run under a Python 3 lane instead of failing inside
pytest. Verdicts on inputs both lanes already handled do not change; that is
what the parity fixtures assert.

## Local Python Enforcement

Existing flags, environment variables, marker output, evidence schema, and exit
codes remain unchanged. Internally the CLI will construct an
`EnforcementRequest`, build a one-entry registry holding
`PythonEnforcementBinding`, and call `enforce()` once. No UTA checkout, task database, agent-core package, API server, or
LangGraph installation is required.

## Full UTA Enforcement

Full CLI, CI, repair, and generation verification use the same contract. CI
mutation sampling is injected by UTA's CI composition as a selection policy on
the invocation context; no other caller constructs one, so local development,
full CLI, and repair remain unsampled. Java enforcement uses the UTA-local Java
binding behind the same service.

## Rollout And Rollback

Agent-core is released first with additive lifecycle APIs. UTA then pins that
release. UTA migration proceeds by caller with parity gates; final facade
deletion happens only after zero production callers remain. No database
migration is planned, so rollback is a code rollback to the preceding
compatible release.

The Python enforcement redirect is the exception, because it is the one step
where two implementations exist at once. It is controlled by
`UTA_PYTHON_ENFORCEMENT_IMPL` (`legacy` | `shadow` | `canonical`):

- `legacy` is the initial default and the rollback target;
- `shadow` runs both and records one JSON line per comparison under
  `.uta_cache/parity/python-enforcement/`, at roughly double enforcement cost.
  The canonical run has its own budget and never gates the enclosing job.
  Enable it per repository, never in the local developer lane. Read the results
  with `uta parity-report`;
- `canonical` becomes the default after soak (≥ 500 comparisons over ≥ 12
  repositories, zero verdict mismatches, no shadow run dropped for timeout, all
  migration fixtures green).

Soak is run by replaying production requests at a beta node, so it is measured
in coverage rather than elapsed time — there is no calendar wait. Run
`uta parity-report` to see where it stands; when it refuses it lists the reasons.

Rolling back is flipping the variable to `legacy`; it takes effect on the next
run and needs no data repair.

## Operational Proof

Before beta is accepted:

- the packaged dependency scan is clean;
- Java and Python managed canaries finish with expected durable operation rows
  and unchanged evidence versions;
- standalone Java/Python runs leave no execution residue after close;
- sparse-copy Python evidence matches full UTA for the same fixture; and
- UTA logs show neutral lifecycle calls and no concrete OpenCode setup path.

## Changelog

- 2026-08-20 — Initial design-stage usage and architecture guide.
- 2026-08-21 — Adds the configuration migration table and deprecation window,
  the local-lane precheck improvement, and the Python enforcement rollout
  variable.
