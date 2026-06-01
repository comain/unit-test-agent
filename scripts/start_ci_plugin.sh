#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${UTA_ENV_FILE:-$ROOT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

VENV_DIR="${UTA_VENV_DIR:-$ROOT_DIR/.venv}"
RUNNER_HOME="${UTA_RUNNER_HOME:-$ROOT_DIR/.uta_runner}"
HOST="${UTA_CI_PLUGIN_HOST:-0.0.0.0}"
PORT="${UTA_CI_PLUGIN_PORT:-8001}"
LOG_FILE="${UTA_CI_PLUGIN_LOG:-$RUNNER_HOME/ci_plugin.log}"
PID_FILE="${UTA_CI_PLUGIN_PID_FILE:-$RUNNER_HOME/ci_plugin.pid}"
DEFAULT_CI_JAVA_HOME="/opt/app/jdks/jdk8"
FOREGROUND=0
STOP_FIRST=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground)
      FOREGROUND=1
      shift
      ;;
    --no-stop)
      STOP_FIRST=0
      shift
      ;;
    --host)
      if [[ $# -lt 2 ]]; then
        echo "--host requires a value" >&2
        exit 1
      fi
      HOST="$2"
      shift 2
      ;;
    --port)
      if [[ $# -lt 2 ]]; then
        echo "--port requires a value" >&2
        exit 1
      fi
      PORT="$2"
      shift 2
      ;;
    --log-file)
      if [[ $# -lt 2 ]]; then
        echo "--log-file requires a path" >&2
        exit 1
      fi
      LOG_FILE="$2"
      shift 2
      ;;
    --pid-file)
      if [[ $# -lt 2 ]]; then
        echo "--pid-file requires a path" >&2
        exit 1
      fi
      PID_FILE="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$RUNNER_HOME" "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"

if [[ -n "${UTA_MAVEN_BIN:-}" ]]; then
  export PATH="$(dirname "$UTA_MAVEN_BIN"):$PATH"
fi
if [[ -n "${MAVEN_HOME:-}" ]]; then
  export PATH="$MAVEN_HOME/bin:$PATH"
fi

CI_JAVA_HOME="${UTA_CI_JAVA_HOME:-${JAVA_HOME:-}}"
if [[ -z "$CI_JAVA_HOME" && -x "$DEFAULT_CI_JAVA_HOME/bin/java" ]]; then
  CI_JAVA_HOME="$DEFAULT_CI_JAVA_HOME"
fi
if [[ -n "$CI_JAVA_HOME" ]]; then
  export JAVA_HOME="$CI_JAVA_HOME"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

configure_python_enforcement_runtime() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    export PATH="$VENV_DIR/bin:$PATH"
    export UTA_SERVICE_PYTHON_BIN="$VENV_DIR/bin/python"
  fi
  if [[ -n "${UTA_PYTHON2_BIN:-}" ]]; then
    export UTA_PYTHON2_BIN
  fi
  if [[ -n "${UTA_PYTHON2_MUTMUT_BIN:-}" ]]; then
    export UTA_PYTHON2_MUTMUT_BIN
  fi
}

python_enforcement_runtime_summary() {
  echo "Python enforcement: python=${UTA_PYTHON_BIN:-repo/default auto} python2=${UTA_PYTHON2_BIN:-not configured} python2-mutmut=${UTA_PYTHON2_MUTMUT_BIN:-not configured}"
}

configure_python_enforcement_runtime

CMD=("$VENV_DIR/bin/python" -m uvicorn uta.ci_plugin.app:app --host "$HOST" --port "$PORT")

if [[ "$STOP_FIRST" == "1" ]]; then
  pkill -f "uvicorn uta.ci_plugin.app:app" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

if [[ "$FOREGROUND" == "1" ]]; then
  cd "$ROOT_DIR"
  echo "UTA CI plugin listening on ${HOST}:${PORT}"
  python_enforcement_runtime_summary
  exec "${CMD[@]}"
fi

cd "$ROOT_DIR"
nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 < /dev/null &
plugin_pid=$!
echo "$plugin_pid" >"$PID_FILE"
echo "Started UTA CI plugin pid=$plugin_pid"
echo "URL: http://${HOST}:${PORT}"
echo "Log: $LOG_FILE"
python_enforcement_runtime_summary
