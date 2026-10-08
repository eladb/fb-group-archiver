#!/usr/bin/env bash
# Is the comment-query rate limit still in force?
#
# A 2-post probe, then a look at what came back. Deliberately tiny: the thing
# being tested is a rate limit, so the probe must not be part of the problem.
#
# It does NOT resume the crawl. Restarting a multi-day pass unattended is a
# bigger decision than this script is allowed to make; it reports and stops.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"
DB="$REPO/archive/archive.db"
# LOG_DIR as its own variable, because the auto-resume block below needs the
# directory too. It referenced $LOG_DIR while only $LOG existed, and `set -u`
# killed the script immediately after it wrote "CLEAR" -- so the window opened,
# the probe saw it, and the crawl did not resume for 19 hours.
LOG_DIR="${PANDAS_LOG_DIR:-$HOME/.local/share/pandas-agent}"
LOG="$LOG_DIR/rate-limit-probe.log"
mkdir -p "$LOG_DIR"

q() { sqlite3 "file:$DB?mode=ro" "$1"; }

# A running pass already answers the question, and probing alongside it means two
# sessions against one account on one Chrome profile. On 2026-10-03 22:00 the
# probe did exactly that, then "auto-resumed" a service that had never stopped
# and spent one of the five resume attempts on it.
if systemctl --user is-active --quiet pandas-comments.service; then
  echo "$(date -Is)  SKIPPED: comments pass is running" >> "$LOG"
  exit 0
fi

before_raw=$(q "select coalesce(max(offset),-1) from raw")
before_comments=$(q "select count(*) from comments")
probe_started=$(date +%s)

# Bounded every way available: 2 posts, no media, generous delay, hard timeout.
# Keep the output. The first version sent it to /dev/null and then logged
# "rc=1 raw=+0 UNCLEAR" three times running -- a probe that reports only that it
# failed is the same silent-progress trap this whole exercise was about.
ERR="$(dirname "$LOG")/rate-limit-probe.last-run.txt"
set +e
# --profile ABSOLUTE, not the default relative ".chrome-profile". Under systemd
# the working directory is not the repo, so the relative form resolved to an
# empty directory, Chromium opened a fresh profile, and the run died on
# "Not logged in" without ever reaching Facebook -- three scheduled probes
# reporting UNCLEAR while the session was perfectly healthy.
# --profile is a TOP-LEVEL argument, so it precedes the subcommand.
timeout 300 "$PY" "$REPO/scrape.py" \
  --profile "$REPO/.chrome-profile" comments \
  --max-posts 4 --shuffle --no-media --headless --delay-min 10 --delay-max 20 \
  >"$ERR" 2>&1
rc=$?
set -e

after_raw=$(q "select coalesce(max(offset),-1) from raw")
comment_queries=$(q "select count(*) from raw where offset > $before_raw \
  and friendly like '%Comment%'")
after_comments=$(q "select count(*) from comments")
new_comments=$(( after_comments - before_comments ))

# The verdict comes from the payloads, not the exit code: GraphQL puts errors in
# a 200 body, which is the whole reason this was misread as a block for a day.
refusals=$("$PY" - "$REPO" "$before_raw" "$after_raw" <<'PY'
import gzip, json, re, sys, pathlib
repo, lo, hi = pathlib.Path(sys.argv[1]), int(sys.argv[2]) + 1, int(sys.argv[3])
d = repo / "archive"
legacy = d / "raw.ndjson.gz"
chunks = ([legacy] if legacy.exists() else []) + sorted(
    d.glob("raw-*.ndjson.gz"),
    key=lambda p: int(re.search(r"\.(\d+)\.ndjson\.gz$", p.name).group(1)))
n, off = 0, 0
for c in chunks:
    with gzip.open(c, "rt") as fh:
        for line in fh:
            if lo <= off <= hi:
                try:
                    rec = json.loads(line)
                except Exception:
                    off += 1; continue
                for e in (rec.get("errors") or []):
                    if e.get("code") == 1675004 or "rate limit" in (e.get("message") or "").lower():
                        n += 1
            off += 1
            if off > hi: break
    if off > hi: break
print(n)
PY
)

if [ "$refusals" -gt 0 ]; then
  verdict="LIMITED ($refusals refused queries)"
elif [ "$new_comments" -gt 0 ]; then
  verdict="CLEAR ($new_comments comments collected) -- safe to resume"
elif [ "$comment_queries" -eq 0 ]; then
  # Nothing was asked, so nothing was refused. Says nothing about the limit.
  verdict="NO SIGNAL (no comment query fired; sampled posts may be complete already)"
