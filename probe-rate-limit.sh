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
set +e
timeout 300 "$PY" "$REPO/scrape.py" comments \
  --max-posts 2 --no-media --headless --delay-min 10 --delay-max 20 \
  >/dev/null 2>&1
rc=$?
set -e

after_raw=$(q "select coalesce(max(offset),-1) from raw")
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
else
  verdict="UNCLEAR (no refusals, no comments; look at the payloads by hand)"
fi

echo "$(date -Is)  rc=$rc raw=+$(( after_raw - before_raw )) comments=+$new_comments  $verdict" >> "$LOG"

# Only the CLEAR case needs a human, so only that one is loud.
case "$verdict" in
  CLEAR*) echo "rate limit has cleared -- resume with:"
          echo "  cd $REPO && ./.venv/bin/python scrape.py comments --delay-min 8 --delay-max 20" ;;
esac
