# Service Configuration

Environment reference for the API trigger service (`uta/app/`). This is
deployment and operations detail; see the README for what the service does and
[docs/rdc-api-trigger-usage.md](rdc-api-trigger-usage.md) for the RDC contract.

## Running the service

Use `scripts/start_api_trigger.sh` to run the FastAPI service. It sources `.env`, defaults to port `8001`, and writes logs to `$UTA_RUNNER_HOME/api_trigger.log`.

## GitHub protocol credentials

The Checks API can only be written by a GitHub App (a personal access token
cannot create check runs), so result reporting mints a short-lived installation
token from the App id + private key. Configure:

- `GITHUB_WEBHOOK_SECRET`: shared secret for webhook signature verification. Until set, the route accepts requests but performs no signature check.
- `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH`: GitHub App credentials used to mint installation tokens. Until both are set, the webhook is parsed but no check run is reported.
- `UTA_GITHUB_API_BASE_URL`: GitHub API base (default `https://api.github.com`; set for GitHub Enterprise).
- `UTA_GITHUB_CHECK_NAME`: check-run name shown in the PR (default `uta/unit-test-enforcement`).
- `UTA_GITHUB_CALLBACK_TIMEOUT_SECONDS` / `UTA_GITHUB_CALLBACK_RETRY_TIMES`: bounded check-run delivery.

The GitHub App needs **Checks: read & write**, **Pull requests: read**, and
**Contents: read** permissions, subscribed to the *Pull request* event.

## Enforcement execution

Check-only enforcement uses:

- `UTA_CI_WORKSPACE_ROOT`: isolated RDC check workspaces. On a single-host deployment, keep this under a shared parent such as `/opt/app/uta-ci-data/workspaces`.
- `UTA_CI_ENFORCEMENT_COMMAND`: UTA test-enforcement command. Plain `mvn test` is rejected. The default includes `-DskipTests=false`, `-Dmaven.test.skip=false`, `-Dmaven.test.failure.ignore=true`, and `-Dsurefire.timeout=900` so inherited Maven defaults cannot skip unit tests, while unrelated existing test failures or stuck forked test JVMs do not stop Maven before the UTA verify-time gate emits evidence. See [docs/test-enforce-usage.md](docs/test-enforce-usage.md) for the embedded UTA usage guide.
- `UTA_CI_ENFORCEMENT_TIMEOUT_SECONDS`: command timeout.
- `UTA_CI_PYTHON_ENFORCEMENT_TIMEOUT_SECONDS`: Python enforcement command timeout. The default is 1800 seconds; UTA also passes this value to `uta python-enforce` as `UTA_PYTHON_GATE_TIMEOUT_SECONDS` so a stuck pytest or mutation process cannot hold the single CI report slot indefinitely.
- `UTA_PYTHON_ENVIRONMENT_RECIPES`: JSON object mapping an exact CI `appName` to a UTA-managed Python test environment. Each recipe declares `python` (the base interpreter), an absolute cached `venv`, a profile name, and the minimal packages required by that application's tests. The selected recipe overrides repository dependency-overlay setup and is snapshotted onto repair tasks, so CI verification, repair verification, and final enforcement use the same runtime.

Example:

