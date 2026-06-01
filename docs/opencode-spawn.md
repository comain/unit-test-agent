# Migration Plan: Programmatic OpenCode Spawning & Event Streaming

## 1. Executive Summary
Currently, `unit-test-agent` (UTA) connects to a pre-running OpenCode server via HTTP. It uses inefficient polling (fetching the entire message history every 3 seconds) to detect completions and resorts to brittle disk-log scraping to detect rate limits. 

This plan details the migration to a **Process Spawning & Streaming** architecture (inspired by Cloudflare's approach). UTA will programmatically spawn OpenCode as a child process and consume its `stdout` as a structured JSONL stream.

## 2. Architectural Benefits
1. **Zero-Latency Reactions:** Replaces the 3-second HTTP polling loop with instant, event-driven stream parsing.
2. **Reliable Error Detection:** Eliminates the disk-based log scraping hack (`detect_rate_limit_issue`) by capturing 429s and provider errors directly from the JSONL stream.
3. **Perfect Isolation & Concurrency:** UTA binds OpenCode to a dynamic port for each run, allowing parallel UTA runs on the same machine without port conflicts.
4. **Guaranteed Cleanup:** The Python parent process guarantees the OpenCode child is killed upon exit, preventing zombie background processes.
5. **Real-time Telemetry:** Token usage is accumulated seamlessly from the stream, deprecating the heavy post-run `analyze_session_tokens` method.

---

## 3. Execution Phases

### Phase 1: OpenCode Server Lifecycle Management
**Goal:** Enable Python to boot and kill OpenCode reliably with full plugin support.
*   **Action:** 
    *   Create `uta.opencode.process.OpenCodeServer`. 
    *   Use `socket` to find an ephemeral free port. 
    *   **Environment Propagation:** Ensure `subprocess.Popen(..., env=os.environ.copy())` is used so that API keys and plugin-specific variables are passed to the child.
    *   **Custom Spawn Command:** Implement `settings.opencode_spawn_cmd` (List[str]) to allow users to specify exactly how to run OpenCode (e.g., `["bun", "run", "src/index.ts"]` or a path to a global binary). This is critical for loading external plugins like `cursor-auth`.
    *   **Working Directory:** Support setting the CWD for the spawned process to ensure `node_modules` and local plugins are resolvable.
*   **Verification (Phase 1 Gate):**
    *   **Unit Test:** `test_server_spawns_on_random_port` — Verify port binding and process existence.
    *   **Unit Test:** `test_env_propagation` — Verify that specific env vars (e.g., `ANTHROPIC_API_KEY`) are visible to a mock child process.
    *   **Integration Test:** `test_server_teardown` — Verify that calling `.stop()` or exiting the context manager kills the child process.
    *   **Integration Test:** `test_custom_command_execution` — Verify UTA can spawn a simple shell script via `opencode_spawn_cmd`.
    *   **Test Command:** `pytest tests/test_opencode_process.py`

### Phase 2: Event-Driven Stream Consumption
**Goal:** Parse OpenCode output in real-time.
*   **Action:** Attach a background thread to `stdout`. Parse JSONL lines and route them to an internal event queue.
*   **Verification (Phase 2 Gate):**
    *   **Unit Test:** `test_stream_parser_handles_malformed_json` — Ensure the reader doesn't crash on non-JSON lines (e.g., plugin initialization logs).
    *   **Unit Test:** `test_event_routing` — Feed a mock `stdout` with `step-finish`, `tool`, and `metric` lines and verify the internal queue/state is updated correctly.
    *   **Test Command:** `pytest tests/test_opencode_stream.py`

### Phase 3: Client Refactoring (`uta/opencode/client.py`)
**Goal:** Rip out HTTP polling and integrate the stream.
*   **Action:** Refactor `poll_completion` to block on the event queue. Remove log-scraping and post-run metric methods.
*   **Verification (Phase 3 Gate):**
    *   **Mock Test:** `test_poll_completion_blocks_until_event` — Verify `poll_completion` returns immediately upon receiving a `completed` event from the stream.
    *   **Integration Test:** `test_realtime_metrics_accumulation` — Run a short generation and verify `AgentState["session_token_usage"]` is populated *during* the run.
    *   **Test Command:** `pytest tests/test_opencode_client_streaming.py`

### Phase 4: Active Circuit Breaking
**Goal:** Improve resilience using instant error detection.
*   **Action:** Update `tiered_router.py` to track model health state.
*   **Verification (Phase 4 Gate):**
    *   **Unit Test:** `test_router_switches_on_429` — Mock a 429 event in the stream and verify that the *next* `effective_model()` call returns the fallback model.
    *   **Integration Test:** `test_workflow_recovery_after_provider_error` — Verify the LangGraph continues to the next phase using the fallback model (e.g., failing over from `cursor` to `gpt-4o`).
    *   **Test Command:** `pytest tests/test_tiered_routing_resilience.py`

### Phase 5: Dev Mode (Hybrid Support)
**Goal:** Maintain fast local development.
*   **Action:** Support `UTA_OPENCODE_DEV_PORT` to bypass spawning.
*   **Verification (Phase 5 Gate):**
    *   **Unit Test:** `test_dev_mode_skips_spawn` — Ensure that if the env var is set, `OpenCodeServer` connects to the specified port instead of spawning.
    *   **Test Command:** `pytest tests/test_opencode_config.py`

---

## 4. Rollout Strategy & Compliance
1.  **Strict Gating:** No phase shall be merged into the main branch until all associated Phase Gate tests pass.
2.  **Plugin Compatibility:** Before completing Phase 1, manually verify that the `cursor` provider (via `cursor-auth`) correctly initializes when spawned by UTA using a local `bun run` command.
3.  **Documentation:** Update `README.md` to reflect the new `opencode_spawn_cmd` setting and how to configure it for different installation types (npm global, bun local, etc.).
