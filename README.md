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