```bash
export UTA_PYTHON_ENVIRONMENT_RECIPES='{
  "w_ais_paimian": {
    "python": "/opt/app/unit_test_agent/.venv311/bin/python",
    "venv": "/opt/app/uta-data/python-envs/w_ais_paimian-py311",
    "profile": "paimian-unit-test-py311-v2",
    "packages": [
      "numpy==1.23.5",
      "requests==2.28.2",
      "pytest==8.3.5",
      "coverage==7.6.12",
      "pytest-timeout==2.3.1",
      "python-json-logger==3.3.0",
      "mutmut==3.5.0"
    ]
  }
}'
```
- `UTA_RDC_ACK_URL`: RDC AppTool ack endpoint.
- `UTA_CI_CALLBACK_TIMEOUT_SECONDS` and `UTA_CI_CALLBACK_RETRY_TIMES`: bounded RDC callback delivery.
- `UTA_CI_CONTEXT_RUNTIME_ROOT`: run-scoped RDC context artifact root, outside `.uta_cache`, for example `/opt/app/uta-ci-data/context`.
- `UTA_CI_RECORD_STORE_ROOT`: API trigger report/status records, for example `/opt/app/uta-ci-data/records`.
- `UTA_CI_TASK_DB_PATH`: UTA repair-task SQLite DB for CI-triggered fix sessions, for example `/opt/app/uta-ci-data/runner/uta_tasks.db`.
- `UTA_CI_PUBLIC_BASE_URL`: public URL prefix used in RDC task/report links, for example `http://ci.example.com/unit-test`.
- `UTA_JIRA_RAW_URL`: optional Jira raw Jira endpoint; UTA extracts only `data.fields.description`.
- `UTA_CI_GIT_SSH_KEY_PATH`: optional dedicated SSH private key for UTA clone/fetch/push operations. When set, Git uses `GIT_SSH_COMMAND` with `IdentitiesOnly=yes` so deployment behavior is not tied to the host user's default SSH identity.
- `GIT_AC`: optional GitLab access token for UTA clone/fetch/push operations. When set, UTA rewrites GitLab SSH-style URLs to HTTPS and injects the token through Git's process environment; the token is not written into command arguments or repo remote URLs. This takes precedence over `UTA_CI_GIT_SSH_KEY_PATH`.

The runner classifies deterministic outcomes as passed, failed, timeout, command error, skipped, or missing evidence. Branches with no changed production Java files under `origin/master...HEAD` pass without running Maven because there is no UTA unit-test gate target. A green Maven return code without diff coverage and mutation/PIT evidence is not considered a pass. Once required required diff coverage and mutation/PIT evidence is present, unrelated Maven test failures after that evidence do not fail the gate unless the output contains explicit test-enforcement failure markers. Enforcement result JSON includes `usage_guide`, pointing to the local guide copied from Buffett.

The API trigger exposes `/task-status/{taskId}` for queued/running visibility,
`/task-status/{taskId}/data` for machine-readable status,
`/reports/{taskId}/index.html` for enforcement evidence, and
`/reports/{taskId}/detail` for report JSON. A fix-session progress page opens a
read-only `/reports/{taskId}/fix-sessions/{sessionId}/progress/events` SSE
stream. It resumes with `Last-Event-ID`, closes on the persisted terminal
cursor, and renders deterministic work separately from one tab per agent
session so parallel turns never mix.

Use `scripts/start_api_trigger.sh` to run the FastAPI service. It sources `.env`, defaults to port `8001`, and writes logs to `$UTA_RUNNER_HOME/api_trigger.log`.

**Agent selection**: `UTA_AGENT_HARNESS` names an implementation registered by
agent-core and defaults to `opencode`. UTA's workflow does not import that
implementation. With the default adapter, `UTA_OPENCODE_PROVIDER_CHAIN` remains
the model-selection source. Configure providers left to right, with
comma-separated model fallback within each provider, for example:

```bash
export UTA_OPENCODE_PROVIDER_CHAIN='token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;openai:openai/gpt-5.5;deepseek:deepseek/deepseek-v4-pro'
export UTA_OPENCODE_PROVIDER_TOKENS='token-pool.token=${TOKEN_POOL_API_KEY};openai.token=${OPENAI_API_KEY};deepseek.token=${DEEPSEEK_API_KEY}'
export UTA_OPENCODE_PROVIDER_BASE_URLS='token-pool.base_url=https://token-pool.example/v1;openai.base_url=https://api.openai.com/v1;deepseek.base_url=https://api.deepseek.com/v1'
export UTA_OPENCODE_PROVIDER_FALLBACK_ENABLED=true
```

When fallback is disabled, UTA uses the first configured provider/model. `model` and `small_model` are both set to the selected provider-chain model.

Supported provider families:

- `openrouter/*` via `OPENROUTER_API_KEY`
- `google/*` via `GEMINI_API_KEY`
- `openai/*` via native OpenCode `/connect`
- `cursor/*` or plain Cursor model names (for example `gpt-5`) via OpenCode's configured Cursor provider and `UTA_OPENCODE_PROVIDER=cursor`
- `tencent/*` or plain Tencent model names (for example `glm-5`) via `TENCENT_API_KEY` and `UTA_OPENCODE_PROVIDER=tencent`
- `ollama/*` via local Ollama and optional `OLLAMA_HOST`

