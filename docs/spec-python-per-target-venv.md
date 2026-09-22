# Spec: Per-target Python verification venv

## Status

- Phase: Spec generation, pending human approval before design
- Jira: Confirmed non-Jira tool work. Do not require a Jira key, release approval
  release approval, or RDC pipeline for this change.
- Design document: `docs/design-python-per-target-venv.md` after spec approval
- ADR: `docs/decisions/ADR-016-python-per-target-venv.md` after spec approval
- Usage document: `docs/usage-python-per-target-venv.md` after design approval
- Original request: Close the Python enforcement overlay leak instead of
  patching the `--target` overlay heuristic. Give the verifier its own
  environment. Verify on beta by replaying production task
  `f12aec735b1840c18051263bc614d92c` (mmc_app_voice_robot,
  branch `TASK-41000-20260831`, repair session
  `e544ba2d0b4a4fcb964b6a68dfe169db`).

## Assumptions

1. This is non-Jira internal tooling. Base branch is `main`.
2. Work lands on a short-lived branch `fix/python-per-target-venv`, not on
   `main` until the beta replay is recorded.
3. The unclosable overlay heuristic (import scan, alias table, token matching,
   marker-to-requirement maps) is already gone from current `main`. The
   remaining defect is the missing environment boundary: `--target` overlay on
   `PYTHONPATH` plus `_python_runtime_fallback_bin` falling back to
   `sys.executable`.
4. `setup_command` remains the documented escape hatch for repos a plain
   manifest install cannot set up.
5. Python 2 / `mutmut-legacy-py2` is out of scope for this venv change.
6. Beta verification means deploy the branch to the beta node and replay this
   production task. Production deploy is out of scope.

→ Correct these before design if any of them is wrong.

## Objective

Python enforcement currently borrows UTA's interpreter whenever the node's
`python3` lacks pytest/coverage, and it installs target dependencies with
`pip install --target` onto `PYTHONPATH`. That makes UTA's own dependency
tree part of the target's test environment. After the agent-core migration,
that tree includes pytest plugins (`langsmith`, `anyio`) that autoload into
every target run. The overlay-heuristic patches of 2026-09-01 cannot close
this: they paper over missing packages, ABI mismatches, and plugin leakage
one incident at a time.

The user of this change is UTA itself — the Python verifier used by CI,
repair sessions, and the lightweight `uta_py_enforce` binding. Success is a
verifier that never executes target tests inside UTA's venv, never silently
under-provisions the target environment, and can be shown to work on the
production `mmc_app_voice_robot` failure by replaying it on beta.

## Scope Discovery

