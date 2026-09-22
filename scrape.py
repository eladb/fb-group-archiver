#!/usr/bin/env python3
"""Archive a Facebook group you belong to, locally.

Drives a real Chrome profile with Playwright and captures the group's GraphQL
traffic rather than parsing the DOM: the JSON carries full post text (the DOM
truncates it), reaction breakdowns, timestamps and CDN URLs, and it survives UI
rewrites that break selector-based scrapers.

Nothing leaves your machine. Run `login` once, then `crawl`.

The browser can also be a remote one that keeps its Facebook session between
runs, which is what makes this usable from a sandbox: a Sidekick box you own
(set SIDEKICK_TOKEN and it is used automatically -- see sidekick.py), or a
rented Browserbase session (--browserbase, opt-in every time -- see
browserbase.py).
"""

import argparse
import json
import math
import random
import re
import sys
import time
from collections import deque
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # keeps parsing/storage importable (and testable) without a browser
    sync_playwright = None

import browserbase
import sidekick
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

# How long `login` watches a remote browser for a hand-driven login when there
# is no terminal to press Enter at.
DEFAULT_LOGIN_WAIT = 900


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


# ---------------------------------------------------------------- browser

REMOTE_ERRORS = (sidekick.SidekickError, browserbase.BrowserbaseError)


class BrowserSession:
    """A browser to drive: the local Chrome profile, or a remote one.

    One surface for three cases. What differs is teardown, and each backend
    owns that decision: a local context is ours to close, a Sidekick box's must
    be left alone because it *is* the logged-in profile, and a Browserbase
    session has to be closed and released or the meter keeps running.
    """

    def __init__(self, pw, ctx, browser=None, remote=None):
        self.pw = pw
        self.ctx = ctx
        self.browser = browser
        self.remote = remote  # a sidekick.Sidekick or browserbase.Browserbase; None when local

    def page(self):
        return self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self) -> None:
        try:
            if self.remote is None:
                self.ctx.close()
            else:
                self.remote.close(self.browser)
        finally:
            self.pw.stop()


def pick_remote(args):
    """Which browser to drive. Only --browserbase is ever opt-in -- it spends
    metered minutes and runs from a datacenter IP, neither of which should
    happen because a key happened to be in the environment."""
    if getattr(args, "local", False):
        return None
    if getattr(args, "browserbase", False):
        return browserbase.load(required=True)
    return sidekick.load(required=getattr(args, "sidekick", False))


def open_browser(args, headless: bool = None) -> BrowserSession:
    """A persistent real-Chrome profile: stable fingerprint, session survives runs.

    Remotely that profile lives on a machine that outlives this process -- a
    Sidekick box or a Browserbase context -- which is the only way a sandboxed
    run gets a Facebook session at all. Locally it is a directory.
    """
    if sync_playwright is None:
        sys.exit("playwright is not installed -- see README.md")
    try:
        remote = pick_remote(args)
    except REMOTE_ERRORS as exc:
        sys.exit(str(exc))

    pw = sync_playwright().start()
    try:
        if remote is not None:
            log(remote.describe())
            if isinstance(remote, browserbase.Browserbase):
                remote.start(timeout=getattr(args, "bb_timeout", browserbase.DEFAULT_TIMEOUT),
                             proxy=getattr(args, "bb_proxy", False))
                log(f"session {remote.session_id} on context {remote.context_id}")
            else:
                remote.version()  # fail fast, and legibly, on a box that is gone
            browser, ctx = remote.connect(pw)
            log(f"attached to the {remote.label} browser over CDP")
            if headless or getattr(args, "headless", False):
                log(f"note: --headless is ignored on a {remote.label} browser (it runs headful)")
            return BrowserSession(pw, ctx, browser, remote)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile)),
            channel="chrome",
            headless=getattr(args, "headless", False) if headless is None else headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        return BrowserSession(pw, ctx)
    except REMOTE_ERRORS as exc:
        if remote is not None:
            remote.close()
        pw.stop()
        sys.exit(str(exc))
    except Exception:
        if remote is not None:
            remote.close()
        pw.stop()
        raise


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

def ingest(store: Store, capture: Capture, group_id: str, known: set) -> int:
    """Persist everything captured so far. Returns the count of new posts."""
    new_posts = 0
    for url, friendly, payload in capture.drain():
        raw_ref = store.append_raw(url, friendly, payload)
        posts, comments = harvest(payload, group_id, raw_ref)
        for p in posts:
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
    """Download queued CDN assets. Signed URLs expire in hours -- do this inline.

    The fetch carries the context's cookies but is issued by the Playwright
    driver rather than from inside the browser, so against a sidekick box it
    leaves from this machine, not the box. Signed CDN URLs are not IP-bound, so
    it works either way.
    """
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