**Cursor auth**: UTA registers configured Cursor models in the workspace OpenCode config, but it does not install plugins or modify the user's global OpenCode config. Authentication and any provider plugin setup remain owned by the OpenCode installation.

**OpenAI / ChatGPT subscription auth**: when the selected provider-chain model uses `openai/*` without an explicit `openai.token`, UTA preserves native OpenCode OAuth behavior and strips inherited `OPENAI_API_KEY` from spawned OpenCode processes.

**Classes per agent run** (`--classes-per-run`, alias `--batch-size`, default 1; environment variable `UTA_CLASSES_PER_AGENT_RUN`): number of candidate classes included in a single generation prompt. Values greater than 1 reuse the same OpenCode session and context export for the whole batch, which usually shortens end-to-end time at the cost of larger prompts and more work per response. In production task mode, `UTA_SMART_BATCHING_ENABLED=true` batches simple classes up to `UTA_SMART_SIMPLE_BATCH_SIZE` (default `3`) even when this generic value remains `1`, while complex classes still run alone. If you previously set `UTA_BATCH_SIZE` in `.env`, rename that key to `UTA_CLASSES_PER_AGENT_RUN`.

**Explicit class override** (`--class-fqn`, repeatable): bypass git-history candidate selection and run UTA against the exact class FQNs you provide. This is useful for A/B comparisons across prompt/workflow changes because the candidate set stays stable even when recent git activity changes.

**All-files selection** (`--all`): bypass git-history ranking and consider every production Java file under the selected repo/module. This is useful for selector audits and one-time backfills. `--max-files` is ignored in this mode; explicit `--class-fqn` still takes precedence.

**Project context for the agent**: after parsing, UTA writes cached guidance under `.uta_cache/context/`, including:
- `project_summary.md`: Maven + graph statistics
- `test_generation_guidance.md`: cached source-of-truth lookup order, test-construction constraints, and observed repo test patterns
- `session_retrospect.md`: post-run prompt/workflow hints mined from the latest OpenCode session

UTA also preserves the raw slash-init transcript at `.uta_cache/context/opencode_init_output.md` when `/init` is attempted. By default, slash-init is **disabled** (`UTA_OPENCODE_INIT_SLASH_ENABLED=false`) because some OpenCode builds return only a generic task summary and can leave the main build session in a bad state for the next prompt. If you explicitly enable it and `.uta_summary.md` is still missing or nearly empty, UTA will try `/init` and harvest `AGENTS.md`, `CLAUDE.md`, or `.opencode/AGENTS.md` into `.uta_summary.md` when those files are actually created. Timeout: **`UTA_OPENCODE_INIT_SLASH_TIMEOUT`** (default 900s). If slash-init is disabled or nothing is harvested, **`UTA_OPENCODE_INIT_COMMAND`** may still run as a shell fallback; otherwise UTA may create its own stub during `parse_context`.

**Agent progress streaming**: during long agent turns, agent-core projects
provider-neutral activity into bounded, public-safe events. The report shows
the 13 stable generation phases, safe reasoning synopses and tool activity,
status, and diagnostics without persisting raw reasoning, commands, prompts,
tool output, absolute paths, or provider errors. Progress is sampled and
batched; task row/byte caps emit one `progress_truncated` marker without
changing operation truth.

Toggle the session-message stream with `UTA_OPENCODE_STREAM_PROGRESS` (default `true`).

**Generation timeout control**: the generation turn timeout is `40 minutes * batch size * UTA_OPENCODE_GENERATION_TIMEOUT_RATIO` with a minimum of `300s`. If generation times out, UTA now records `GENERATION_TIMEOUT`, writes `session_retrospect.md`, and keeps the stalled session learnings for later runs.

**Headless OpenCode external-directory permissions**: UTA writes the `permission.external_directory` section of each generated `opencode.json` from [config/opencode_external_dirs.json](/path/to/unit-test-agent/config/opencode_external_dirs.json). Keep machine- or workspace-specific allow patterns there so headless runs can access known sibling repos without triggering interactive permission prompts. Temp scratch paths are still allowed automatically.

**Reports**: UTA report JSON now includes:
- per-project summary metrics
- per-file metric tables
- phase timing details
- mutation breakdown buckets (`KILLED`, `NO_COVERAGE`, etc.)
- session retrospect hints mined from the OpenCode transcript
