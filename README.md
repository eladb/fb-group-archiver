# fbgroup — local archive of a Facebook group you belong to

Captures the group's GraphQL traffic from a real, logged-in Chrome profile and
writes everything to disk on your machine. No cloud service, no cookie handoff
to a third party, nothing leaves the box.

## Why GraphQL interception and not DOM scraping

Facebook renders the group feed from `POST /api/graphql/` responses. Reading
those directly gets you:

- **full post text** — the DOM truncates long posts behind "See more"
- **real timestamps** — the DOM shows "3d" and localized relative strings
- **reaction/comment/share counts** and author IDs as structured fields
- **CDN URLs** for every attachment

and it keeps working through UI rewrites, which is what breaks selector-based
scrapers every few months.

Raw payloads are persisted verbatim to `raw.ndjson.gz` *before* parsing. If
Facebook reshapes its schema and the normalizer misses fields, you fix
`extract.py` and run `reparse` — you never re-crawl.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium     # skip if you want to use your system Chrome
cd fbgroup
```

The scripts import each other by module name, so run them from this directory.

## Use

```bash
# 1. Log in by hand, once. A Chrome window opens; complete 2FA; press Enter.
python scrape.py login

# 2. Sweep the feed. Resumable — re-run it and it picks up where it stopped.
python scrape.py crawl --group "https://www.facebook.com/share/g/XXXXXXXX/"

# 3. Second pass for full comment threads (slow; run it after the feed sweep).
python scrape.py comments

# 4. Check state / flatten to JSONL.
python scrape.py status
python scrape.py export
```

Useful flags:

| flag | effect |
|---|---|
| `--no-media` | metadata and text only; skips photo/video download |
| `--max-minutes 45` | bounded session, then stop cleanly |
| `--max-posts 500` | good for a first trial run |
| `--delay-min/--delay-max` | scroll pacing in seconds (default 2.5–6.0) |
| `--expand-pattern` | regex for "view more comments" in your UI language |

## Driving a remote browser (Sidekick)

The hard requirement here is a Chrome profile that stays logged into Facebook
between runs. On your own laptop that is `.chrome-profile/`. From a sandbox — a
CI job, a cloud agent — there is no window to log into and nothing survives the
run, so instead you point the scraper at a [Sidekick](https://github.com/eladb/sidekick)
box: a small server you own running a real headful Chromium behind a Cloudflare
tunnel, reachable over CDP.

```bash
export SIDEKICK_TOKEN=...            # printed by sidekick's scripts/install.sh

python scrape.py sidekick            # is the box up, and still logged in?
python scrape.py login               # opens FB there; you click through in the watch URL
python scrape.py crawl --group ...   # same as before, driving the remote browser
```

`SIDEKICK_TOKEN` is used automatically whenever it is set. `--local` ignores it
and uses the local profile; `--sidekick` makes its absence an error instead of a
silent fallback.

What changes in this mode:

- **Cookies live on the box, not in `.chrome-profile/`.** `login` is still a
  one-time manual step, but you do it in the box's browser through the token's
  watch URL (noVNC in a browser tab). The session then outlives every run.
- **Page traffic comes from the box's IP** — one consistent location for one
  account, instead of a session that hops networks between runs. Media downloads
  are the exception: Playwright issues those from the driver process (wherever
  you run the CLI) using the browser's cookies, not from inside the remote
  browser. Signed CDN URLs don't check the caller's IP, so they work; if you
  would rather every byte came from one address, run the CLI on the box itself
  through the token's shell endpoint.
- **The context is never closed.** Teardown just drops the CDP connection;
  closing the box's browser would throw away the profile the next run needs.
- `--headless` is ignored: the box runs headful under a virtual display, which
  is also the less bannable configuration.

The token is a full credential for that machine — it embeds the bearer secret in
every endpoint URL. Keep it out of the repo (it is env-only by design, and
nothing here logs it), and remember that "nothing leaves your machine" becomes
"nothing leaves machines you control" once a box is in the loop. A
`trycloudflare.com` quick-tunnel hostname dies with the `cloudflared` process
that minted it, so a token that stops resolving usually means the tunnel
rotated, not that the box is gone: re-run the installer and export the new one.

## Output layout

```
archive/
  archive.db        SQLite: posts, comments, media, media_queue, raw, state
  raw.ndjson.gz     every GraphQL payload, verbatim, append-only
  media/ab/cd/<sha256>.jpg
  export.jsonl      posts with comments + local media paths inlined
