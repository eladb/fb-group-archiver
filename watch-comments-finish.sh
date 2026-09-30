#!/usr/bin/env bash
# Notify once when the comments pass finishes. Survives the session that
# started the crawl, which a chat-side watch does not -- the pass runs for days.
#
# Fires on either ending: a clean "done:" line, or the process vanishing without
# one (crash, rate-limit abort, OOM). Both are things worth being told about, and
# only reporting success would make a silent death look like "still running".
set -uo pipefail
LOG_DIR="$HOME/.local/share/pandas-agent"
MARKER="$LOG_DIR/.comments-finish-notified"
LOG="$(ls -t "$LOG_DIR"/comments-resume-*.log 2>/dev/null | head -1)"

[ -n "$LOG" ] || exit 0
[ -e "$MARKER" ] && exit 0                      # already told them
pgrep -f "[s]crape.py comments" >/dev/null && exit 0   # still running

COMMENTS=$(sqlite3 "file:$HOME/pandas/archive/archive.db?mode=ro" \
  "select count(*) from comments" 2>/dev/null || echo "?")

if grep -q "^\[.*\] done:" "$LOG" 2>/dev/null || grep -q " done:" "$LOG" 2>/dev/null; then
  MSG="Comments pass FINISHED -- $COMMENTS comments. $(basename "$LOG")"
elif grep -q "RATE LIMITED" "$LOG" 2>/dev/null; then
  MSG="Comments pass STOPPED: rate limited -- $COMMENTS comments collected."
else
  MSG="Comments pass ENDED without 'done:' -- $COMMENTS comments. Check $(basename "$LOG")"
fi

echo "$(date -Is)  $MSG" >> "$LOG_DIR/comments-finish.log"
command -v notify-send >/dev/null && \
  notify-send -u critical "PANDAS crawl" "$MSG" 2>/dev/null
touch "$MARKER"
