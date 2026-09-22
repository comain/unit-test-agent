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
WAIT_FOR_RUNNING_TASKS=1
STOP_WAIT_TIMEOUT_SECONDS="${UTA_DAEMON_STOP_WAIT_TIMEOUT_SECONDS:-0}"
STOP_WAIT_POLL_SECONDS="${UTA_DAEMON_STOP_WAIT_POLL_SECONDS:-10}"
STOP_GRACE_SECONDS="${UTA_DAEMON_STOP_GRACE_SECONDS:-5}"
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
    --force-stop)
      WAIT_FOR_RUNNING_TASKS=0
      shift
      ;;
    --stop-wait-timeout)
      if [[ $# -lt 2 ]]; then
        echo "--stop-wait-timeout requires seconds" >&2
        exit 1
      fi
      STOP_WAIT_TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --stop-wait-poll)
      if [[ $# -lt 2 ]]; then
        echo "--stop-wait-poll requires seconds" >&2
        exit 1
      fi
      STOP_WAIT_POLL_SECONDS="$2"
      shift 2
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
DAEMON_JAVA_HOME="${UTA_DAEMON_JAVA_HOME:-}"
if [[ -z "$DAEMON_JAVA_HOME" && -x "$DEFAULT_DAEMON_JAVA_HOME/bin/java" ]]; then
  DAEMON_JAVA_HOME="$DEFAULT_DAEMON_JAVA_HOME"
fi
if [[ -z "$DAEMON_JAVA_HOME" && -n "${JAVA_HOME:-}" ]]; then
  DAEMON_JAVA_HOME="$JAVA_HOME"
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
  if [[ -n "${UTA_PYTHON_MUTMUT_BIN:-}" ]]; then
    export UTA_PYTHON_MUTMUT_BIN
  fi
  if [[ -n "${UTA_PYTHON2_BIN:-}" ]]; then
    export UTA_PYTHON2_BIN
  fi
  if [[ -n "${UTA_PYTHON2_MUTMUT_BIN:-}" ]]; then
    export UTA_PYTHON2_MUTMUT_BIN
  fi
}

python_enforcement_runtime_summary() {
  echo "Python enforcement: python=${UTA_PYTHON_BIN:-repo/default auto} mutmut=${UTA_PYTHON_MUTMUT_BIN:-repo/default auto} python2=${UTA_PYTHON2_BIN:-not configured} python2-mutmut=${UTA_PYTHON2_MUTMUT_BIN:-not configured}"
  echo "Java enforcement: JAVA_HOME=${JAVA_HOME:-not configured}"
}

configure_python_enforcement_runtime

CMD=("$VENV_DIR/bin/python" uta/app/cli.py tasks daemon --task-db "$TASK_DB" "${EXTRA_ARGS[@]}")

active_running_task_count() {
  if [[ ! -f "$TASK_DB" ]] || ! command -v sqlite3 >/dev/null 2>&1; then
    echo 0
    return
  fi
  sqlite3 "$TASK_DB" "
    select count(*)
    from repo_tasks
    where status='RUNNING';
  " 2>/dev/null || echo 0
}

active_running_task_summary() {
  if [[ ! -f "$TASK_DB" ]] || ! command -v sqlite3 >/dev/null 2>&1; then
    return
  fi
  sqlite3 "$TASK_DB" "
    select id || ':' || repo_slug || ':' || coalesce(current_stage, '')
    from repo_tasks
    where status='RUNNING'
    order by updated_at, id
    limit 5;
  " 2>/dev/null || true
}

wait_for_running_tasks() {
  local started now elapsed running_count summary
  started="$(date +%s)"
  while true; do
    running_count="$(active_running_task_count)"
    if [[ "$running_count" == "0" ]]; then
      return 0
    fi

    summary="$(active_running_task_summary | paste -sd ', ' -)"
    echo "Waiting for running UTA repo tasks before daemon restart: running_tasks=$running_count${summary:+ [$summary]}"

    if [[ "$STOP_WAIT_TIMEOUT_SECONDS" != "0" ]]; then
      now="$(date +%s)"
      elapsed=$((now - started))
      if (( elapsed >= STOP_WAIT_TIMEOUT_SECONDS )); then
        echo "Timed out waiting for running UTA repo tasks after ${elapsed}s; daemon restart aborted. Use --force-stop to override." >&2
        return 1
      fi
    fi
    sleep "$STOP_WAIT_POLL_SECONDS"
  done
}

existing_daemon_pids() {
  pgrep -f "python .*uta/app/cli[.]py tasks daemon" 2>/dev/null || true
}

stop_existing_daemon_processes() {
  local pids pid deadline remaining
  pids="$(existing_daemon_pids | tr '\n' ' ')"
  if [[ -z "${pids// }" ]]; then
    return 0
  fi

  echo "Stopping existing UTA daemon process(es): $pids"
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done

  deadline=$(( $(date +%s) + STOP_GRACE_SECONDS ))
  while true; do
    remaining="$(existing_daemon_pids | tr '\n' ' ')"
    if [[ -z "${remaining// }" ]]; then
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      echo "Force stopping stale UTA daemon process(es): $remaining"
      for pid in $remaining; do
        kill -KILL "$pid" 2>/dev/null || true
      done
      return 0
    fi
    sleep 1
  done
}

if [[ "$STOP_FIRST" == "1" ]]; then
  if [[ "$WAIT_FOR_RUNNING_TASKS" == "1" ]]; then
    wait_for_running_tasks
  fi
  stop_existing_daemon_processes
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
