#!/usr/bin/env bash
set -euo pipefail

# Reusable benchmark runner for a single Java class.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="${TS:-$(date +%Y%m%d_%H%M%S)}"

REPO="${REPO:-$HOME/src/sample-service}"
MODULE="${MODULE:-service}"
CLASS_FQN="${CLASS_FQN:-com.example.service.SampleService}"
COVERAGE_GATE="${COVERAGE_GATE:-80}"
MUTATION_GATE="${MUTATION_GATE:-70}"
MAX_FILES="${MAX_FILES:-1}"

OPENCODE_SRC_DIR="${OPENCODE_SRC_DIR:-$HOME/src/opencode/packages/opencode}"
BUN_BIN="${BUN_BIN:-$HOME/.bun/bin/bun}"
OPENCODE_BIN="${OPENCODE_BIN:-}"
OPENCODE_MODE="${OPENCODE_MODE:-patched}"
MODEL="${MODEL:-deepseek/deepseek-v4-pro}"
PROVIDER="${PROVIDER:-deepseek}"
VARIANT="${VARIANT:-}"
TIMEOUT_MULTIPLIER="${TIMEOUT_MULTIPLIER:-1.0}"
TRACE_ENABLED="${TRACE_ENABLED:-true}"
BRANCH_NAME="${BRANCH_NAME:-unit-code-gen}"
EXISTING_BRANCH="${EXISTING_BRANCH:-}"
OPENROUTER_PROVIDER_ONLY="${OPENROUTER_PROVIDER_ONLY:-auto}"
OPENROUTER_PROVIDER_ORDER="${OPENROUTER_PROVIDER_ORDER:-}"
OPENROUTER_ALLOW_FALLBACKS="${OPENROUTER_ALLOW_FALLBACKS:-false}"
OPENROUTER_REQUIRE_PARAMETERS="${OPENROUTER_REQUIRE_PARAMETERS:-true}"

LOG="${LOG:-}"
TRACE_DIR="${TRACE_DIR:-}"

