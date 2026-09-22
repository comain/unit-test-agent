# Implementation Plan: OpenCode Provider Fallback

Jira: N/A, confirmed non-Jira tool work.
Spec: `docs/spec-opencode-provider-fallback.md`
Design: `docs/design-opencode-provider-fallback.md`
Usage: `docs/usage-opencode-provider-fallback.md`
Release approval: N/A.

## Overview

Implement provider-chain based OpenCode routing where the chain is the single model-selection source. UTA should select the first configured candidate when fallback is disabled, advance through provider/model candidates only for narrow availability failures, record routing metadata for investigation, and stop/resume the task so the next run continues with the next candidate.

## Architecture Decisions

- Provider chain is canonical: standalone model env vars are no longer model-selection inputs for the new path.
- Token mapping uses `provider.token` entries so each provider receives only its own token.
- Model availability cache is process-local for v1.
- Cheap/small model routing converges on the selected provider-chain model.
- Fallback is task-level, not turn-level: availability failure stops and resumes the task instead of retrying the same prompt inside the same running task.
- No DB schema migration in v1: use task config snapshots plus task events.

## Dependency Graph

```text
Config parser and token resolver
  -> Provider-chain router and process-local availability cache
    -> OpenCode config generation for all chain candidates
    -> OpenCode process env/token injection
    -> OpenCode availability-error classification
      -> Task metadata/events
        -> Stop/resume fallback behavior
          -> CLI/daemon integration and node2 verification
```

## Task List

### Phase 1: Routing Foundation

#### Task 1: Add Provider Chain Config Contract

**Description:** Add settings and parsing helpers for `UTA_OPENCODE_PROVIDER_CHAIN`, `UTA_OPENCODE_PROVIDER_TOKENS`, fallback enablement, and model API cache/probe settings.

**Acceptance criteria:**
- [x] Provider chain parses provider order and model order deterministically.
- [x] Invalid chain entries are ignored safely.
- [x] `provider.token` values map to provider ids without exposing token values in returned metadata.
- [x] Fallback-disabled selection returns only the first configured candidate.

**Verification:**
- [x] `python3 -m pytest tests/test_tiered_routing.py`
- [x] New parser tests cover valid chain, invalid entries, missing tokens, and fallback disabled.

**Dependencies:** None.

**Files likely touched:**
- `uta/config.py`
- `uta/opencode/tiered_router.py`
- `tests/test_tiered_routing.py`

**Estimated scope:** M.

#### Task 2: Replace Cheap/Small Routing With Chain-Selected Model

**Description:** Update routing so compile-fix and other cheap/small phases use the same provider-chain-selected model as main generation.

**Acceptance criteria:**
- [x] `effective_model("compile_fix")` and generation phases return the same selected chain candidate.
- [x] Existing cheap-model env behavior no longer overrides provider-chain selection.
- [x] Tests document the new behavior explicitly.

**Verification:**
- [x] `python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py`

**Dependencies:** Task 1.

**Files likely touched:**
- `uta/opencode/tiered_router.py`
- `tests/test_tiered_routing.py`
- `tests/test_tiered_routing_resilience.py`

**Estimated scope:** S.

### Checkpoint: Routing Foundation

- [x] Chain parser and selected-model tests pass.
- [x] No token value appears in test output, task metadata fixtures, or logs.
- [x] Env format is documented before process/config integration.

### Phase 2: OpenCode Runtime Integration

#### Task 3: Generate OpenCode Config From Provider Chain

**Description:** Update `opencode.json` generation to register every configured provider/model candidate from the provider chain and set main/small model fields to the selected candidate.

**Acceptance criteria:**
- [x] Generated `opencode.json` contains all chain provider model definitions.
- [x] `model` and `small_model` point to the same selected candidate.
- [x] Provider-specific options remain intact for `token-pool`, native `openai`, OpenAI-compatible `openai`, and `deepseek`.
- [x] Provider-chain token values are not written to `opencode.json`.

**Verification:**
- [x] `python3 -m pytest tests/test_opencode_config.py`

**Dependencies:** Task 1, Task 2.

**Files likely touched:**
- `uta/opencode/config.py`
- `tests/test_opencode_config.py`

**Estimated scope:** M.

#### Task 4: Inject Provider-Specific Tokens Into OpenCode Process Env

**Description:** Update process environment construction so the selected provider gets its matching `provider.token` value using the provider's expected env variable without leaking unrelated provider tokens.

**Acceptance criteria:**
- [x] Selected `deepseek` candidate receives only the DeepSeek token env.
- [x] Selected `token-pool` candidate receives only the token-pool token env.
- [x] Native `openai` OAuth still clears `OPENAI_API_KEY` unless explicitly using OpenAI-compatible API key config.
- [x] No token values are logged or persisted.

**Verification:**
- [x] `python3 -m pytest tests/test_opencode_process.py`
- [x] New env tests assert selected provider token mapping and non-selected token omission.

