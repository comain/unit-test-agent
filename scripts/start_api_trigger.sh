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
HOST="${UTA_CI_PLUGIN_HOST:-0.0.0.0}"
PORT="${UTA_CI_PLUGIN_PORT:-8001}"
LOG_FILE="${UTA_CI_PLUGIN_LOG:-$RUNNER_HOME/api_trigger.log}"
PID_FILE="${UTA_CI_PLUGIN_PID_FILE:-$RUNNER_HOME/api_trigger.pid}"
INFLIGHT_DIR="${UTA_CI_INFLIGHT_DIR:-$RUNNER_HOME/ci_inflight}"
DEFAULT_CI_JAVA_HOME="/opt/app/jdks/jdk8"
STOP_WAIT_TIMEOUT_SECONDS="${UTA_CI_PLUGIN_STOP_WAIT_TIMEOUT_SECONDS:-0}"
STOP_WAIT_POLL_SECONDS="${UTA_CI_PLUGIN_STOP_WAIT_POLL_SECONDS:-10}"
STOP_GRACE_SECONDS="${UTA_CI_PLUGIN_STOP_GRACE_SECONDS:-5}"
FOREGROUND=0
STOP_FIRST=1
WAIT_FOR_RUNNING_TASKS=1

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
    --inflight-dir)
      if [[ $# -lt 2 ]]; then
        echo "--inflight-dir requires a path" >&2
        exit 1
      fi
      INFLIGHT_DIR="$2"
      shift 2
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

mkdir -p "$RUNNER_HOME" "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")" "$INFLIGHT_DIR"
export UTA_CI_INFLIGHT_DIR="$INFLIGHT_DIR"

if [[ -n "${UTA_MAVEN_BIN:-}" ]]; then
  export PATH="$(dirname "$UTA_MAVEN_BIN"):$PATH"
fi
if [[ -n "${MAVEN_HOME:-}" ]]; then
  export PATH="$MAVEN_HOME/bin:$PATH"
fi

CI_JAVA_HOME="${UTA_CI_JAVA_HOME:-}"
if [[ -z "$CI_JAVA_HOME" && -x "$DEFAULT_CI_JAVA_HOME/bin/java" ]]; then
  CI_JAVA_HOME="$DEFAULT_CI_JAVA_HOME"
fi
if [[ -z "$CI_JAVA_HOME" && -n "${JAVA_HOME:-}" ]]; then
  CI_JAVA_HOME="$JAVA_HOME"
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

CMD=("$VENV_DIR/bin/python" -m uvicorn uta.app.app:app --host "$HOST" --port "$PORT")

active_enforcement_task_count() {
  if [[ ! -f "$TASK_DB" ]] || ! command -v sqlite3 >/dev/null 2>&1; then
    echo 0
    return
  fi
  sqlite3 "$TASK_DB" "
    select count(*)
    from class_tasks
    where status='RUNNING'
      and lower(coalesce(current_stage, stage, '')) in (
        'python_verify',
        'compile_verification',
        'test_verification',
        'test_execution',
        'mutation_testing',
        'verify',
        'enforcement',
        'rerun_enforcement',
        'python_enforcement',
        'java_enforcement'
      );
  " 2>/dev/null || echo 0
}

active_enforcement_task_summary() {
  if [[ ! -f "$TASK_DB" ]] || ! command -v sqlite3 >/dev/null 2>&1; then
    return
  fi
  sqlite3 "$TASK_DB" "
    select repo_task_id || ':' || id || ':' || coalesce(current_stage, stage, '')
    from class_tasks
    where status='RUNNING'
      and lower(coalesce(current_stage, stage, '')) in (
        'python_verify',
        'compile_verification',
        'test_verification',
        'test_execution',
        'mutation_testing',
        'verify',
        'enforcement',
        'rerun_enforcement',
        'python_enforcement',
        'java_enforcement'
      )
    order by updated_at, id
    limit 5;
  " 2>/dev/null || true
}

wait_for_enforcement_runs() {
  local started now elapsed enforcement_count summary
  started="$(date +%s)"
  while true; do
    enforcement_count="$(active_enforcement_task_count)"
    if [[ "$enforcement_count" == "0" ]]; then
      return 0
    fi

    summary="$(active_enforcement_task_summary | paste -sd ', ' -)"
    echo "Waiting for active enforcement task rows before API trigger restart: enforcement_tasks=$enforcement_count${summary:+ [$summary]}"

    if [[ "$STOP_WAIT_TIMEOUT_SECONDS" != "0" ]]; then
      now="$(date +%s)"
      elapsed=$((now - started))
      if (( elapsed >= STOP_WAIT_TIMEOUT_SECONDS )); then
        echo "Timed out waiting for active enforcement task rows after ${elapsed}s; API trigger restart aborted. Use --force-stop to override." >&2
        return 1
      fi
    fi
    sleep "$STOP_WAIT_POLL_SECONDS"
  done
}

existing_plugin_pids() {
  {
    pgrep -f "uvicorn uta[.]app.app:app" 2>/dev/null || true
    pgrep -f "uvicorn uta[.]api_trigger.app:app" 2>/dev/null || true
    # Stop pre-rename deployments that still own the same port.
    pgrep -f "uvicorn uta[.]ci_plugin.app:app" 2>/dev/null || true
  } | sort -u
}

stop_existing_plugin_processes() {
  local pids pid pgid deadline remaining
  pids="$(existing_plugin_pids | tr '\n' ' ')"
  if [[ -z "${pids// }" ]]; then
    return 0
  fi

  echo "Stopping existing UTA API trigger process tree(s): $pids"
  for pid in $pids; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [[ -n "$pgid" ]]; then
      kill -TERM "-$pgid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  deadline=$(( $(date +%s) + STOP_GRACE_SECONDS ))
  while true; do
    remaining="$(existing_plugin_pids | tr '\n' ' ')"
    if [[ -z "${remaining// }" ]]; then
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      echo "Force stopping stale UTA API trigger process tree(s): $remaining"
      for pid in $remaining; do
        pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
        if [[ -n "$pgid" ]]; then
          kill -KILL "-$pgid" 2>/dev/null || true
        else
          kill -KILL "$pid" 2>/dev/null || true
        fi
      done
      return 0
    fi
    sleep 1
  done
}

if [[ "$STOP_FIRST" == "1" ]]; then
  if [[ "$WAIT_FOR_RUNNING_TASKS" == "1" ]]; then
    wait_for_enforcement_runs
  fi
  stop_existing_plugin_processes
  rm -f "$PID_FILE"
fi

if [[ "$FOREGROUND" == "1" ]]; then
  cd "$ROOT_DIR"
  echo "$$" >"$PID_FILE"
  echo "UTA API trigger listening on ${HOST}:${PORT}"
  python_enforcement_runtime_summary
  exec "${CMD[@]}"
fi

cd "$ROOT_DIR"
nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 < /dev/null &
plugin_pid=$!
echo "$plugin_pid" >"$PID_FILE"
echo "Started UTA API trigger pid=$plugin_pid"
echo "URL: http://${HOST}:${PORT}"
echo "Log: $LOG_FILE"
python_enforcement_runtime_summary
