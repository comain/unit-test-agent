# Todo: OpenCode Provider Fallback

Spec: `docs/spec-opencode-provider-fallback.md`
Design: `docs/design-opencode-provider-fallback.md`
Usage: `docs/usage-opencode-provider-fallback.md`

## Phase 1: Routing Foundation

- [x] Task 1: Add provider chain config contract.
  - Acceptance: chain parser preserves provider/model order; invalid entries are safe; `provider.token` maps without leaking values; fallback disabled returns only first candidate.
  - Verify: `python3 -m pytest tests/test_tiered_routing.py`
  - Files: `uta/config.py`, `uta/opencode/tiered_router.py`, `tests/test_tiered_routing.py`

- [x] Task 2: Replace cheap/small routing with chain-selected model.
  - Acceptance: compile-fix and generation phases use the same selected chain candidate; tests document removal of separate cheap-model override.
  - Verify: `python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py`
  - Files: `uta/opencode/tiered_router.py`, `tests/test_tiered_routing.py`, `tests/test_tiered_routing_resilience.py`

- [x] Checkpoint: Routing foundation reviewed.
  - Verify: no token values in output; env format approved.

## Phase 2: OpenCode Runtime Integration

- [x] Task 3: Generate OpenCode config from provider chain.
  - Acceptance: all chain providers/models are registered; `model` and `small_model` are identical selected candidate; tokens are not written to `opencode.json`.
  - Verify: `python3 -m pytest tests/test_opencode_config.py`
  - Files: `uta/opencode/config.py`, `tests/test_opencode_config.py`

- [x] Task 4: Inject provider-specific tokens into OpenCode process env.
  - Acceptance: only selected provider token is injected; native OpenAI OAuth behavior remains intact; no token values persist.
  - Verify: `python3 -m pytest tests/test_opencode_process.py`
  - Files: `uta/opencode/process.py`, `tests/test_opencode_process.py`

- [x] Task 5: Add OpenAI-compatible model availability probe.
  - Acceptance: parses supported response shapes; probe failure is non-fatal; cache is process-local TTL.
  - Verify: `python3 -m pytest tests/test_tiered_routing_resilience.py`
  - Files: `uta/opencode/tiered_router.py`, `tests/test_tiered_routing_resilience.py`

- [x] Checkpoint: Runtime integration reviewed.
  - Verify: `python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py tests/test_opencode_process.py tests/test_opencode_config.py`

## Phase 3: Availability Error Handling And Task Lifecycle

- [x] Task 6: Classify fallback-eligible provider/model errors.
  - Acceptance: rate-limit, model-disabled, model-unavailable, model-not-found are fallback eligible; timeouts/stalls/generic errors are not.
  - Verify: `python3 -m pytest tests/test_opencode_process.py`
  - Files: `uta/opencode/process.py`, `uta/opencode/rate_limit.py`, `tests/test_opencode_process.py`

- [x] Task 7: Persist routing metadata and fallback events.
  - Acceptance: task snapshot stores selected provider/model, chain, token status, probe result, capped fallback history; token values absent.
  - Verify: `python3 -m pytest tests/test_tasks.py`
  - Files: `uta/tasks/manager.py`, `uta/tasks/db.py`, `tests/test_tasks.py`

- [x] Task 8: Stop and resume tasks on fallback-eligible errors.
  - Acceptance: task becomes `STOPPED` then `QUEUED`; next run selects next candidate; exhausted candidates fail terminally without looping.
  - Verify: `python3 -m pytest tests/test_daemon_preemption.py tests/test_daemon_retry.py tests/test_tasks.py`
  - Files: `uta/graph/nodes.py`, `uta/tasks/manager.py`, `tests/test_workflow.py`, `tests/test_tasks.py`

- [x] Checkpoint: Lifecycle reviewed.
  - Verify: simulated fallback shows metadata, stop, resume, next candidate.

## Phase 4: CLI, Daemon, Documentation, Deployment

- [x] Task 9: Integrate provider chain into CLI/daemon startup.
  - Acceptance: task snapshots and heartbeat hashes include provider chain; Java/Python entry points use same selected model contract.
  - Verify: `python3 -m pytest tests/test_cli.py tests/test_python_batch_generation.py tests/test_daemon_retry.py`
  - Files: `uta/cli.py`, `uta/language/python/batch.py`, `uta/language/java/batch.py`, `tests/test_cli.py`

- [x] Task 10: Update operational documentation.
  - Acceptance: spec/design/usage reflect final behavior; README updated only if needed; no secrets or local-only absolute paths added.
  - Verify: `rg -n "UTA_OPENCODE_PROVIDER_CHAIN|UTA_OPENCODE_PROVIDER_TOKENS" docs README.md`
  - Files: `docs/spec-opencode-provider-fallback.md`, `docs/design-opencode-provider-fallback.md`, `docs/usage-opencode-provider-fallback.md`, optional `README.md`

- [x] Task 11: Final verification and the server rollout.
  - Acceptance: full tests pass; the server deploy uses git pull; fallback-disabled first-candidate behavior and controlled fallback are verified.
  - Verify: `python3 -m pytest`; the server readyz; controlled task event inspection with `uta tasks show`.
  - Evidence: local full suite `923 passed, 9 skipped`; the server git pull from `933df24` to `4febdb6`; public health/readyz green; controlled fallback selected `openai/gpt-5.4` after recording stop/resume events.
  - Files: none expected unless evidence notes are recorded.

- [x] Checkpoint: Ready for review.
  - Verify: all tests pass, docs match behavior, the server evidence recorded, no token leaks.
