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
RUNNER_HOME="${UTA_RUNNER_HOME:-$HOME/.local/share/uta}"
TASK_DB="${UTA_TASK_DB_PATH:-$RUNNER_HOME/uta_tasks.db}"
MIN_PYTHON_VERSION="${UTA_MIN_PYTHON_VERSION:-3.9}"
BOOTSTRAP_UV="${UTA_BOOTSTRAP_UV:-0}"
UV_BIN="${UTA_UV_BIN:-$HOME/.local/bin/uv}"
UV_PYTHON_VERSION="${UTA_UV_PYTHON_VERSION:-3.11}"
INSTALL_OPENCODE="${UTA_INSTALL_OPENCODE:-1}"
OPENCODE_INSTALL_URL="${UTA_OPENCODE_INSTALL_URL:-https://opencode.ai/install}"
OPENCODE_INSTALL_TIMEOUT_SECONDS="${UTA_OPENCODE_INSTALL_TIMEOUT_SECONDS:-300}"
OPENCODE_DOWNLOAD_TIMEOUT_SECONDS="${UTA_OPENCODE_DOWNLOAD_TIMEOUT_SECONDS:-300}"
OPENCODE_INSTALL_DIR="${OPENCODE_INSTALL_DIR:-$HOME/.opencode/bin}"
OPENCODE_BIN="${UTA_OPENCODE_BIN:-}"
OPENCODE_BINARY="${UTA_OPENCODE_BINARY:-}"
OPENCODE_BINARY_URL="${UTA_OPENCODE_BINARY_URL:-}"
INSTALL_MAVEN="${UTA_INSTALL_MAVEN:-1}"
MAVEN_VERSION="${UTA_MAVEN_VERSION:-3.9.11}"
MAVEN_INSTALL_PARENT="${UTA_MAVEN_INSTALL_PARENT:-$HOME/.local/opt}"
MAVEN_ARCHIVE="${UTA_MAVEN_ARCHIVE:-}"
MAVEN_ARCHIVE_URL="${UTA_MAVEN_ARCHIVE_URL:-https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz}"
MAVEN_DOWNLOAD_TIMEOUT_SECONDS="${UTA_MAVEN_DOWNLOAD_TIMEOUT_SECONDS:-300}"
MAVEN_BIN="${UTA_MAVEN_BIN:-}"
MAVEN_HOME="${MAVEN_HOME:-${UTA_MAVEN_HOME:-}}"
MAVEN_SETTINGS_PATH="${UTA_MAVEN_SETTINGS_PATH:-$HOME/.m2/settings.xml}"
MAVEN_SETTINGS_SOURCE="${UTA_MAVEN_SETTINGS_SOURCE:-}"
GIT_USER_NAME="${UTA_GIT_USER_NAME:-UTA Runner}"
GIT_USER_EMAIL="${UTA_GIT_USER_EMAIL:-uta-runner@example.com}"

python_version_ok() {
  local python_bin="$1"
  "$python_bin" - "$MIN_PYTHON_VERSION" <<'PY' >/dev/null 2>&1
import sys
required = tuple(int(part) for part in sys.argv[1].split(".")[:2])
sys.exit(0 if sys.version_info[:2] >= required else 1)
PY
}

