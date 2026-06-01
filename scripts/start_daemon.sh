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
TASK_DB="${UTA_TASK_DB_PATH:-$RUNNER_HOME/uta_tasks.db}"
LOG_FILE="${UTA_DAEMON_LOG:-$RUNNER_HOME/daemon.log}"
PID_FILE="${UTA_DAEMON_PID_FILE:-$RUNNER_HOME/daemon.pid}"
DEFAULT_DAEMON_JAVA_HOME="/opt/app/jdks/jdk8"

FOREGROUND=0
STOP_FIRST=1
EXTRA_ARGS=()

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
    --task-db)
      if [[ $# -lt 2 ]]; then
        echo "--task-db requires a path" >&2
        exit 1
      fi
      TASK_DB="$2"
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
      EXTRA_ARGS+=("$1")
      shift
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
DAEMON_JAVA_HOME="${UTA_DAEMON_JAVA_HOME:-${JAVA_HOME:-}}"
if [[ -z "$DAEMON_JAVA_HOME" && -x "$DEFAULT_DAEMON_JAVA_HOME/bin/java" ]]; then
  DAEMON_JAVA_HOME="$DEFAULT_DAEMON_JAVA_HOME"
fi
if [[ -n "$DAEMON_JAVA_HOME" ]]; then
  export JAVA_HOME="$DAEMON_JAVA_HOME"
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

CMD=("$VENV_DIR/bin/python" uta/cli.py tasks daemon --task-db "$TASK_DB" "${EXTRA_ARGS[@]}")

if [[ "$STOP_FIRST" == "1" ]]; then
  pkill -f "uta/cli.py tasks daemon" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

if [[ "$FOREGROUND" == "1" ]]; then
  cd "$ROOT_DIR"
  echo "UTA task daemon using $TASK_DB"
  python_enforcement_runtime_summary
  exec "${CMD[@]}"
fi

cd "$ROOT_DIR"
nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 < /dev/null &
daemon_pid=$!
echo "$daemon_pid" >"$PID_FILE"
echo "Started UTA daemon pid=$daemon_pid"
echo "Task DB: $TASK_DB"
echo "Log: $LOG_FILE"
python_enforcement_runtime_summary