def wait_for_login(sess, seconds: int, poll: float = 5.0) -> bool:
    """Poll the remote browser's cookies until someone logs in over there.

    The alternative -- blocking on Enter -- assumes whoever runs the CLI is also
    the one clicking through the login. With a remote browser they are often
    not: the crawl is driven from a sandbox while the human is in a browser tab
    somewhere else, with no terminal to press Enter at.
    """
    deadline = time.time() + seconds
    # Count down from a moving mark rather than on `remaining % 60`: each pass
    # costs a shade more than `poll` (the cookie read), so a modulo window gets
    # stepped over and the countdown skips minutes.
    next_notice = seconds - 60
    while time.time() < deadline:
        if is_logged_in(sess.ctx):
            return True
        time.sleep(poll)
        remaining = deadline - time.time()
        if remaining <= next_notice and remaining > poll:
            # Round up: with 3m58s left, "3 min" reads as less time than there
            # is, and someone mid-2FA is watching this line to decide whether
            # they have to hurry.
            left = f"{math.ceil(remaining / 60)} min" if remaining >= 60 else "under a min"
            log(f"still waiting for the login ({left} left)")
            next_notice = remaining - 60
    return is_logged_in(sess.ctx)


def cmd_login(args) -> None:
    # No terminal to press Enter at means polling is the only thing that works.
    wait = args.wait or (0 if sys.stdin.isatty() else DEFAULT_LOGIN_WAIT)
    sess = open_browser(args, headless=False)
    ctx = sess.ctx
    try:
        page = sess.page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        if sess.remote is not None:
            print(
                f"\n  Facebook is open in the {sess.remote.label} browser. Watch it and\n"
                "  click through the login here (the URL is a live handle on that\n"
                f"  browser -- keep it to yourself):\n\n    {sess.remote.view_url}\n"
                "\n  Complete any 2FA and dismiss the cookie banner. The session is\n"
                "  kept in the remote profile and reused by every later `crawl`.\n"
            )
        else:
            print(
                "\n  A Chrome window is open. Log into Facebook there by hand,\n"
                "  complete any 2FA, and dismiss the cookie banner.\n"
                "  The session is saved to the profile directory and reused by `crawl`.\n"
            )
        if wait:
            log(f"watching for the login for up to {wait // 60} min")
            ok = wait_for_login(sess, wait)
        else:
            print("  Press Enter here once you're logged in...")
            input()
            ok = is_logged_in(ctx)
        if ok:
            log("logged in -- session saved. You can close the browser.")
        else:
            log("WARNING: no c_user cookie found -- login may not have completed.")
    finally:
        sess.close()


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
    sess = open_browser(args)
    ctx = sess.ctx
    try:
        if not is_logged_in(ctx):
            raise SystemExit("Not logged in. Run `scrape.py login` first.")
        page = sess.page()
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
        sess.close()


def cmd_comments(args) -> None:
    """Second pass: open each post permalink and expand its comment threads."""
    store = Store(Path(args.out))
    sess = open_browser(args)
    ctx = sess.ctx
    expand_re = re.compile(args.expand_pattern, re.I)
    try:
        if not is_logged_in(ctx):
            raise SystemExit("Not logged in. Run `scrape.py login` first.")
        group_id = store.get_state("group_id")
        page = sess.page()
        capture = Capture()
        capture.attach(page)

        rows = store.db.execute(
            """SELECT p.id, p.url, p.comment_count,
                      (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS have
               FROM posts p
               WHERE p.comment_count > 0
                 AND p.url IS NOT NULL
                 AND have < p.comment_count
               ORDER BY p.created_at DESC"""
        ).fetchall()
        log(f"{len(rows)} posts with unfetched comments")

        for i, row in enumerate(rows, 1):
            if args.max_posts and i > args.max_posts:
                break
            try:
                page.goto(row["url"], wait_until="domcontentloaded")
                page.wait_for_timeout(int(jitter(2.0, 4.0) * 1000))
                for _ in range(args.max_expansions):
                    buttons = page.get_by_role("button").filter(has_text=expand_re)
                    if buttons.count() == 0:
                        break
                    buttons.first.click(timeout=5000)
                    page.wait_for_timeout(int(jitter(1.5, 3.5) * 1000))
            except Exception as exc:
                log(f"  {row['id']}: {str(exc)[:120]}")

            # Comments arriving from a permalink page belong to that post even
            # when the payload omits the association.
            for url, friendly, payload in capture.drain():
                raw_ref = store.append_raw(url, friendly, payload)
                posts, comments = harvest(payload, group_id, raw_ref)
                for p in posts:
                    store.upsert_post(p)
                for c in comments:
                    c["post_id"] = c.get("post_id") or row["id"]
                    store.upsert_comment(c)
                    for att in c.get("attachments") or []:
                        store.enqueue_media(att["url"], c["post_id"], att["kind"])
            store.commit()
            if args.media:
                drain_media(ctx, store)
            if i % 10 == 0:
                log(f"{i}/{len(rows)} posts -- {store.counts()}")
            time.sleep(jitter(args.delay_min, args.delay_max))
        log(f"done: {store.counts()}")
    finally:
        store.close()
        sess.close()


