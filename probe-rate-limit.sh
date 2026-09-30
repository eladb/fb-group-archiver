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
LOG="${PANDAS_LOG_DIR:-$HOME/.local/share/pandas-agent}/rate-limit-probe.log"
mkdir -p "$(dirname "$LOG")"

q() { sqlite3 "file:$DB?mode=ro" "$1"; }

before_raw=$(q "select coalesce(max(offset),-1) from raw")
before_comments=$(q "select count(*) from comments")

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
  --max-posts 4 --no-media --headless --delay-min 10 --delay-max 20 \
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
  # Queries went out, came back without an error, and carried no comments. Not a
  # rate limit -- worth a look at the payloads before assuming anything.
  verdict="ODD ($comment_queries comment queries, no refusals, no comments)"
fi

echo "$(date -Is)  rc=$rc raw=+$(( after_raw - before_raw )) comments=+$new_comments  $verdict" >> "$LOG"

# A non-zero exit with nothing captured means it never got as far as Facebook,
# which is a local fault rather than a rate-limit answer. Put the last line in
# the log so the log alone is enough to tell those apart.
if [ "$rc" -ne 0 ] && [ "$(( after_raw - before_raw ))" -eq 0 ]; then
  echo "    did not reach Facebook -- last line: $(tail -1 "$ERR" 2>/dev/null | cut -c1-160)" >> "$LOG"
  echo "    full output: $ERR" >> "$LOG"
fi

# Only the CLEAR case needs a human, so only that one is loud.
case "$verdict" in
  CLEAR*) echo "rate limit has cleared -- resume with:"
          echo "  cd $REPO && ./.venv/bin/python scrape.py comments --delay-min 8 --delay-max 20" ;;
esac