else
  # Queries went out, came back without an error, and carried no comments. Near
  # the end of a pass that is the expected answer, not an odd one: the queue is
  # then all residue -- posts already swept, a few hidden comments short -- and
  # re-opening them gains nothing by definition. On 2026-10-09 this branch logged
  # ODD for an open window and auto-resume never fired, because a residue-only
  # queue can never produce new comments and so could never say CLEAR.
  # So: if every post this probe opened had been opened before, call it clear.
  # A fresh post coming back empty is still ODD -- resuming on that could retire
  # real posts on a broken night.
  sampled=$(q "select count(*) from comment_sweeps where last_attempt >= $probe_started")
  fresh=$(q "select count(*) from comment_sweeps where last_attempt >= $probe_started and attempts <= 1")
  if [ "$sampled" -gt 0 ] && [ "$fresh" -eq 0 ]; then
    verdict="CLEAR ($comment_queries comment queries, no refusals; $sampled sampled posts were residue) -- safe to resume"
  else
    verdict="ODD ($comment_queries comment queries, no refusals, no comments; $fresh of $sampled sampled posts were fresh)"
  fi
fi

echo "$(date -Is)  rc=$rc raw=+$(( after_raw - before_raw )) comments=+$new_comments  $verdict" >> "$LOG"

# A non-zero exit with nothing captured means it never got as far as Facebook,
# which is a local fault rather than a rate-limit answer. Put the last line in
# the log so the log alone is enough to tell those apart.
if [ "$rc" -ne 0 ] && [ "$(( after_raw - before_raw ))" -eq 0 ]; then
  echo "    did not reach Facebook -- last line: $(tail -1 "$ERR" 2>/dev/null | cut -c1-160)" >> "$LOG"
  echo "    full output: $ERR" >> "$LOG"
fi

# ---------------------------------------------------------------- auto-resume
#
# Gated on a marker file, so this stays a read-only diagnostic unless somebody
# has explicitly asked for the crawl to be restarted when the window opens.
# A probe that silently starts multi-day jobs is not a probe.
#
# Attempts are capped. If the limit is still effectively closed, a resume will
# trip it again within minutes, and an uncapped loop would spend the whole
# window re-tripping it -- which is how you turn a rate limit into a longer
# rate limit.
RESUME_MARKER="$LOG_DIR/.auto-resume-comments"
ATTEMPTS_FILE="$LOG_DIR/.auto-resume-attempts"
MAX_RESUMES=5

case "$verdict" in
  CLEAR*)
    echo "rate limit has cleared"
    if [ ! -e "$RESUME_MARKER" ]; then
      echo "  auto-resume not enabled; resume by hand with:"
      echo "  cd $REPO && ./.venv/bin/python scrape.py comments --shuffle --delay-min 20 --delay-max 45"
      exit 0
    fi
    n=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)
    if [ "$n" -ge "$MAX_RESUMES" ]; then
      MSG="auto-resume gave up after $n attempts -- needs a human"
      echo "$(date -Is)  $MSG" >> "$LOG_DIR/comments-finish.log"
      command -v notify-send >/dev/null && notify-send -u critical "PANDAS crawl" "$MSG"
      exit 1
    fi
    echo $((n + 1)) > "$ATTEMPTS_FILE"

    # Slower than the run that earned the limit: 43 hours at ~47s/post tripped
    # it, so the delay goes up rather than staying put and hoping.
    # Start a SERVICE, not a background child. This probe is Type=oneshot, so
    # systemd reaps its whole cgroup when it exits -- a nohup'd crawl launched
    # here died within seconds of the probe finishing, twice, while the log
    # showed only the two startup lines. pandas-comments.service has its own
    # cgroup and outlives this script.
    systemctl --user start pandas-comments.service || exit 1
    sleep 10
    systemctl --user is-active --quiet pandas-comments.service || {
      echo "comments service failed to stay up -- see journalctl --user -u pandas-comments"
      exit 1
    }
    # The sleep lock takes itself only while a crawl is running, so it has to be
    # started after, not before.
    systemctl --user start pandas-inhibit-sleep.service 2>/dev/null || true
    # Let the finish watcher fire again for this new run.
    rm -f "$LOG_DIR/.comments-finish-notified"

    MSG="comments pass auto-resumed (attempt $((n+1))/$MAX_RESUMES) -- $after_comments comments so far"
    echo "$(date -Is)  $MSG  (pandas-comments.service)" >> "$LOG_DIR/comments-finish.log"
    command -v notify-send >/dev/null && notify-send "PANDAS crawl" "$MSG"
    echo "  $MSG"
    ;;
esac