**Dependencies:** Task 1.

**Files likely touched:**
- `uta/opencode/process.py`
- `tests/test_opencode_process.py`

**Estimated scope:** M.

#### Task 5: Add OpenAI-Compatible Model Availability Probe

**Description:** Add a process-local model probe and TTL cache that filters unavailable configured models when provider model API information is available.

**Acceptance criteria:**
- [x] Supports `data`, `models`, and plain list response shapes.
- [x] Probe failures keep configured models as candidates.
- [x] Cache is process-local and respects configured TTL.
- [x] Missing model API config does not block provider selection.

**Verification:**
- [x] New tests in `tests/test_tiered_routing_resilience.py` using fake responses.
- [x] `python3 -m pytest tests/test_tiered_routing_resilience.py`

**Dependencies:** Task 1.

**Files likely touched:**
- `uta/opencode/tiered_router.py`
- `tests/test_tiered_routing_resilience.py`

**Estimated scope:** M.

### Checkpoint: Runtime Integration

- [x] `python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py tests/test_opencode_process.py tests/test_opencode_config.py`
- [x] Generated `opencode.json` tests confirm model and `small_model` are identical selected chain candidates.
- [x] Token redaction reviewed before task lifecycle work.

### Phase 3: Availability Error Handling And Task Lifecycle

#### Task 6: Classify Fallback-Eligible Provider/Model Errors

**Description:** Extend OpenCode error parsing to distinguish rate-limit, model-disabled, model-unavailable, and model-not-found errors from generic errors, timeouts, and stalls.

**Acceptance criteria:**
- [x] Rate-limit parsing keeps existing structured payload behavior.
- [x] Disabled/unavailable/not-found model messages are classified as fallback-eligible.
- [x] Timeout, stalled, generic command error, unsafe diff, budget, compile, and test failures are not fallback-eligible.
- [x] `TurnResult` exposes safe classification metadata without token leakage.

**Verification:**
- [x] `python3 -m pytest tests/test_opencode_process.py`
- [x] New tests cover positive and negative classifications.

**Dependencies:** Task 4.

**Files likely touched:**
- `uta/opencode/process.py`
- `uta/opencode/rate_limit.py`
- `tests/test_opencode_process.py`

**Estimated scope:** M.

#### Task 7: Persist Routing Metadata And Fallback Events

**Description:** Store selected candidate, token-presence status, probe result, fallback history, and task events using existing JSON fields/events.

**Acceptance criteria:**
- [x] Task config snapshot records provider chain, selected provider/model, candidate index, and token presence only.
- [x] Fallback history is capped to avoid unbounded JSON growth.
- [x] Task events include `opencode_model_selected`; fallback event emission lands with stop/resume wiring.
- [x] No DB schema migration is needed.

**Verification:**
- [x] `python3 -m pytest tests/test_tasks.py`
- [x] New task metadata tests assert token values are absent.

**Dependencies:** Task 1, Task 5, Task 6.

**Files likely touched:**
- `uta/tasks/manager.py`
- `uta/tasks/db.py` only if helper methods are needed, not schema changes.
- `tests/test_tasks.py`

**Estimated scope:** M.

#### Task 8: Stop And Resume Tasks On Fallback-Eligible Errors

**Description:** Wire graph-node provider failures into cooperative task stop/resume so the next daemon run selects the next candidate.

**Acceptance criteria:**
- [x] Production task encountering fallback-eligible error becomes `STOPPED` then `QUEUED`.
- [x] Resume event records the failed candidate and next candidate selection context.
- [x] No immediate same-turn prompt retry happens.
- [x] When all candidates are exhausted, task fails terminally instead of looping forever.

**Verification:**
- [x] `python3 -m pytest tests/test_daemon_preemption.py tests/test_daemon_retry.py tests/test_tasks.py`
- [x] New task lifecycle tests simulate provider failure and assert stop/resume behavior.

**Dependencies:** Task 6, Task 7.

**Files likely touched:**
- `uta/graph/nodes.py`
- `uta/tasks/manager.py`
- `tests/test_workflow.py`
- `tests/test_tasks.py`

**Estimated scope:** M.

### Checkpoint: Lifecycle Complete

- [x] Focused OpenCode tests pass.
- [x] Task lifecycle tests pass.
- [x] Simulated fallback tests show metadata, stop, resume, and next candidate.
- [x] Exhausted-candidate behavior fails terminally before CLI/daemon integration.

### Phase 4: CLI, Daemon, Documentation, And Deployment

#### Task 9: Integrate Provider Chain Into CLI/Daemon Startup

**Description:** Ensure task creation/resume snapshots and daemon heartbeats use provider-chain selection and config hashes that reflect routing state.