def cmd_media(args) -> None:
    """Drain the media queue on its own (e.g. after an interrupted crawl)."""
    store = Store(Path(args.out))
    sess = open_browser(args)
    try:
        total = 0
        while True:
            n = drain_media(sess.ctx, store, limit=50)
            if not n:
                break
            total += n
            log(f"{total} downloaded")
        log(f"done: {store.counts()}")
    finally:
        store.close()
        sess.close()


def cmd_reparse(args) -> None:
    """Re-run the normalizer over raw captures -- no network, no re-crawl."""
    store = Store(Path(args.out))
    group_id = store.get_state("group_id")
    posts = comments = 0
    for offset, payload in store.iter_raw():
        p, c = harvest(payload, group_id, offset)
        for item in p:
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


def cmd_sidekick(args) -> None:
    """Is the box up, and is its browser still logged into Facebook?"""
    try:
        sk = sidekick.load(required=True)
        version = sk.version()
    except sidekick.SidekickError as exc:
        raise SystemExit(f"sidekick: {exc}")
    log(f"{sk.host} is reachable: {version.get('Browser', 'unknown browser')}")
    if sk.view_url:
        print(f"  watch: {sk.view_url}")
    check_login(args)


def cmd_browserbase(args) -> None:
    """Project, quota and profile -- and whether that profile is still logged in.

    Opening the browser reports all of it. This starts a real session, so it
    spends a minute of quota; the live view is not printed here because the
    session is released on the way out. Use `login` for that.
    """
    check_login(args)


def check_login(args) -> None:
    """Attach to whichever browser the flags select and read its cookies."""
    if sync_playwright is None:
        log("playwright is not installed, so the login check is skipped")
        return
    sess = open_browser(args)
    try:
        log("logged into Facebook" if is_logged_in(sess.ctx)
            else "NOT logged in -- run `scrape.py login`")
    finally:
        sess.close()


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
    ap.add_argument("--sidekick", action="store_true",
                    help="require the remote Sidekick browser (used automatically "
                         "whenever SIDEKICK_TOKEN is set)")
    ap.add_argument("--browserbase", action="store_true",
                    help="drive a rented Browserbase session (never automatic: it is "
                         "metered and runs from a datacenter IP)")
    ap.add_argument("--bb-timeout", type=int, default=browserbase.DEFAULT_TIMEOUT,
                    help="Browserbase session cap in seconds (default: %(default)s)")
    ap.add_argument("--bb-proxy", action="store_true",
                    help="route the Browserbase session through its proxies (paid plans)")
    ap.add_argument("--local", action="store_true",
                    help="force the local Chrome profile, ignoring SIDEKICK_TOKEN")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, media_default=True):
        p.add_argument("--headless", action="store_true",
                       help="run without a visible window (likelier to trip checks)")
        p.add_argument("--delay-min", type=float, default=2.5)
        p.add_argument("--delay-max", type=float, default=6.0)
        p.add_argument("--media", dest="media", action="store_true", default=media_default)
        p.add_argument("--no-media", dest="media", action="store_false")

    p = sub.add_parser("login", help="open Chrome so you can log in by hand (run once)")
    p.add_argument("--wait", type=int, default=0,
                   help="watch for the login for N seconds instead of waiting on Enter "
                        f"(automatic, {DEFAULT_LOGIN_WAIT}s, when stdin is not a terminal)")
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
    p.add_argument("--expand-pattern", default=DEFAULT_EXPAND,
                   help="regex matching your UI language's expander buttons")
    add_common(p)
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("media", help="download any queued media")
    add_common(p)
    p.set_defaults(func=cmd_media)

    p = sub.add_parser("sidekick", help="check the Sidekick box and its Facebook session")
    add_common(p)
    p.set_defaults(func=cmd_sidekick, sidekick=True)

    p = sub.add_parser("browserbase", help="check the Browserbase project, context and session")
    add_common(p)
    p.set_defaults(func=cmd_browserbase, browserbase=True)

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
