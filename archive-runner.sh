#!/usr/bin/env bash
# One bounded cycle of ongoing capture: sweep the feed, then fill in comments.
#
# For keeping an archive current, not for building one. The feed only serves
# the last few weeks, so this catches what is new; reaching older history is
# `scrape.py backfill`.
#
# Deliberately not a continuous loop. Meta's automation heuristics key on
# sustained uniform request patterns, and a tripped check is a 24-72h lock with
# identity re-verification -- which ends the archiving altogether. Each cycle is
# time-boxed and the timer leaves a gap between them.
#
# Usage:
#   archive-runner.sh --group <url> [--repo DIR] [--crawl-minutes N]
#                     [--comment-posts N] [--timeout SECONDS]
#
# Environment equivalents: FBGROUP_GROUP, FBGROUP_REPO, FBGROUP_CRAWL_MINUTES,
# FBGROUP_COMMENT_POSTS, FBGROUP_TIMEOUT.
#
# Intended to be driven by a timer, e.g. a systemd user unit:
#   [Service]
#   Type=oneshot
#   ExecStart=%h/pandas/archive-runner.sh --group https://www.facebook.com/groups/<id>/
set -uo pipefail

REPO="${FBGROUP_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
GROUP="${FBGROUP_GROUP:-}"
CRAWL_MINUTES="${FBGROUP_CRAWL_MINUTES:-30}"
COMMENT_POSTS="${FBGROUP_COMMENT_POSTS:-60}"
STEP_TIMEOUT="${FBGROUP_TIMEOUT:-2400}"

while [ $# -gt 0 ]; do
  case "$1" in
    --group)          GROUP="$2"; shift 2 ;;
    --repo)           REPO="$2"; shift 2 ;;
    --crawl-minutes)  CRAWL_MINUTES="$2"; shift 2 ;;
    --comment-posts)  COMMENT_POSTS="$2"; shift 2 ;;
    --timeout)        STEP_TIMEOUT="$2"; shift 2 ;;
    -h|--help)        sed -n '2,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;/^set -uo/d'; exit 0 ;;
    *)                echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$GROUP" ] || { echo "error: --group is required (or set FBGROUP_GROUP)" >&2; exit 2; }
[ -x "$REPO/.venv/bin/python" ] || { echo "error: no venv at $REPO/.venv -- see README" >&2; exit 2; }

LOG_DIR="$REPO/archive/runner"
LOCK="$LOG_DIR/.lock"
mkdir -p "$LOG_DIR"

# Overlapping cycles would mean two sessions against one account, which is the
# thing most likely to trip a check.
exec 9>"$LOCK"
flock -n 9 || {
  echo "$(date -Is) cycle skipped: previous run still going" >> "$LOG_DIR/runner.log"
  exit 0
}

cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"
DB="file:$REPO/archive/archive.db?mode=ro"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log() { echo "$(date -Is) $*" >> "$LOG_DIR/runner.log"; }
counts() { sqlite3 "$DB" \
  "select (select count(*) from posts)||' posts, '||(select count(*) from comments)||' comments'"; }

# An expired session yields an endless run of empty cycles that look like a
# quiet group. Fail loudly instead, and leave a flag a human can find.
if ! REPO="$REPO" "$PY" - <<'EOF' >/dev/null 2>&1
import os, sys
sys.path.insert(0, os.environ["REPO"])
from pathlib import Path
import scrape
pw, ctx = scrape.open_browser(Path(".chrome-profile"), headless=True)
ok = scrape.is_logged_in(ctx)
ctx.close(); pw.stop()
raise SystemExit(0 if ok else 1)
EOF
then
  log "ABORT: Facebook session is no longer valid -- run 'scrape.py login' to renew"
  echo "session-expired" > "$LOG_DIR/HALTED"
  exit 1
fi

log "cycle $stamp start -- $(counts)"

timeout "$STEP_TIMEOUT" "$PY" scrape.py crawl --group "$GROUP" \
  --max-minutes "$CRAWL_MINUTES" --no-media >> "$LOG_DIR/crawl-$stamp.log" 2>&1
log "  crawl done -- $(counts)"

timeout "$STEP_TIMEOUT" "$PY" scrape.py comments \
  --max-posts "$COMMENT_POSTS" --no-media >> "$LOG_DIR/comments-$stamp.log" 2>&1
log "  comments done -- $(counts)"

printf '%s\t%s\n' "$stamp" "$(counts)" >> "$LOG_DIR/cycles.tsv"
