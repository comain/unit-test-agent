# Unit Test Agent (UTA)

UTA is a Python CLI that generates and repairs unit tests for Java and Python
repositories with help from OpenCode, static analysis, and deterministic
verification gates.

## What It Does

- Selects test targets from git history, explicit target flags, or all source files.
- Builds compact source context with tree-sitter and language adapters.
- Asks OpenCode to plan, generate, and repair tests in separate focused sessions.
- Runs compile, test, coverage, and mutation checks.
- Produces JSON and terminal reports with timing, token, coverage, and mutation data.
- Optionally runs as a CI-plugin service that accepts repository/branch payloads and
  reports queued, running, passed, failed, timeout, or missing-evidence outcomes.

## Project Layout

- `uta/`: CLI, orchestration, reporting, provider integration, and CI-plugin code.
- `uta/engine/`: language-neutral workflow, target, report, progress, cost, and
  enforcement contracts.
- `uta/language/java/`: Java parsing, context building, Maven/JUnit verification,
  coverage, mutation, and CI adapters.
- `uta/language/python/`: Python parsing, context building, pytest verification,
  coverage, mutation, and CI adapters.
- `scripts/`: local runner, daemon, deployment, and benchmark helpers.
- `config/`: public example config files. Machine-specific config belongs outside
  source control or in ignored local files.
- `docs/`: public design and usage notes.
- `tests/`: unit and integration tests.

## Installation

```bash
python -m pip install -e .
```

UTA expects Python 3.9+ and an `opencode` executable in `PATH`.

## Basic Usage

```bash
# Scan recent changes and generate tests for selected Java targets.
uta run --repo ~/src/sample-service --module service --days 30

# Process all production Java files in a module.
uta run --repo ~/src/sample-service --module service --all

# Target exact Java classes.
uta run --repo ~/src/sample-service --module service \
  --class-fqn com.example.service.OrderService \
  --class-fqn com.example.service.InvoiceService

# Run a Python target.
uta run --repo ~/src/python-job --language python --target jobs/forecast.py

# Inspect an OpenCode session after a run.
uta assess --session-id ses_candidate --json-output
```

## Configuration

Runtime configuration is loaded from environment variables and `.env` through
`pydantic-settings`. Do not commit `.env` files.

Useful variables:

- `UTA_OPENCODE_PROVIDER_CHAIN`: provider/model fallback chain, for example
  `openai:openai/gpt-5.5;deepseek:deepseek/deepseek-chat`.
- `UTA_OPENCODE_PROVIDER_TOKENS`: provider token mapping, for example
  `openai.token=${OPENAI_API_KEY}`.
- `UTA_OPENCODE_PROVIDER_BASE_URLS`: base URLs for OpenAI-compatible providers.
- `UTA_INDEX_SOURCE_DIRS`: comma-separated sibling source roots used by the
  source index, for example `~/src/shared-api,~/src/platform-libs`.
- `UTA_OPENCODE_EXTERNAL_DIRS`: comma-separated directories OpenCode may read in
  headless mode.
- `UTA_CI_WORKSPACE_ROOT`: workspace root for CI-triggered checks.
- `UTA_CI_PUBLIC_BASE_URL`: public URL prefix for status/report links.
- `UTA_CI_ALLOWED_GIT_HOSTS`: comma-separated git hosts allowed by the CI workspace
  manager.

For headless OpenCode external-directory permissions, copy
`config/opencode_external_dirs.example.json` to `config/opencode_external_dirs.json`
or set `UTA_OPENCODE_EXTERNAL_DIRS`. The real `config/opencode_external_dirs.json`
file is ignored so local allowlists do not leak into public source.

## CI Plugin

The optional CI-plugin service exposes:

- `POST /api/v1/ci/trigger`
- `GET /task-status/{taskId}`
- `GET /task-status/{taskId}/data`
- `GET /reports/{taskId}/index.html`
- `GET /reports/{taskId}/detail`

The trigger payload accepts repository URL, branch, task id, record id, optional
issue id, and optional language. It returns a status envelope with queued/running
links.

## OpenCode Sessions

UTA intentionally splits work into multiple sessions:

1. `plan`: choose the public methods, branch axes, and first-pass test cases.
2. `generate`: write the initial test file.
3. `compile-fix`: repair compile failures.
4. `test-fix`: repair generated tests that fail at runtime.
5. `coverage-fix`: improve line coverage from focused coverage output.
6. `mutation-fix`: kill surviving mutants from focused mutation output.

Splitting sessions keeps repair prompts narrow and makes token/cost attribution
clearer in reports.

## Generated Artifacts

UTA writes runtime artifacts under ignored paths such as `.uta_cache/`,
`.uta_reports/`, `.uta_runner/`, and local task databases. These are not intended
for source control.

## License

Apache-2.0
