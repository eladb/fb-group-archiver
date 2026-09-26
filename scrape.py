#!/usr/bin/env python3
"""Archive a Facebook group you belong to, locally.

Drives a real Chrome profile with Playwright and captures the group's GraphQL
traffic rather than parsing the DOM: the JSON carries full post text (the DOM
truncates it), reaction breakdowns, timestamps and CDN URLs, and it survives UI
rewrites that break selector-based scrapers.

Nothing leaves your machine. Run `login` once, then `crawl`.
"""

import argparse
import base64
import datetime as dt
import json
import random
import re
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # keeps parsing/storage importable (and testable) without a browser
    sync_playwright = None

from extract import harvest, typename_census
from store import Store

GRAPHQL_RE = re.compile(r"/api/graphql")
GROUP_ID_RE = re.compile(r"facebook\.com/groups/([^/?#]+)")
JSON_PREFIX_RE = re.compile(r"^\s*for\s*\(\s*;\s*;\s*\)\s*;")

# "View more comments" / "N replies" expanders. Facebook localizes these, so
# override with --expand-pattern if your account's UI language differs.
DEFAULT_EXPAND = (
    # "View more comments", "View 24 more comments", "View all 31 comments",
    # "View previous comments", "3 replies". Deliberately does NOT match a bare
    # "See more" -- that expands truncated body text, which the GraphQL capture
    # already carries in full.
    r"(view|see)\s+(all\s+)?(\d+\s+)?(more\s+|previous\s+)?(comments?|replies)"
    r"|\d+\s+repl(y|ies)"
    r"|עוד\s+תגובות|הצג\s+עוד\s+תגובות"
)


# The comment-ordering control, and the option within it that shows everything.
# Both are localized; override with --sort-pattern / --sort-choice if the UI is
# not in English.
DEFAULT_SORT_BUTTON = r"most relevant|newest|all comments|top comments|הכי רלוונטי|החדשות ביותר"
# Anchored at the start: the "Newest" option's own description reads "Show all
# comments with the newest comments first", so an unanchored "all comments"
# selects Newest and silently leaves the hidden/spam comments uncaptured.
DEFAULT_SORT_CHOICE = r"^\s*(all comments|כל התגובות)"

# Comment threads load in slices; these bound the per-post effort.
EXPANDS_PER_ROUND = 6      # reply expanders clicked before scrolling again
COMMENT_IDLE_ROUNDS = 3    # consecutive rounds yielding no new comments
POST_TIME_BUDGET = 150     # seconds; hard cap so one odd page cannot stall a run


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


# ---------------------------------------------------------------- browser

def open_browser(profile: Path, headless: bool = False):
    """A persistent real-Chrome profile: stable fingerprint, session survives runs."""
    if sync_playwright is None:
        sys.exit("playwright is not installed -- see README.md")
    pw = sync_playwright().start()
    opts = dict(
        user_data_dir=str(profile),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    # Real Chrome is preferred (stable fingerprint) but is not distributed for
    # every platform -- e.g. there is no Chrome for Linux/arm64. Fall back to
    # the Chromium that `playwright install chromium` provides.
    try:
        ctx = pw.chromium.launch_persistent_context(channel="chrome", **opts)
    except Exception:
        log("Google Chrome not available; falling back to bundled Chromium.")
        ctx = pw.chromium.launch_persistent_context(**opts)
    return pw, ctx


def is_logged_in(ctx) -> bool:
    return any(c["name"] == "c_user" for c in ctx.cookies("https://www.facebook.com"))


class Capture:
    """Buffers GraphQL response bodies off the event handler for the main loop."""

    def __init__(self):
        self.queue = deque()

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, resp):
        if not GRAPHQL_RE.search(resp.url):
            return
        try:
            body = resp.text()
        except Exception:
            return  # body already discarded; nothing to do
        friendly = ""
        try:
            post_data = resp.request.post_data or ""
            m = re.search(r"fb_api_req_friendly_name=([^&]+)", post_data)
            if m:
                friendly = m.group(1)
        except Exception:
            pass
        self.queue.append((resp.url, friendly, body))

    def drain(self):
        """Yield (url, friendly, payload) for each captured JSON document.

        A single GraphQL response can contain several newline-delimited JSON
        documents (Facebook streams @defer chunks that way).
        """
        while self.queue:
            url, friendly, body = self.queue.popleft()
            body = JSON_PREFIX_RE.sub("", body)
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield url, friendly, json.loads(line)
                except json.JSONDecodeError:
                    continue