```

Media is content-addressed by SHA-256, so duplicates across posts cost one copy.

## Tests

```bash
pip install pytest
pytest
```

They cover the parts that can be checked without a live session — payload
normalization, storage idempotency and resume, GraphQL stream splitting, and the
comment-expander regex. No network, no browser; Playwright doesn't need to be
installed for them to run.

What they deliberately pin:

- **Resume safety** — offsets continue across a reopen, `known_post_ids` drives
  dedupe, and a partial re-read never blanks text you already captured.
- **Stream splitting** — one GraphQL response can carry several newline-delimited
  `@defer` chunks, plus a `for (;;);` anti-hijack prefix.
- **Degradation** — missing authors, absent feedback blocks and error payloads
  yield nulls rather than exceptions, because a crawl must not die on one odd node.
- **Media queue** — expired signed URLs retry three times then stop, and
  identical bytes are stored once.
- **Sidekick token handling** — an unpadded or malformed token fails with a
  sentence instead of a traceback, an absent one falls back to local Chrome
  rather than erroring, and the line that goes into logs never carries the
  secret.

What they can't cover: whether the structural predicates in `extract.py` match
the shapes Facebook is actually serving today. The fixtures encode the shapes as
documented here, not as observed live. That's what the first small run plus
`inspect` is for.

## Things that will actually bite you

**Signed CDN URLs expire** within hours. That's why downloads are interleaved
with the crawl instead of deferred — a URL list harvested today is dead links
tomorrow. If a run is interrupted, `python scrape.py media` drains whatever is
still queued, but do it promptly.

**Pacing is the ban vector.** Meta's automation heuristics key on sustained
uniform request patterns. The defaults randomize scroll delays and take a
30–90s cooldown every 40 rounds. Don't drop them much, don't run it headless
(`--headless` is likelier to trip checks), and don't run two sessions at once.
A tripped check is usually a 24–72h lock with identity re-verification.

**The feed has a depth ceiling.** A long scroll degrades and eventually stalls
well before the beginning of a large, old group — this is a Facebook-side limit,
not a bug here. The crawl sorts chronologically so the sweep is at least stable
and resumable, but if `status` shows the earliest post is nowhere near the
group's founding date, the fix is date-windowed crawling: drive the group's own
search with month-by-month bounds and union results by post ID. Not implemented
yet, because whether you need it depends on how deep the scroll actually gets.

**Comment expansion is localized.** The default `--expand-pattern` covers
English and some Hebrew. If your Facebook UI is in another language, pass a
regex that matches your "view more comments" / "N replies" buttons, or the
second pass will silently collect only first-page comments.

**The normalizer is heuristic.** `extract.py` recognizes nodes structurally
(`__typename == "Story"`, `post_id` + `feedback`, …) rather than by fixed
paths, because the paths move. If posts come back with null authors or missing
text, run `python scrape.py inspect` to see which `__typename`s are actually
present in the capture, adjust the predicates, and `reparse`.

## Untested against live Facebook

This was written without a Facebook session available, so the code has not been
exercised end-to-end against real traffic. The interception, storage, resume and
re-parse machinery is straightforward; the part most likely to need adjustment
is the field extraction in `extract.py`, since those payload shapes are
undocumented and change.

Do a small run first — `crawl --max-posts 50 --no-media` — then `status` and
`inspect`. If posts land with null `text` or `author_name`, that's the
normalizer needing a tweak against the shapes in your capture, and the raw file
already has everything needed to fix it offline.

## Two caveats worth keeping in mind

Logged-in scraping breaches Meta's Terms of Service. The *Meta v. Bright Data*
ruling that people cite for "scraping is legal" explicitly does **not** cover
this — the court found Bright Data was scraping while logged *out*, and declined
to rule on data behind a login. Realistic exposure is account action, not
litigation, but use an account you can afford to lose.

A private group's contents are other people's personal data. If this is anything
beyond purely personal use and you're in the EU or Israel, you become the
controller of that dataset. Deciding up front whether you need author identities
at all is much easier than scrubbing them later — `--no-media` plus dropping
`author_id`/`author_name` in `extract.py` gets you a pseudonymous corpus.
