#!/bin/bash
# cron-stub.sh — Main entry point for ai-radar periodic update.
#
# Usage:
#   ./cron-stub.sh            # full run (fetch + detect + build + push if changed)
#   ./cron-stub.sh --dry-run # run all steps but never push, never write to git
#   ./cron-stub.sh --fetch   # only fetch (skip build/push)
#   ./cron-stub.sh --build   # fetch + build, but skip push
#
# Designed to be triggered by systemd timer on GCP. Runs every 6 hours.
# Idempotent: if no changes detected, push is skipped.
# Resilient: if any step fails, aborts before pushing broken data.

set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/cron.log"
LOCK_FILE="$SCRIPT_DIR/.cron.lock"
GIT_REMOTE="origin"
GIT_BRANCH="main"

DRY_RUN=false
SKIP_PUSH=false
SKIP_FETCH=false
SKIP_BUILD=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --no-push) SKIP_PUSH=true ;;
    --fetch-only) SKIP_BUILD=true ;;
    --help|-h)
      grep -E '^#' "$0" | sed 's/^# \?//'
      exit 0
      ;;
  esac
done

# --- Helpers ---
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE" >&2; }
fail() { log "ERROR: $*"; exit 1; }

mkdir -p "$LOG_DIR"

# Lock to prevent overlapping runs.
# flock is unavailable on macOS by default; fall back to mkdir-based lock.
acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    flock -n 9 || return 1
    return 0
  else
    # mkdir is atomic on POSIX: if the dir already exists, this fails.
    if mkdir "$LOCK_FILE" 2>/dev/null; then
      echo $$ > "$LOCK_FILE/pid"
      return 0
    fi
    return 1
  fi
}
release_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>&- 2>/dev/null || true
  else
    rm -rf "$LOCK_FILE" 2>/dev/null || true
  fi
}

if ! acquire_lock; then
  fail "another cron run is in progress (lock: $LOCK_FILE)"
fi
trap release_lock EXIT

START_TS=$(date +%s)
log "=== ai-radar cron run start (dry_run=$DRY_RUN) ==="

# --- 1. Fetch ---
if [ "$SKIP_FETCH" != "true" ]; then
  log "step 1/3: fetch_pricing.py"
  if ! python3 scripts/fetch_pricing.py >> "$LOG_FILE" 2>&1; then
    fail "fetch_pricing failed"
  fi
  log "step 1/3: fetch_news.py"
  if ! python3 scripts/fetch_news.py >> "$LOG_FILE" 2>&1; then
    fail "fetch_news failed"
  fi
else
  log "step 1/3: SKIPPED (--fetch-only not set)"
fi

# --- 2. Detect changes ---
log "step 2/3: detect_changes.py"
if ! python3 scripts/detect_changes.py >> "$LOG_FILE" 2>&1; then
  fail "detect_changes failed"
fi

# --- 3. Build ---
if [ "$SKIP_BUILD" != "true" ]; then
  log "step 3/3: build.py"
  if ! python3 scripts/build.py >> "$LOG_FILE" 2>&1; then
    fail "build failed"
  fi
else
  log "step 3/3: SKIPPED (--fetch-only)"
fi

# --- 4. Push if changed ---
if [ "$SKIP_PUSH" = "true" ] || [ "$DRY_RUN" = "true" ]; then
  log "skip push (--dry-run or --no-push)"
  log "data files updated locally only"
  exit 0
fi

# Check if anything changed
if git diff --quiet data/ index.html 2>/dev/null; then
  log "no changes detected — skipping push"
  ELAPSED=$(( $(date +%s) - START_TS ))
  log "=== cron run done (no-op, ${ELAPSED}s) ==="
  exit 0
fi

# Commit and push
log "changes detected — committing"
git add data/ index.html scripts/

if git diff --cached --quiet; then
  log "nothing to commit after staging — race? skipping push"
  exit 0
fi

COMMIT_MSG="chore: automated update $(date -u +%Y-%m-%dT%H:%M:%SZ)"
if ! git -c user.name=echo \
        -c user.email=242155312+w1977-0@users.noreply.github.com \
        commit -m "$COMMIT_MSG" >> "$LOG_FILE" 2>&1; then
  fail "git commit failed"
fi

log "pushing to $GIT_REMOTE/$GIT_BRANCH"
if ! git push "$GIT_REMOTE" "$GIT_BRANCH" >> "$LOG_FILE" 2>&1; then
  fail "git push failed (data updated locally, will retry next run)"
fi

ELAPSED=$(( $(date +%s) - START_TS ))
log "=== cron run done (pushed, ${ELAPSED}s) ==="