# ---------------------------------------------------------------- pipeline

def in_group(post: dict, group_id: str) -> bool:
    """Is this post actually part of the group being archived?

    A permalink page carries Facebook's recommendations alongside the post --
    reels and videos from elsewhere entirely. Those nodes parse perfectly well,
    so without this check they are stored as group content, stamped with the
    crawl's group id, and then queued for a comment pass that can never fetch
    them (a reel is a different page type). One live capture picked up 18 such
    reels carrying 64,942 phantom comments: 89% of the apparent remaining work,
    none of it real, and retried forever because `have < comment_count` stays
    true.
    """
    if not group_id:
        return True
    url = post.get("url") or ""
    return f"/groups/{group_id}/" in url or f"/groups/{group_id}" == url.rstrip("/")


def ingest(store: Store, capture: Capture, group_id: str, known: set) -> int:
    """Persist everything captured so far. Returns the count of new posts."""
    new_posts = 0
    for url, friendly, payload in capture.drain():
        raw_ref = store.append_raw(url, friendly, payload)
        posts, comments = harvest(payload, group_id, raw_ref)
        for p in posts:
            if not in_group(p, group_id):
                continue
            if store.upsert_post(p):
                new_posts += 1
                known.add(p["id"])
            for att in p.get("attachments") or []:
                store.enqueue_media(att["url"], p["id"], att["kind"])
        for c in comments:
            store.upsert_comment(c)
            for att in c.get("attachments") or []:
                store.enqueue_media(att["url"], c.get("post_id"), att["kind"])
    store.commit()
    return new_posts


def ext_for(url: str, content_type: str) -> str:
    for cand in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm"):
        if cand in url.lower():
            return cand
    if "video" in content_type:
        return ".mp4"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def drain_media(ctx, store: Store, limit: int = 25) -> int:
    """Download queued CDN assets. Signed URLs expire in hours -- do this inline."""
    saved = 0
    for row in store.pending_media(limit):
        url = row["src_url"]
        try:
            resp = ctx.request.get(url, timeout=45000)
            if resp.status != 200:
                store.fail_media(url, f"http {resp.status}")
                continue
            body = resp.body()
            ctype = (resp.headers or {}).get("content-type", "")
            store.save_media(url, row["post_id"], row["kind"], body, ext_for(url, ctype))
            saved += 1
        except Exception as exc:
            store.fail_media(url, str(exc)[:200])
        time.sleep(jitter(0.3, 1.0))
    store.commit()
    return saved


# ---------------------------------------------------------------- commands