| Candidate | Current role | Decision | Reason |
| --- | --- | --- | --- |
| `tools/python-enforcement/uta_py_enforce/dependency_overlay.py` | Nested-manifest overlay: flatten, `pip install --target`, digest cache, flock, skip-unavailable | In scope | This is the environment builder. It must become a per-target venv. |
| `uta/language/python/verification/runtime_setup.py` | Chooses interpreter, installs overlay, falls back when pytest/coverage missing | In scope | `_select_overlay_runtime` and post-overlay `sys.executable` fallback are the leak. |
| `uta/language/python/verification/runtime_config.py` | `_python_runtime_fallback_bin` includes `sys.executable` | In scope | `sys.executable` must never be the verifier. |
| `tools/python-enforcement/uta_py_enforce/runtime.py` | Lightweight runtime resolution prefers `sys.executable` | In scope | Same leak on the promoted binding. |
| `tools/python-enforcement/uta_py_enforce/config.py` | `UTA_PYTHON_BIN` defaults to `sys.executable` | In scope | Default must be `python3`, not UTA's interpreter. |
| `tools/python-enforcement/uta_py_enforce/api.py` | Calls overlay then runs coverage/mutation with overlay `PYTHONPATH` | In scope | Must run with the venv interpreter, not overlay-on-PYTHONPATH. |
| `uta/language/python/verification/pytest_execution.py` | Sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` because UTA plugins leak | In scope | Autoload disable is a symptom of the missing boundary; delete once the venv exists. |
| `tools/python-enforcement/uta_py_enforce/pytest_env.py` | Same autoload disable on the lightweight path | In scope | Same deletion. |
| `tools/python-enforcement/uta_py_enforce/optional_plugins.py` | Re-injects marker-activated plugins after autoload is killed | In scope for deletion of the autoload workaround | pytest-timeout as a UTA safety net stays as a toolchain package installed into the venv, not as a PYTEST_PLUGINS shim. |
| `uta/language/python/verification/dependency_requirements.py` | Facade over `uta_py_enforce.dependency_overlay` | In scope only as a thin re-export | Keep the language-adapter facade; do not duplicate venv logic. |
| `setup_command` in `runtime_config.py` / `models.py` | Documented escape hatch, unset everywhere | In scope as fallback | Repos a plain manifest cannot set up use `setup_command`; do not grow another overlay special case. |
| Digest-keyed cache and flock in overlay | Cache + lock for concurrent installs | In scope, keep | Sound. Re-key on `(manifest sha, interpreter)`, not a selected-subset list. |
| Skip-unavailable (`No matching distribution`) | Drop one missing index line and retry | In scope, keep at manifest level | A single missing package must not empty the environment; an index that serves nothing must still fail loudly. |
| Import-scan / alias table / token matching | Already deleted on `main` | Out of scope | Do not revive. |
| Java enforcement, Maven, PIT | Other language backend | Out of scope | Architecture invariant: language-specific runtime stays behind the Python adapter. |
| Python 2 / `mutmut-legacy-py2` | Legacy lane | Out of scope | Do not create a venv for Python 2 in this change. |
| Production deploy of UTA | Shipping to agent1 | Out of scope | This iteration verifies on beta only. |

## Requirements

1. When a target has a nearest nested `requirements.txt`, the verifier creates
   an isolated virtualenv from that manifest with `pip install -r`. Pip owns
   resolution, including transitives, extras, and environment markers.
2. UTA's fixed toolchain is installed into that venv: `pytest`, `coverage`,
   `mutmut==3.5.0`, and `pytest-timeout`. Target tests run with that venv's
   interpreter.
3. `sys.executable` is never the verifier interpreter. If the configured
   `python3` cannot create a venv, the run fails with an actionable
   `setup_failed` / `missing_python_runtime` rather than borrowing UTA's venv.
   `UTA_SERVICE_PYTHON_BIN` may be used as an alternative *interpreter* for
   creating the venv; it is not a license to import UTA's site-packages.
4. When there is no nested manifest, do not create an empty venv that hides
   system packages. Use the configured `python3` only. If it lacks pytest or
   coverage, fail `missing_pytest` / `missing_coverage`. Do not fall back to
   `sys.executable`.
5. Delete, do not patch: `--target` overlay, overlay-on-PYTHONPATH as the
   isolation mechanism, `_select_overlay_runtime` as "pick an interpreter that
   already has pytest", `PYTEST_DISABLE_PLUGIN_AUTOLOAD`,
   `UTA_PYTHON_DEPENDENCY_INDEX_URL` as a UTA-specific index override, and
   the optional-plugin re-injection that exists only because autoload was
   disabled. A real venv inherits pip's configured index.
6. Keep: nearest-nested-manifest discovery, flatten of `-r` includes,
   digest-keyed cache, flock, skip-unavailable at the whole-manifest level
   (never cache an empty environment), and `setup_command` as the escape
   hatch. Cache key is `(flattened manifest sha, interpreter identity)`.
7. A full manifest install that fails wholesale is a `setup_failed` with the
   pip error in evidence. That is preferred to a silently under-provisioned
   overlay and a wrong gate verdict.
8. The lightweight binding (`uta_py_enforce`) and full UTA verification share
   this environment builder. Do not grow a second overlay/venv implementation
   in `runtime_setup.py`.
9. UTA's import-roots plugin may still be loaded via `PYTEST_PLUGINS` by
   putting only the `uta_py_enforce` package root on `PYTHONPATH`. UTA's
   site-packages directory must not appear on that path.

## Tech Stack

- Python 3 verifier (`python3 -m venv`), not Python 2
- `pip` for resolution
- Existing UTA toolchain pins: `mutmut==3.5.0`; pytest and coverage as
  currently used by enforcement
- Tests: existing pytest suite under `tests/`
- Cache location: remain under the target repo's
  `{artifact_dir}/dependencies/{digest[:16]}`

## Commands

```
# unit tests for the overlay/venv and runtime isolation
python3 -m pytest tests/test_python_dependency_overlay_promotion.py tests/test_python_verification.py tests/test_binding_pytest_import_roots.py tests/test_python_standalone_enforcer.py -q

# lightweight enforcement path
python3 -m pytest tests/test_python_standalone_enforcer.py tests/test_binding_pytest_import_roots.py -q