usage() {
  cat <<'EOF'
Usage: scripts/benmark.sh [options]

Options:
  --model MODEL              OpenCode model, e.g. deepseek/deepseek-v4-pro
  --provider PROVIDER        OpenCode provider, e.g. deepseek
  --variant VARIANT          OpenCode model variant/reasoning effort, e.g. none, low
  --timeout-multiplier N     Multiplier for all OpenCode LLM turn timeouts
  --small-model MODEL        Small model override; defaults to --model
  --repo PATH                Java repo path
  --module NAME              Maven module name
  --class-fqn FQN            Target class FQN
  --coverage-gate PERCENT    Coverage gate
  --mutation-gate PERCENT    Mutation gate
  --stop-after-stage STAGE   UTA checkpoint: plan_tests or generation
  --resume                   Resume from latest_generation_plan.md
  --branch-name NAME         Generation branch to create/reset on first run; default unit-code-gen
  --existing-branch NAME     Reuse an existing local branch without reset/cleanup
  --preserve-branch          Do not recreate/reset current generation branch before this run
  --opencode-mode MODE       patched or builtin; default patched
  --opencode-bin PATH        Installed OpenCode binary for --opencode-mode builtin
  --trace                    Enable OpenCode LLM request tracing
  --no-trace                 Disable OpenCode LLM request tracing
  --openrouter-provider-only PROVIDERS
                            Comma-separated OpenRouter provider slugs to allow, or auto; default auto
  --openrouter-provider-order PROVIDERS
                            Comma-separated OpenRouter provider slugs to try first
  --openrouter-allow-fallbacks BOOL
                            Allow OpenRouter to switch backend providers; default false
  --openrouter-require-parameters BOOL
                            Require providers to support all request parameters; default true
  --log PATH                 Log file path
  --trace-dir PATH           OpenCode LLM trace directory
  -h, --help                 Show this help

Environment variables with the same uppercase names are also supported.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL="$2"
      shift 2
      ;;
    --provider)
      PROVIDER="$2"
      shift 2
      ;;
    --variant)
      VARIANT="$2"
      shift 2
      ;;
    --timeout-multiplier)
      TIMEOUT_MULTIPLIER="$2"
      shift 2
      ;;
    --small-model)
      SMALL_MODEL="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --module)
      MODULE="$2"
      shift 2
      ;;
    --class-fqn)
      CLASS_FQN="$2"
      shift 2
      ;;
    --coverage-gate)
      COVERAGE_GATE="$2"
      shift 2
      ;;
    --mutation-gate)
      MUTATION_GATE="$2"
      shift 2
      ;;
    --stop-after-stage)
      STOP_AFTER_STAGE="$2"
      shift 2
      ;;
    --resume)
      RESUME=true
      shift
      ;;
    --branch-name)
      BRANCH_NAME="$2"
      shift 2
      ;;
    --existing-branch)
      EXISTING_BRANCH="$2"
      shift 2
      ;;
    --preserve-branch)
      PRESERVE_BRANCH=true
      shift
      ;;
    --opencode-mode)
      OPENCODE_MODE="$2"
      shift 2
      ;;
    --opencode-bin)
      OPENCODE_BIN="$2"
      shift 2
      ;;
    --trace)
      TRACE_ENABLED=true
      shift
      ;;
    --no-trace)
      TRACE_ENABLED=false
      shift
      ;;
    --openrouter-provider-only)
      OPENROUTER_PROVIDER_ONLY="$2"
      shift 2
      ;;
    --openrouter-provider-order)
      OPENROUTER_PROVIDER_ORDER="$2"
      shift 2
      ;;
    --openrouter-allow-fallbacks)
      OPENROUTER_ALLOW_FALLBACKS="$2"
      shift 2
      ;;
    --openrouter-require-parameters)
      OPENROUTER_REQUIRE_PARAMETERS="$2"
      shift 2
      ;;
    --log)
      LOG="$2"
      shift 2
      ;;
    --trace-dir)
      TRACE_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MODEL_SLUG="$(echo "${MODEL}" | tr '/: ' '---' | tr -cd 'A-Za-z0-9._-')"
LOG="${LOG:-/tmp/uta-${MODEL_SLUG}-full-${TS}.log}"
TRACE_DIR="${TRACE_DIR:-/tmp/uta-${MODEL_SLUG}-full-trace-${TS}}"

case "${OPENCODE_MODE}" in
  patched|builtin)
    ;;
  *)
    echo "Invalid --opencode-mode: ${OPENCODE_MODE}; expected patched or builtin" >&2
    exit 2
    ;;
esac

# Correct patched OpenCode invocation. Do not use `bun run --cwd ...`; that only
# prints Bun help under UTA and causes OpenCode turns to look like stalled output.
SPAWN_CMD="[\"${BUN_BIN}\",\"--cwd\",\"${OPENCODE_SRC_DIR}\",\"src/index.ts\",\"run\"]"

if [[ "${TRACE_ENABLED}" == "true" ]]; then
  mkdir -p "${TRACE_DIR}"
fi

args=(
  -m uta.cli run
  --repo "${REPO}"
  --module "${MODULE}"
  --class-fqn "${CLASS_FQN}"
  --max-files "${MAX_FILES}"
  --coverage-gate "${COVERAGE_GATE}"
  --mutation-gate "${MUTATION_GATE}"
  --branch-name "${BRANCH_NAME}"
  --verbose
)

if [[ -n "${STOP_AFTER_STAGE:-}" ]]; then
  args+=(--stop-after-stage "${STOP_AFTER_STAGE}")
fi

if [[ "${RESUME:-false}" == "true" ]]; then
  args+=(--resume)
fi

if [[ "${PRESERVE_BRANCH:-false}" == "true" ]]; then
  args+=(--preserve-branch)
fi

if [[ -n "${EXISTING_BRANCH}" ]]; then
  args+=(--existing-branch "${EXISTING_BRANCH}")