find_python() {
  local candidates=()
  if [[ -n "${UTA_PYTHON_BIN:-}" ]]; then
    candidates+=("$UTA_PYTHON_BIN")
  fi
  candidates+=(python3.12 python3.11 python3.10 python3.9 python3)
  local candidate
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

install_uv_if_needed() {
  if [[ -x "$UV_BIN" ]]; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to bootstrap uv, but curl was not found" >&2
    return 1
  fi
  echo "Installing uv to bootstrap Python $UV_PYTHON_VERSION ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  [[ -x "$UV_BIN" ]]
}

find_opencode() {
  if [[ -n "$OPENCODE_BIN" && -x "$OPENCODE_BIN" ]]; then
    echo "$OPENCODE_BIN"
    return 0
  fi
  if command -v opencode >/dev/null 2>&1; then
    command -v opencode
    return 0
  fi
  local candidate
  for candidate in \
    "$HOME/.local/bin/opencode" \
    "$HOME/.opencode/bin/opencode" \
    "$OPENCODE_INSTALL_DIR/opencode" \
    "$HOME/.bun/bin/opencode"; do
    if [[ -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

persist_env_var() {
  local name="$1"
  local value="$2"
  if [[ ! -f "$ENV_FILE" ]]; then
    return 0
  fi
  if grep -q "^${name}=" "$ENV_FILE"; then
    return 0
  fi
  printf '\n%s=%q\n' "$name" "$value" >>"$ENV_FILE"
}

persist_path_prefix() {
  local dir="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    return 0
  fi
  if grep -Eq "^PATH=.*(^|:)$dir(:|$)" "$ENV_FILE"; then
    return 0
  fi
  printf '\nPATH=%s:$PATH\n' "$dir" >>"$ENV_FILE"
}

configure_git_identity_if_needed() {
  if ! command -v git >/dev/null 2>&1; then
    echo "git is required for committing generated tests, but git was not found" >&2
    return 1
  fi
  if [[ -z "$(git config --global user.name || true)" ]]; then
    git config --global user.name "$GIT_USER_NAME"
  fi
  if [[ -z "$(git config --global user.email || true)" ]]; then
    git config --global user.email "$GIT_USER_EMAIL"
  fi
}

find_maven() {
  if [[ -n "$MAVEN_BIN" && -x "$MAVEN_BIN" ]]; then
    echo "$MAVEN_BIN"
    return 0
  fi
  if [[ -n "$MAVEN_HOME" && -x "$MAVEN_HOME/bin/mvn" ]]; then
    echo "$MAVEN_HOME/bin/mvn"
    return 0
  fi
  if command -v mvn >/dev/null 2>&1; then
    command -v mvn
    return 0
  fi
  local candidate
  for candidate in "$MAVEN_INSTALL_PARENT"/apache-maven-*/bin/mvn; do
    if [[ -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

install_maven_archive() {
  local archive="$1"
  mkdir -p "$MAVEN_INSTALL_PARENT"
  tar -xzf "$archive" -C "$MAVEN_INSTALL_PARENT"
  local found
  found="$(find "$MAVEN_INSTALL_PARENT" -maxdepth 3 -type f -path "*/bin/mvn" -perm -111 | sort | tail -1 || true)"
  if [[ -z "$found" ]]; then
    echo "Maven archive did not contain an executable bin/mvn" >&2
    return 1
  fi
  MAVEN_BIN="$found"
  MAVEN_HOME="$(cd "$(dirname "$found")/.." && pwd)"
}

install_maven_from_url() {
  local tmp_dir archive
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/uta-maven-download.XXXXXX")"
  archive="$tmp_dir/apache-maven.tar.gz"
  curl -fL --connect-timeout 20 --max-time "$MAVEN_DOWNLOAD_TIMEOUT_SECONDS" -o "$archive" "$MAVEN_ARCHIVE_URL"
  install_maven_archive "$archive"
  rm -rf "$tmp_dir"
}

install_maven_if_needed() {
  local found
  found="$(find_maven || true)"
  if [[ -n "$found" ]]; then
    MAVEN_BIN="$found"
    MAVEN_HOME="$(cd "$(dirname "$found")/.." && pwd)"
    return 0
  fi

  if [[ "$INSTALL_MAVEN" != "1" ]]; then
    cat >&2 <<EOF
Maven was not found.

Install Maven manually, set UTA_MAVEN_BIN=/path/to/mvn, or rerun with:
  UTA_INSTALL_MAVEN=1 $0
EOF
    return 1
  fi

  if [[ -n "$MAVEN_ARCHIVE" ]]; then
    if [[ ! -f "$MAVEN_ARCHIVE" ]]; then
      echo "UTA_MAVEN_ARCHIVE was set but does not exist: $MAVEN_ARCHIVE" >&2
      return 1
    fi
    echo "Installing Maven from local archive ..."
    install_maven_archive "$MAVEN_ARCHIVE"
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install Maven, but curl was not found" >&2
    return 1
  fi

  echo "Installing Maven $MAVEN_VERSION ..."
  install_maven_from_url
}

install_maven_settings_if_needed() {
  if [[ -f "$MAVEN_SETTINGS_PATH" ]]; then
    return 0
  fi
  if [[ -z "$MAVEN_SETTINGS_SOURCE" ]]; then
    echo "Maven settings not found at $MAVEN_SETTINGS_PATH; internal dependencies may not resolve" >&2
    return 0
  fi
  if [[ ! -f "$MAVEN_SETTINGS_SOURCE" ]]; then
    echo "UTA_MAVEN_SETTINGS_SOURCE was set but does not exist: $MAVEN_SETTINGS_SOURCE" >&2
    return 1
  fi
  mkdir -p "$(dirname "$MAVEN_SETTINGS_PATH")"
  cp "$MAVEN_SETTINGS_SOURCE" "$MAVEN_SETTINGS_PATH"
  chmod 600 "$MAVEN_SETTINGS_PATH"
}

install_opencode_binary() {
  local source_bin="$1"
  mkdir -p "$OPENCODE_INSTALL_DIR"
  cp "$source_bin" "$OPENCODE_INSTALL_DIR/opencode"
  chmod 755 "$OPENCODE_INSTALL_DIR/opencode"
  OPENCODE_BIN="$OPENCODE_INSTALL_DIR/opencode"
}

install_opencode_archive() {
  local archive="$1"
  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/uta-opencode-install.XXXXXX")"
  tar -xzf "$archive" -C "$tmp_dir"
  local extracted="$tmp_dir/opencode"
  if [[ ! -f "$extracted" ]]; then
    extracted="$(find "$tmp_dir" -type f -name opencode -perm -111 | head -1 || true)"
  fi
  if [[ -z "$extracted" || ! -f "$extracted" ]]; then
    rm -rf "$tmp_dir"
    echo "OpenCode archive did not contain an executable named opencode" >&2
    return 1
  fi
  install_opencode_binary "$extracted"
  rm -rf "$tmp_dir"
}

install_opencode_from_url() {
  local url="$1"
  local tmp_dir archive
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/uta-opencode-download.XXXXXX")"
  archive="$tmp_dir/opencode.tar.gz"
  curl -fL --connect-timeout 20 --max-time "$OPENCODE_DOWNLOAD_TIMEOUT_SECONDS" -o "$archive" "$url"
  install_opencode_archive "$archive"
  rm -rf "$tmp_dir"
}

run_opencode_install_script() {
  mkdir -p "$OPENCODE_INSTALL_DIR"
  if command -v timeout >/dev/null 2>&1; then
    OPENCODE_INSTALL_DIR="$OPENCODE_INSTALL_DIR" timeout "$OPENCODE_INSTALL_TIMEOUT_SECONDS" \
      bash -c 'curl -fsSL "$1" | bash -s -- --no-modify-path' bash "$OPENCODE_INSTALL_URL"
  else
    curl -fsSL --connect-timeout 20 --max-time "$OPENCODE_INSTALL_TIMEOUT_SECONDS" "$OPENCODE_INSTALL_URL" \
      | OPENCODE_INSTALL_DIR="$OPENCODE_INSTALL_DIR" bash -s -- --no-modify-path
  fi
}

install_opencode_if_needed() {
  local found
  found="$(find_opencode || true)"
  if [[ -n "$found" ]]; then
    OPENCODE_BIN="$found"
    return 0
  fi

  if [[ "$INSTALL_OPENCODE" != "1" ]]; then
    cat >&2 <<EOF
OpenCode was not found.

Install OpenCode manually, set UTA_OPENCODE_BIN=/path/to/opencode, or rerun with:
  UTA_INSTALL_OPENCODE=1 $0
EOF
    return 1
  fi

  if [[ -n "$OPENCODE_BINARY" ]]; then
    if [[ ! -f "$OPENCODE_BINARY" ]]; then
      echo "UTA_OPENCODE_BINARY was set but does not exist: $OPENCODE_BINARY" >&2
      return 1
    fi
    echo "Installing OpenCode from local binary/archive ..."
    case "$OPENCODE_BINARY" in
      *.tar.gz|*.tgz) install_opencode_archive "$OPENCODE_BINARY" ;;
      *) install_opencode_binary "$OPENCODE_BINARY" ;;
    esac
    return 0
  fi

  if [[ -n "$OPENCODE_BINARY_URL" ]]; then
    echo "Installing OpenCode from UTA_OPENCODE_BINARY_URL ..."
    install_opencode_from_url "$OPENCODE_BINARY_URL"
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install OpenCode, but curl was not found" >&2
    return 1
  fi

  echo "Installing OpenCode ..."
  run_opencode_install_script

  found="$(find_opencode || true)"
  if [[ -z "$found" ]]; then
    echo "OpenCode install completed, but no opencode binary was found" >&2
    return 1
  fi
  OPENCODE_BIN="$found"
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" && "$BOOTSTRAP_UV" == "1" ]]; then
  install_uv_if_needed
  "$UV_BIN" python install "$UV_PYTHON_VERSION"
  PYTHON_BIN="$("$UV_BIN" python find "$UV_PYTHON_VERSION")"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  cat >&2 <<EOF
No Python >= $MIN_PYTHON_VERSION found.

Set UTA_PYTHON_BIN=/path/to/python, install a modern Python, or rerun with:
  UTA_BOOTSTRAP_UV=1 $0
EOF
  exit 2
fi

mkdir -p "$RUNNER_HOME"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"

install_maven_if_needed
install_maven_settings_if_needed
configure_git_identity_if_needed
persist_env_var "MAVEN_HOME" "$MAVEN_HOME"
persist_env_var "UTA_MAVEN_BIN" "$MAVEN_BIN"
persist_env_var "UTA_MAVEN_SETTINGS_PATH" "$MAVEN_SETTINGS_PATH"
persist_path_prefix "$(dirname "$MAVEN_BIN")"

install_opencode_if_needed
persist_env_var "UTA_OPENCODE_BIN" "$OPENCODE_BIN"

UTA_RUNNER_HOME="$RUNNER_HOME" UTA_TASK_DB_PATH="$TASK_DB" "$VENV_DIR/bin/uta" tasks list --limit 1 >/dev/null

cat <<EOF
UTA single-host runner deployed.

Root:        $ROOT_DIR
Venv:        $VENV_DIR
Python:      $("$VENV_DIR/bin/python" --version)
Runner home: $RUNNER_HOME
Task DB:     $TASK_DB
Maven:       $MAVEN_BIN
Mvn settings: $MAVEN_SETTINGS_PATH
OpenCode:    $OPENCODE_BIN
Python enforcement: modern=${UTA_PYTHON_BIN:-$VENV_DIR/bin/python}; legacy-python2=${UTA_PYTHON2_BIN:-not configured}; legacy-mutmut=${UTA_PYTHON2_MUTMUT_BIN:-not configured}

Next:
  export UTA_RUNNER_HOME="$RUNNER_HOME"
  export UTA_TASK_DB_PATH="$TASK_DB"
  "$VENV_DIR/bin/uta" tasks create --repo /path/to/java-repo --all
  "$VENV_DIR/bin/uta" tasks create --repo /path/to/python-repo --language python --target jobs/forecast.py
  # Equivalent CLI readiness check: uta python-enforce --help
  "$VENV_DIR/bin/uta" python-enforce --help
  # Equivalent readiness check: python -c "import tree_sitter_python"
  "$VENV_DIR/bin/python" -c "import tree_sitter_python"
  # Optional legacy lane:
  # export UTA_PYTHON2_BIN=/path/to/python2.7
  # export UTA_PYTHON2_MUTMUT_BIN=/path/to/mutmut-1.5.0
  scripts/start_daemon.sh --poll-interval 10
EOF