# standalone workflow gate
python3 /home/user/.grok/skills/using-agent-skills/scripts/uta_dev_gate.py pre-build --non-uta
python3 /home/user/.grok/skills/using-agent-skills/scripts/uta_dev_gate.py pre-review --non-uta
```

Beta replay (after the branch is on the beta node): reuse production CI task
`f12aec735b1840c18051263bc614d92c` / app `mmc_app_voice_robot` /
branch `TASK-41000-20260831` as the verification fixture. Record the new
beta task id and whether the environment no longer reports UTA-venv leakage
(`langsmith` plugin, ABI mismatch, empty overlay).

## Project Structure

```
tools/python-enforcement/uta_py_enforce/dependency_overlay.py  → canonical environment builder
tools/python-enforcement/uta_py_enforce/api.py                 → lightweight enforce uses venv python
tools/python-enforcement/uta_py_enforce/runtime.py             → interpreter resolution, no sys.executable
tools/python-enforcement/uta_py_enforce/pytest_env.py          → import roots only; no autoload disable
uta/language/python/verification/runtime_setup.py              → adapter: call the builder, switch python_bin
uta/language/python/verification/runtime_config.py             → fallback policy
uta/language/python/verification/pytest_execution.py           → pytest env without autoload disable
uta/language/python/verification/dependency_requirements.py    → facade only
tests/test_python_dependency_overlay_promotion.py              → venv create/install/cache tests
tests/test_python_verification.py                              → runtime isolation tests
docs/spec-python-per-target-venv.md                            → this spec
docs/design-python-per-target-venv.md                          → design after approval
docs/decisions/ADR-016-python-per-target-venv.md               → decision record
docs/usage-python-per-target-venv.md                           → operator notes
```

## Code Style

Match the existing overlay module: module docstring states who owns
resolution (pip, not UTA); functions return evidence dicts; cache is
flock-guarded; failures are recorded, not swallowed.

```python
# Good: venv python is the only interpreter after prepare
evidence, venv_dir, digest = prepare_dependency_overlay(...)
python_bin = venv_python(venv_dir)  # venv_dir/bin/python
# coverage / pytest / mutmut all use python_bin
# PYTHONPATH is not the isolation mechanism

# Bad: overlay on PYTHONPATH, host python still visible
env["PYTHONPATH"] = str(overlay_dir)
runner([sys.executable, "-m", "pytest", ...])
```

## Testing Strategy

Prove-it tests first, then implementation.

Must-fail-before-fix cases:

1. A nested manifest is installed as a whole (`pip install -r`), not as a
   selected subset. Already true on `main`; keep it.
2. Isolation is a venv, not `--target`. Subsequent pytest/coverage commands
   use `venv/bin/python`, not the host `python3` and not `sys.executable`.
3. Cache is keyed by `(manifest, interpreter)` and is flock-guarded. An
   invalid marker rebuilds. An index that serves nothing does not cache an
   empty environment.
4. `_python_runtime_fallback_bin` / lightweight `resolve_python_runtime` never
   return `sys.executable`.
5. Pytest plugin autoload is not disabled once the venv exists. Target
   plugins declared in the manifest load; UTA's `langsmith` / `anyio` plugins
   do not, because they are not in the venv.
6. `setup_command`, when set, still runs instead of the automatic venv and
   still fails the gate on non-zero.

Do not add live `pip install` network tests. Fake the runner as today's
overlay tests do; assert the argv (`-m venv`, then venv pip `-r`, then
toolchain install) and the interpreter used afterwards.

## Boundaries

- Always: keep the language-agnostic core (workflow, task, report, progress,
  cost) untouched. Python runtime stays in `uta/language/python` and
  `uta_py_enforce`. Preserve unrelated local edits and `.uta_reports` /
  `.uta_cache`.
- Always: fail loudly when the environment cannot be built.
- Ask first: changing skip-unavailable into a hard fail on the first missing
  distribution; adding a new UTA-owned pip index env var; creating a venv
  when there is no nested manifest.
- Never: fall back to `sys.executable` as the verifier. Revive the import
  scan, alias table, or token matching. Install overlay packages into UTA's
  own environment. Deploy this branch to production in this iteration.

## Success Criteria

- [ ] Nested-manifest targets run pytest/coverage/mutmut with a venv
      interpreter created from that manifest plus the UTA toolchain.
- [ ] No verification path uses `sys.executable` as the target interpreter
      when `python_bin` is at its default.
- [ ] `--target` overlay, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, and
      `UTA_PYTHON_DEPENDENCY_INDEX_URL` are gone.
- [ ] Empty-environment cache is still refused. Skip-unavailable still
      drops a single missing distribution and still fails when nothing
      remains.
- [ ] Overlay/runtime tests above pass.
- [ ] Beta replay of `mmc_app_voice_robot` task
      `f12aec735b1840c18051263bc614d92c` is recorded with the new task id
      and whether the verifier environment is isolated.

## Open Questions

None that block the spec. The production session's first error is
`ModuleNotFoundError: No module named 'wechatlet_ai.access_guard'`, which
is a local import-root issue as well as an environment issue. The venv
change must not regress import-roots (`UTA_PYTEST_IMPORT_ROOTS` /
`uta_py_enforce.pytest_import_roots_plugin`). Beta replay is what tells
us whether isolation plus existing import-roots is enough for this target.
