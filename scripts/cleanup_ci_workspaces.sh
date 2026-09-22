#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${UTA_WORKSPACE_ROOT:-/opt/app/uta-ci-data/workspaces}"
RETENTION_DAYS="${UTA_WORKSPACE_RETENTION_DAYS:-3}"
LOG_FILE="${UTA_WORKSPACE_CLEANUP_LOG:-/opt/app/uta-ci-data/logs/workspace-cleanup.log}"
LOCK_FILE="${UTA_WORKSPACE_CLEANUP_LOCK:-/tmp/uta-workspace-cleanup.lock}"
DRY_RUN="${DRY_RUN:-0}"
RM_ONE_FILE_SYSTEM=""
if rm --help 2>/dev/null | grep -q -- '--one-file-system'; then
  RM_ONE_FILE_SYSTEM="--one-file-system"
fi

usage() {
  cat <<'EOF'
Usage: cleanup_ci_workspaces.sh [--dry-run] [--help]

Deletes direct child workspace directories older than UTA_WORKSPACE_RETENTION_DAYS.

Environment:
  UTA_WORKSPACE_ROOT          Default: /opt/app/uta-ci-data/workspaces
  UTA_WORKSPACE_RETENTION_DAYS Default: 3
  UTA_WORKSPACE_CLEANUP_LOG   Default: /opt/app/uta-ci-data/logs/workspace-cleanup.log
  UTA_WORKSPACE_CLEANUP_LOCK  Default: /tmp/uta-workspace-cleanup.lock
  DRY_RUN                     Set to 1 to log without deleting
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "RETENTION_DAYS must be a non-negative integer: $RETENTION_DAYS" >&2
  exit 2
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" | tee -a "$LOG_FILE"
}

workspace_is_active() {
  local workspace="$1"
  local resolved_workspace
  resolved_workspace="$(readlink -f "$workspace" 2>/dev/null || printf '%s' "$workspace")"

  local cwd target
  for cwd in /proc/[0-9]*/cwd; do
    target="$(readlink "$cwd" 2>/dev/null || true)"
    case "$target" in
      "$resolved_workspace"|"$resolved_workspace"/*)
        return 0
        ;;
    esac
  done
  return 1
}

cleanup() {
  if [[ ! -d "$WORKSPACE_ROOT" ]]; then
    log "skip missing workspace root: $WORKSPACE_ROOT"
    return 0
  fi

  local before_size deleted_count skipped_count failed_count path size
  before_size="$(du -sh "$WORKSPACE_ROOT" 2>/dev/null | awk '{print $1}')"
  deleted_count=0
  skipped_count=0
  failed_count=0

  log "start workspace cleanup root=$WORKSPACE_ROOT retention_days=$RETENTION_DAYS dry_run=$DRY_RUN size_before=${before_size:-unknown}"

  while IFS= read -r -d '' path; do
    if workspace_is_active "$path"; then
      log "skip active workspace: $path"
      skipped_count=$((skipped_count + 1))
      continue
    fi

    size="$(du -sh "$path" 2>/dev/null | awk '{print $1}')"
    if [[ "$DRY_RUN" == "1" ]]; then
      log "dry-run delete workspace: $path size=${size:-unknown}"
      skipped_count=$((skipped_count + 1))
      continue
    fi

    if [[ -n "$RM_ONE_FILE_SYSTEM" ]]; then
      rm -rf "$RM_ONE_FILE_SYSTEM" "$path"
    else
      rm -rf "$path"
    fi
    if [[ ! -e "$path" ]]; then
      log "deleted workspace: $path size=${size:-unknown}"
      deleted_count=$((deleted_count + 1))
    else
      log "failed delete workspace: $path size=${size:-unknown}"
      failed_count=$((failed_count + 1))
    fi
  done < <(find "$WORKSPACE_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -print0)

  local after_size
  after_size="$(du -sh "$WORKSPACE_ROOT" 2>/dev/null | awk '{print $1}')"
  log "finish workspace cleanup deleted=$deleted_count skipped=$skipped_count failed=$failed_count size_after=${after_size:-unknown}"

  if [[ "$failed_count" -gt 0 ]]; then
    return 1
  fi
}

if command -v flock >/dev/null 2>&1; then
  (
    flock -n 9 || {
      log "skip cleanup because another cleanup is running lock=$LOCK_FILE"
      exit 0
    }
    cleanup
  ) 9>"$LOCK_FILE"
else
  LOCK_DIR="${LOCK_FILE}.d"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "skip cleanup because another cleanup is running lock=$LOCK_DIR"
    exit 0
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  cleanup
fi