fi

echo "UTA benchmark"
echo "repo=${REPO}"
echo "class=${CLASS_FQN}"
echo "branch=${EXISTING_BRANCH:-${BRANCH_NAME}}"
if [[ -n "${EXISTING_BRANCH}" || "${PRESERVE_BRANCH:-false}" == "true" ]]; then
  echo "branch_mode=preserve"
else
  echo "branch_mode=create_or_reset"
fi
echo "model=${MODEL}"
echo "variant=${VARIANT:-<default>}"
echo "timeout_multiplier=${TIMEOUT_MULTIPLIER}"
echo "opencode_mode=${OPENCODE_MODE}"
if [[ "${OPENCODE_MODE}" == "builtin" ]]; then
  echo "opencode_bin=${OPENCODE_BIN:-opencode}"
fi
echo "log=${LOG}"
if [[ "${TRACE_ENABLED}" == "true" ]]; then
  echo "trace=${TRACE_DIR}"
else
  echo "trace=<disabled>"
fi
if [[ "${PROVIDER}" == "openrouter" || "${MODEL}" == openrouter/* ]]; then
  echo "openrouter_provider_only=${OPENROUTER_PROVIDER_ONLY:-<unset>}"
  echo "openrouter_provider_order=${OPENROUTER_PROVIDER_ORDER:-<unset>}"
  echo "openrouter_allow_fallbacks=${OPENROUTER_ALLOW_FALLBACKS}"
  echo "openrouter_require_parameters=${OPENROUTER_REQUIRE_PARAMETERS}"
fi

env_vars=(
  UTA_OPENCODE_PROVIDER="${PROVIDER}"
  UTA_OPENCODE_MODEL="${MODEL}"
  UTA_OPENCODE_SMALL_MODEL="${SMALL_MODEL:-${MODEL}}"
  UTA_OPENCODE_VARIANT="${VARIANT}"
  UTA_OPENCODE_TIMEOUT_MULTIPLIER="${TIMEOUT_MULTIPLIER}"
  UTA_OPENCODE_INIT_SLASH_ENABLED="${UTA_OPENCODE_INIT_SLASH_ENABLED:-false}"
  UTA_OPENROUTER_PROVIDER_ONLY="${OPENROUTER_PROVIDER_ONLY}"
  UTA_OPENROUTER_PROVIDER_ORDER="${OPENROUTER_PROVIDER_ORDER}"
  UTA_OPENROUTER_ALLOW_FALLBACKS="${OPENROUTER_ALLOW_FALLBACKS}"
  UTA_OPENROUTER_REQUIRE_PARAMETERS="${OPENROUTER_REQUIRE_PARAMETERS}"
)

if [[ "${OPENCODE_MODE}" == "patched" ]]; then
  env_vars+=(UTA_OPENCODE_SPAWN_CMD="${SPAWN_CMD}")
  env_vars+=(UTA_OPENCODE_BIN="")
else
  env_vars+=(UTA_OPENCODE_SPAWN_CMD="")
  if [[ -n "${OPENCODE_BIN}" ]]; then
    env_vars+=(UTA_OPENCODE_BIN="${OPENCODE_BIN}")
  fi
fi

if [[ "${TRACE_ENABLED}" == "true" ]]; then
  env_vars+=(OPENCODE_TRACE_LLM_REQUESTS="true")
  env_vars+=(OPENCODE_TRACE_LLM_DIR="${TRACE_DIR}")
else
  env_vars+=(OPENCODE_TRACE_LLM_REQUESTS="false")
  env_vars+=(OPENCODE_TRACE_LLM_DIR="")
fi

(
  cd "${ROOT_DIR}"
  env "${env_vars[@]}" \
    "${ROOT_DIR}/.venv/bin/python" "${args[@]}"
) 2>&1 | tee "${LOG}"

exit_code="${PIPESTATUS[0]}"
echo "UTA_EXIT_CODE=${exit_code}" | tee -a "${LOG}"
exit "${exit_code}"
