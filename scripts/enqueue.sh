#!/usr/bin/env bash
# Enqueue a repo for unit-test generation.
#
# Usage:
#   enqueue.sh <git_url> [--language auto|java|python] [--module <module>] [--branch <branch>]
#              [--target <target>] [--all] [--hard-cap-usd <usd>] [--priority <n>]
#
# Environment:
#   UTA_TASK_DB   path to the SQLite task DB (default: uta_tasks.db)
#   UTA_CLONE_ROOT clone destination root (default: ~/.local/share/uta/code)
#   UTA_VENV_DIR  UTA virtualenv used to run the CLI (default: .venv when present)
#   UTA_CLI_PYTHON_BIN explicit Python used to run the UTA CLI
#
# Examples:
#   ./scripts/enqueue.sh git@git.example.com:team/myrepo.git
#   ./scripts/enqueue.sh https://github.com/org/repo.git --module biz --hard-cap-usd 5.00
#   ./scripts/enqueue.sh git@git.example.com:team/python-job.git --language python --target jobs/forecast.py
#   ./scripts/enqueue.sh git@git.example.com:team/python-job.git --language python --all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASK_DB="${UTA_TASK_DB:-uta_tasks.db}"
VENV_DIR="${UTA_VENV_DIR:-$REPO_ROOT/.venv}"
PYTHON_BIN="${UTA_CLI_PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" && -x "$VENV_DIR/bin/python" ]]; then
  PYTHON_BIN="$VENV_DIR/bin/python"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

cd "$REPO_ROOT"

exec "$PYTHON_BIN" -m uta.cli tasks enqueue "$@" --task-db "$TASK_DB"