def cmd_login(args) -> None:
    pw, ctx = open_browser(Path(args.profile), headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    print(
        "\n  A Chrome window is open. Log into Facebook there by hand,\n"
        "  complete any 2FA, and dismiss the cookie banner.\n"
        "  The session is saved to the profile directory and reused by `crawl`.\n"
    )
    if sys.stdin.isatty():
        print("  Press Enter here once you're logged in...")
        input()
    else:
        # No terminal to press Enter in (launched from a GUI, an agent, a pipe).
        # Poll for the session cookie instead and exit as soon as it appears.
        deadline = time.time() + args.wait * 60
        log(f"Waiting up to {args.wait:g} min for login to complete...")
        while time.time() < deadline:
            if is_logged_in(ctx):
                break
            time.sleep(3)
    if is_logged_in(ctx):
        log("Session saved. You can close the browser.")
    else:
        log("WARNING: no c_user cookie found -- login may not have completed.")
    ctx.close()
    pw.stop()


def force_all_comments(page, button_re, choice_re) -> bool:
    """Switch a post's thread to "All comments". Returns True if it took.

    This is load-bearing for two separate reasons. A post with only a handful
    of comments is server-rendered and never issues a /api/graphql request, so
    interception sees nothing at all until some interaction forces a refetch.
    And the default ordering omits comments Facebook judges low quality or
    spammy -- which the comment_count still counts, so the archive silently
    lands short without it.
    """
    try:
        buttons = page.get_by_role("button").filter(has_text=button_re)
        if buttons.count() == 0:
            return False
        buttons.first.click(timeout=5000)
        page.wait_for_timeout(int(jitter(1.2, 2.2) * 1000))
        items = page.get_by_role("menuitem").filter(has_text=choice_re)
        if items.count() == 0:
            page.keyboard.press("Escape")
            return False
        items.first.click(timeout=5000)
        page.wait_for_timeout(int(jitter(2.5, 4.0) * 1000))
        return True
    except Exception:
        return False


def resolve_group(page, url: str) -> str:
    """Follow a share link to its canonical /groups/<id>/ form."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    m = GROUP_ID_RE.search(page.url)
    if not m:
        raise SystemExit(
            f"Could not resolve a group id from {page.url!r}. "
            "Are you logged in, and is this account a member of the group?"
        )
    return m.group(1)


def cmd_crawl(args) -> None:
    store = Store(Path(args.out))
    pw, ctx = open_browser(Path(args.profile), headless=args.headless)
    try:
        if not is_logged_in(ctx):
            raise SystemExit("Not logged in. Run `scrape.py login` first.")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        capture = Capture()
        capture.attach(page)

        group_id = store.get_state("group_id") or resolve_group(page, args.group)
        store.set_state("group_id", group_id)
        log(f"group id: {group_id}")

        # Chronological ordering makes the crawl a stable sweep rather than a
        # re-shuffled "top posts" feed that repeats and drops items.
        feed = f"https://www.facebook.com/groups/{group_id}/?sorting_setting=CHRONOLOGICAL"
        page.goto(feed, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        known = store.known_post_ids()
        log(f"resuming with {len(known)} posts already archived")

        started = time.time()
        idle_rounds = 0
        rounds = 0
        while True:
            rounds += 1
            new = ingest(store, capture, group_id, known)
            if args.media:
                drain_media(ctx, store)

            idle_rounds = 0 if new else idle_rounds + 1
            if new:
                log(f"round {rounds}: +{new} posts (total {len(known)})")

            if idle_rounds >= args.max_idle:
                log(f"no new posts in {args.max_idle} rounds -- stopping")
                break
            if args.max_posts and len(known) >= args.max_posts:
                log(f"reached --max-posts {args.max_posts}")
                break
            if args.max_minutes and (time.time() - started) / 60 >= args.max_minutes:
                log(f"reached --max-minutes {args.max_minutes}")
                break

            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(jitter(args.delay_min, args.delay_max) * 1000))

            # Periodic longer pause: sustained uniform scrolling is the pattern
            # Meta's automation heuristics key on.
            if rounds % 40 == 0:
                pause = jitter(30, 90)
                log(f"cooling down {pause:.0f}s")
                store.commit()
                time.sleep(pause)

        ingest(store, capture, group_id, known)
        if args.media:
            while drain_media(ctx, store, limit=50):
                pass
        log(f"done: {store.counts()}")
    finally:
        store.close()
        ctx.close()
        pw.stop()


def cmd_comments(args) -> None:
    """Second pass: open each post permalink and expand its comment threads."""
    store = Store(Path(args.out))
    pw, ctx = open_browser(Path(args.profile), headless=args.headless)
    expand_re = re.compile(args.expand_pattern, re.I)
    sort_button_re = re.compile(args.sort_pattern, re.I)
    sort_choice_re = re.compile(args.sort_choice, re.I)
    try:
        if not is_logged_in(ctx):
            raise SystemExit("Not logged in. Run `scrape.py login` first.")
        group_id = store.get_state("group_id")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        capture = Capture()
        capture.attach(page)

        rows = store.db.execute(
            """SELECT p.id, p.url, p.comment_count,
                      (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS have
               FROM posts p
               WHERE p.comment_count > 0
                 AND p.url IS NOT NULL
                 AND p.url LIKE '%/groups/%'
                 AND have < p.comment_count
               -- Never-fetched posts first. A post whose capture came within a
               -- comment or two of its reported count never satisfies
               -- `have < comment_count` (the count and what is fetchable differ
               -- slightly), so it stays queued forever. Newest-first ordering
               -- then puts that permanent residue at the head of the queue, and
               -- every restart re-walks it, re-fetching comments already stored
               -- and recording nothing new.
               ORDER BY (have > 0), p.created_at DESC"""
        ).fetchall()
        log(f"{len(rows)} posts with unfetched comments")

        short_circuited = 0
        for i, row in enumerate(rows, 1):
            if args.max_posts and i > args.max_posts:
                break
            def absorb() -> int:
                """Persist whatever has been captured; return the new-comment count.

                Comments arriving from a permalink page belong to that post even
                when the payload omits the association.
                """
                fresh = 0
                for url, friendly, payload in capture.drain():
                    raw_ref = store.append_raw(url, friendly, payload)
                    posts, comments = harvest(payload, group_id, raw_ref)
                    for p_ in posts:
                        if in_group(p_, group_id):
                            store.upsert_post(p_)
                    for c in comments:
                        c["post_id"] = c.get("post_id") or row["id"]
                        if store.upsert_comment(c):
                            fresh += 1
                        for att in c.get("attachments") or []:
                            store.enqueue_media(att["url"], c["post_id"], att["kind"])
                return fresh

            try:
                page.goto(row["url"], wait_until="domcontentloaded")
                page.wait_for_timeout(int(jitter(2.0, 4.0) * 1000))

                # Drain what the permalink render already fired BEFORE doing any
                # work, and stop here if it satisfied the post. A short thread
                # arrives complete in that first payload, and most posts are
                # short -- about three quarters have 20 comments or fewer, yet
                # each paid the same 12-20s of fixed overhead as a 300-comment
                # thread: sort switch, expansion round, scroll.
                #
                # The test is exactly the one the queue uses to decide a post
                # still needs work, so this can never skip something the next
                # run would re-open. It also makes FEWER requests rather than
                # more, which matters when the constraint is the account rather
                # than the pipe.
                absorb()
                have = store.db.execute(
                    "SELECT COUNT(*) FROM comments WHERE post_id=?", (row["id"],)
                ).fetchone()[0]
                if have >= (row["comment_count"] or 0):
                    short_circuited += 1
                else:
                    if not force_all_comments(page, sort_button_re, sort_choice_re):
                        log(f"  {row['id']}: could not switch to All comments")
                    absorb()
                    # Facebook paginates top-level comments by SCROLL, not by a
                    # button -- a permalink renders only the first slice and loads
                    # the next as the viewport nears the end of the thread. Clicking
                    # expanders alone therefore captures the first slice and its
                    # replies, then silently stops. Alternate expanding and
                    # scrolling, and give up once consecutive rounds add nothing.
                    stale = 0
                    deadline = time.time() + POST_TIME_BUDGET
                    for _ in range(args.max_expansions):
                        clicks = 0
                        while clicks < EXPANDS_PER_ROUND:
                            buttons = page.get_by_role("button").filter(has_text=expand_re)
                            if buttons.count() == 0:
                                break
                            try:
                                buttons.first.click(timeout=5000)
                            except Exception:
                                break
                            clicks += 1
                            page.wait_for_timeout(int(jitter(1.0, 2.2) * 1000))
                        page.mouse.wheel(0, 4000)
                        page.wait_for_timeout(int(jitter(1.5, 3.0) * 1000))
                        # Progress means NEW COMMENTS, not clicks. An expander that
                        # matches but never disappears would otherwise reset the idle
                        # counter every round and burn the full budget yielding
                        # nothing -- five minutes per post, silently.
                        if absorb() == 0:
                            stale += 1
                            if stale >= COMMENT_IDLE_ROUNDS:
                                break
                        else:
                            stale = 0
                        if time.time() > deadline:
                            log(f"  {row['id']}: time budget reached, moving on")
                            break
            except Exception as exc:
                log(f"  {row['id']}: {str(exc)[:120]}")

            absorb()
            store.commit()
            if args.media:
                drain_media(ctx, store)
            if i % 10 == 0:
                log(f"{i}/{len(rows)} posts -- {store.counts()}")
            time.sleep(jitter(args.delay_min, args.delay_max))
        log(f"done: {store.counts()}; {short_circuited}/{len(rows)} complete on open")
    finally:
        store.close()
        ctx.close()
        pw.stop()


def cmd_media(args) -> None:
    """Drain the media queue on its own (e.g. after an interrupted crawl)."""
    store = Store(Path(args.out))
    pw, ctx = open_browser(Path(args.profile), headless=args.headless)
    try:
        total = 0
        while True:
            n = drain_media(ctx, store, limit=50)
            if not n:
                break
            total += n
            log(f"{total} downloaded")
        log(f"done: {store.counts()}")
    finally:
        store.close()
        ctx.close()
        pw.stop()



# ---------------------------------------------------------------- backfill

# Facebook's group search accepts a creation-time filter, encoded as base64 of a
# JSON envelope. Month and week bounds are honoured; narrower ones are not --
# two- and three-day windows were observed returning posts months outside the
# range -- so the window is a hint and every post is re-checked against it.
#
# Search also caps a single query at ~64 results regardless of how much matches,
# and ignores some very common words entirely (a, I, is return nothing). One
# query therefore cannot enumerate a busy week: the way to approach completeness
# is many terms over a narrow window, unioned by post id, until new terms stop
# contributing.
BACKFILL_TERMS = [
    "the", "to", "and", "my", "for", "he", "she", "was", "with", "have",
    "not", "you", "her", "his", "that", "this", "son", "daughter", "doctor",
    "help", "anyone", "pandas", "pans", "flare", "strep", "test", "treatment",
]


def search_filter(start, end) -> str:
    """base64 creation-time filter for a [start, end] date window."""
    args = json.dumps({
        "start_year": str(start.year),
        "start_month": f"{start.year}-{start.month}",
        "start_day": f"{start.year}-{start.month}-{start.day}",
        "end_year": str(end.year),
        "end_month": f"{end.year}-{end.month}",
        "end_day": f"{end.year}-{end.month}-{end.day}",
    })
    envelope = {"rp_creation_time:0": json.dumps({"name": "creation_time", "args": args})}
    return base64.b64encode(json.dumps(envelope).encode()).decode()


def cmd_backfill(args) -> None:
    """Walk backwards through history with date-windowed search.

    The feed stalls a few weeks back -- a Facebook-side depth ceiling -- so the
    only route to older posts is the group's own search, bounded by date and
    unioned across query terms. Resumable: completed windows are recorded in
    state, so an interrupted run skips what it already swept.
    """
    store = Store(Path(args.out))
    pw, ctx = open_browser(Path(args.profile), headless=args.headless)
    try:
        if not is_logged_in(ctx):
            raise SystemExit("Not logged in. Run `scrape.py login` first.")
        group_id = store.get_state("group_id") or resolve_group(
            ctx.pages[0] if ctx.pages else ctx.new_page(), args.group)
        store.set_state("group_id", group_id)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        capture = Capture()
        capture.attach(page)

        done = set(json.loads(store.get_state("backfill_windows") or "[]"))
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
        end = dt.date.fromisoformat(args.until)
        start = dt.date.fromisoformat(args.since)
        deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None

        # Newest window first: recent history is the most likely to be wanted,
        # and it verifies the machinery against data the feed already proved.
        windows = []
        cur_end = end
        while cur_end >= start:
            cur_start = max(start, cur_end - dt.timedelta(days=args.window_days - 1))
            windows.append((cur_start, cur_end))
            cur_end = cur_start - dt.timedelta(days=1)

        log(f"{len(windows)} windows of {args.window_days}d from {start} to {end}; "
            f"{len(done)} already swept")

        for w_start, w_end in windows:
            key = f"{w_start}:{w_end}"
            if key in done:
                continue
            if deadline and time.time() > deadline:
                log("time budget reached; rerun to continue")
                break

            filt = search_filter(w_start, w_end)
            seen_before = store.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            for term in terms:
                if deadline and time.time() > deadline:
                    break
                url = (f"https://www.facebook.com/groups/{group_id}/search/"
                       f"?q={urllib.parse.quote(term)}&filters={filt}")
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(int(jitter(4.0, 7.0) * 1000))
                    stale = 0
                    for _ in range(args.max_scrolls):
                        page.mouse.wheel(0, 5000)
                        page.wait_for_timeout(int(jitter(1.8, 3.2) * 1000))
                        if ingest(store, capture, group_id, set()) == 0:
                            stale += 1
                            if stale >= 3:
                                break
                        else:
                            stale = 0
                except Exception as exc:
                    log(f"  {key} q={term}: {str(exc)[:100]}")
                time.sleep(jitter(args.delay_min, args.delay_max))

            gained = store.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] - seen_before
            done.add(key)
            store.set_state("backfill_windows", json.dumps(sorted(done)))
            store.commit()
            log(f"  {key}: +{gained} new posts (total {store.counts()['posts']})")
            time.sleep(jitter(args.delay_min, args.delay_max) * 2)

        log(f"backfill pass done: {store.counts()}")
    finally:
        store.close()
        ctx.close()
        pw.stop()


def cmd_reparse(args) -> None:
    """Re-run the normalizer over raw captures -- no network, no re-crawl."""
    store = Store(Path(args.out))
    group_id = store.get_state("group_id")
    posts = comments = 0
    for offset, payload in store.iter_raw():
        p, c = harvest(payload, group_id, offset)
        for item in p:
            if not in_group(item, group_id):
                continue
            store.upsert_post(item)
            posts += 1
        for item in c:
            store.upsert_comment(item)
            comments += 1
        if offset % 500 == 0:
            store.commit()
    store.commit()
    log(f"reparsed {posts} post nodes, {comments} comment nodes -- {store.counts()}")
    store.close()


def cmd_inspect(args) -> None:
    """Census of __typenames in the raw capture, to debug a parsing gap."""
    store = Store(Path(args.out))
    census = {}
    n = 0
    for _, payload in store.iter_raw():
        n += 1
        for tn, count in typename_census(payload).items():
            census[tn] = census.get(tn, 0) + count
    log(f"{n} payloads")
    for tn, count in sorted(census.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {count:8d}  {tn}")
    store.close()


def cmd_export(args) -> None:
    """Flat JSONL export with comments and local media paths attached."""
    store = Store(Path(args.out))
    dest = Path(args.out) / "export.jsonl"
    with dest.open("w", encoding="utf-8") as fh:
        for post in store.db.execute("SELECT * FROM posts ORDER BY created_at"):
            rec = dict(post)
            rec["attachments"] = json.loads(rec.get("attachments") or "[]")
            rec["comments"] = [
                dict(c) for c in store.db.execute(
                    "SELECT id, parent_id, author_id, author_name, created_at, text "
                    "FROM comments WHERE post_id=? ORDER BY created_at", (post["id"],)
                )
            ]
            rec["media"] = [
                dict(m) for m in store.db.execute(
                    "SELECT sha256, kind, path, bytes FROM media "
                    "WHERE post_id=? AND error IS NULL", (post["id"],)
                )
            ]
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"wrote {dest}")
    store.close()


def cmd_status(args) -> None:
    store = Store(Path(args.out))
    counts = store.counts()
    print(json.dumps(counts, indent=2))
    row = store.db.execute(
        "SELECT MIN(created_at) lo, MAX(created_at) hi FROM posts WHERE created_at > 0"
    ).fetchone()
    if row and row["lo"]:
        fmt = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
        print(f"date range: {fmt(row['lo'])} .. {fmt(row['hi'])}")
    store.close()


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="archive", help="output directory (default: archive)")
    ap.add_argument("--profile", default=".chrome-profile",
                    help="persistent Chrome profile dir (default: .chrome-profile)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, media_default=True):
        p.add_argument("--headless", action="store_true",
                       help="run without a visible window (likelier to trip checks)")
        p.add_argument("--delay-min", type=float, default=2.5)
        p.add_argument("--delay-max", type=float, default=6.0)
        p.add_argument("--media", dest="media", action="store_true", default=media_default)
        p.add_argument("--no-media", dest="media", action="store_false")

    p = sub.add_parser("login", help="open Chrome so you can log in by hand (run once)")
    p.add_argument("--wait", type=float, default=15,
                   help="minutes to wait for login when there is no terminal (default: 15)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("crawl", help="sweep the group feed")
    p.add_argument("--group", required=True, help="group URL or share link")
    p.add_argument("--max-posts", type=int, default=0, help="stop after N posts (0 = no limit)")
    p.add_argument("--max-minutes", type=float, default=0, help="stop after N minutes")
    p.add_argument("--max-idle", type=int, default=8,
                   help="stop after N scroll rounds yielding nothing new")
    add_common(p)
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("comments", help="second pass: expand comment threads per post")
    p.add_argument("--max-posts", type=int, default=0)
    p.add_argument("--max-expansions", type=int, default=25,
                   help="max 'view more comments' clicks per post")
    p.add_argument("--sort-pattern", default=DEFAULT_SORT_BUTTON,
                   help="regex matching the comment-ordering button in your UI language")
    p.add_argument("--sort-choice", default=DEFAULT_SORT_CHOICE,
                   help="regex matching the 'All comments' menu option in your UI language")
    p.add_argument("--expand-pattern", default=DEFAULT_EXPAND,
                   help="regex matching your UI language's expander buttons")
    add_common(p)
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("backfill",
                       help="walk older history via date-windowed search (past the feed's ceiling)")
    p.add_argument("--group", required=True, help="group URL or share link")
    p.add_argument("--since", required=True, help="oldest date to sweep, YYYY-MM-DD")
    p.add_argument("--until", required=True, help="newest date to sweep, YYYY-MM-DD")
    p.add_argument("--window-days", type=int, default=7,
                   help="days per search window (default 7; search caps at ~64 hits per query)")
    p.add_argument("--terms", default=",".join(BACKFILL_TERMS),
                   help="comma-separated query terms to union over each window")
    p.add_argument("--max-scrolls", type=int, default=20)
    p.add_argument("--max-minutes", type=float, default=0, help="stop cleanly after N minutes")
    add_common(p, media_default=False)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("media", help="download any queued media")
    add_common(p)
    p.set_defaults(func=cmd_media)

    for name, fn, helptext in (
        ("reparse", cmd_reparse, "re-normalize raw captures after a schema change"),
        ("inspect", cmd_inspect, "census of __typenames in raw capture"),
        ("export", cmd_export, "write export.jsonl"),
        ("status", cmd_status, "show archive counts"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=fn)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
