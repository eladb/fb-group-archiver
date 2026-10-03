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
# The service appends every run to one file. It used to look for the newest
# comments-resume-*.log, which nothing has written since the pass became a
# service -- so it would have judged the current run by a stale, unrelated log.
LOG="$LOG_DIR/comments-service.log"

[ -e "$LOG" ] || exit 0
[ -e "$MARKER" ] && exit 0                      # already told them
systemctl --user is-active --quiet pandas-comments.service && exit 0  # still running

COMMENTS=$(sqlite3 "file:$HOME/pandas/archive/archive.db?mode=ro" \
  "select count(*) from comments" 2>/dev/null || echo "?")

# Only this run's lines: everything after the last startup banner. Grepping the
# whole append-only file would find an earlier run's "done:" or "RATE LIMITED".
RUN="$(awk '/posts with unfetched comments/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' "$LOG")"
STATUS=$(systemctl --user show pandas-comments.service -p ExecMainStatus --value 2>/dev/null)

if [ "$STATUS" = "0" ] && grep -q " done:" <<<"$RUN"; then
  MSG="Comments pass FINISHED -- $COMMENTS comments. $(basename "$LOG")"
elif [ "$STATUS" = "3" ] || grep -q "RATE LIMITED" <<<"$RUN"; then
  MSG="Comments pass STOPPED: rate limited -- $COMMENTS comments collected."
else
  MSG="Comments pass ENDED without 'done:' (exit $STATUS) -- $COMMENTS comments. Check $(basename "$LOG")"
fi

echo "$(date -Is)  $MSG" >> "$LOG_DIR/comments-finish.log"
command -v notify-send >/dev/null && \
  notify-send -u critical "PANDAS crawl" "$MSG" 2>/dev/null
touch "$MARKER"
