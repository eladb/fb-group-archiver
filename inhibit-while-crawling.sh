#!/usr/bin/env bash
# Hold a sleep inhibitor for exactly as long as the comments pass is running.
#
# The pass takes ~3 days of UPTIME. The machine suspended at 00:11 and woke at
# 06:50 on 2026-10-01, so the crawl advanced 10 posts in 6h40m of wall clock --
# and the per-post deadline, being wall-clock based, instantly tripped on resume
# and abandoned the post that was in flight.
#
# Self-releasing on purpose: the inhibitor lives inside systemd-inhibit, so when
# the wait loop ends the lock goes with it. A manually held `sleep infinity`
# inhibitor is a thing someone has to remember to kill, and nobody does.
set -uo pipefail

if ! pgrep -f "[s]crape.py comments" >/dev/null; then
  echo "comments pass is not running -- not taking a sleep lock"
  exit 0
fi

exec systemd-inhibit \
  --what=sleep \
  --who="pandas comments pass" \
  --why="multi-day comment collection; suspending wastes the post in flight" \
  --mode=block \
  bash -c 'while pgrep -f "[s]crape.py comments" >/dev/null; do sleep 60; done;
           echo "comments pass ended -- releasing the sleep lock"'