**Acceptance criteria:**
- [x] New production tasks record provider-chain config snapshot.
- [x] Resumed tasks select the next candidate based on metadata.
- [x] Daemon heartbeat config hash changes when provider chain changes.
- [x] Existing Java and Python entry points both use the same selected model contract.

**Verification:**
- [x] `python3 -m pytest tests/test_cli.py tests/test_python_batch_generation.py tests/test_daemon_retry.py`

**Dependencies:** Task 8.

**Files likely touched:**
- `uta/cli.py`
- `uta/language/python/batch.py`
- `uta/language/java/batch.py`
- `tests/test_cli.py`

**Estimated scope:** M.

#### Task 10: Update Operational Documentation

**Description:** Keep spec/design/usage aligned with implementation details and update README or RDC usage docs only if operator-facing behavior must be mirrored there.

**Acceptance criteria:**
- [x] `docs/spec-opencode-provider-fallback.md` reflects final env names and acceptance criteria.
- [x] `docs/design-opencode-provider-fallback.md` reflects actual implementation and rollback path.
- [x] `docs/usage-opencode-provider-fallback.md` includes exact env examples and investigation commands.
- [x] README docs are updated for operator discoverability.

**Verification:**
- [x] `rg -n "UTA_OPENCODE_PROVIDER_CHAIN|UTA_OPENCODE_PROVIDER_TOKENS" docs README.md`
- [x] Documentation review confirms no secrets or local-only absolute paths were added.

**Dependencies:** Tasks 1-9.

**Files likely touched:**
- `docs/spec-opencode-provider-fallback.md`
- `docs/design-opencode-provider-fallback.md`
- `docs/usage-opencode-provider-fallback.md`
- `README.md` only if needed.

**Estimated scope:** S.

#### Task 11: Final Verification And Node2 Rollout

**Description:** Run local full verification, deploy to node2 through git pull, enable env conservatively, and validate controlled fallback behavior.

**Acceptance criteria:**
- [x] Full local test suite passes or any skipped/failing tests are documented with cause.
- [x] Node2 deploy follows git-pull workflow only.
- [x] Fallback disabled uses first provider-chain candidate.
- [x] Controlled fallback task records metadata, stops, resumes, and continues with next candidate.
- [x] Rollback instructions are validated by switching fallback off and confirming first-candidate behavior.

**Verification:**
- [x] `python3 -m pytest`
- [x] Node2 `curl -fsS http://127.0.0.1:8001/unit-test/readyz`
- [x] Public node2 health/readiness URL still works.
- [x] Controlled task event inspection via `uta tasks show <task-id>`.

**Evidence:**
- Local full suite: `923 passed, 9 skipped in 62.68s`.
- Node2 deploy: `/opt/app/unit_test_agent` fast-forwarded by `git pull --ff-only origin main` from `933df24` to `4febdb6`; no rsync used.
- Node2 service: restarted with `scripts/start_api_trigger.sh`, then `readyz` returned `runner.ready=true`.
- Public URL: `http://ci.example.com/unit-test/healthz` and `/readyz` returned `service.status=ok`.
- Controlled fallback: fallback disabled returned only the first candidate, `token-pool/gpt-5.5`; fallback enabled recorded `opencode_model_unavailable`, `task_stopped`, `task_resumed`, and selected `openai/gpt-5.4`.

**Dependencies:** Task 10.

**Files likely touched:** None expected beyond docs if evidence notes are recorded.

**Estimated scope:** M.

### Checkpoint: Ready For Review

- [x] All planned tests pass.
- [x] Docs match shipped behavior.
- [x] Node2 verification evidence is recorded.
- [x] No token values appear in code, logs, docs, task metadata, or reports.
- [ ] Human review approves production enablement.

## Parallelization Opportunities

- Tasks 3 and 4 can proceed in parallel after Task 1 if the selected-candidate contract is stable.
- Task 5 can proceed in parallel with Task 3 and Task 4 after Task 1.
- Task 10 documentation can start after Task 8, then be finalized after Task 9.
- Task 11 must be sequential after implementation and docs.

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Stop/resume loop when every candidate fails | High | Track exhausted candidates and fail terminally when no candidate remains. |
| Token leakage in metadata/logs/docs | High | Store token status only, add tests that assert token strings are absent. |
| Native OpenAI OAuth behavior regresses | Medium | Keep OAuth-specific env sanitization tests and avoid forcing API-key mode. |
| Provider model probe outage blocks all work | Medium | Treat probe failures as warnings and keep configured candidates. |
| Chain config syntax is hard to operate | Medium | Document exact examples and add parser error warnings. |
| Cheap/small routing removal surprises existing tuning | Low | Make chain order the tuning mechanism and document the behavior. |

## Open Questions

- Confirm provider token env syntax exactly as `UTA_OPENCODE_PROVIDER_TOKENS='token-pool.token=...;openai.token=...;deepseek.token=...'`.
- Confirm token-pool runtime env variable name expected by OpenCode or the gateway before implementing token injection.
