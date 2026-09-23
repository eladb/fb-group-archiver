#!/usr/bin/env python3
"""Render a browsable sample of real threads from the archive.

Writes archive/sample-threads.html: a spread of real posts with their full
comment trees, chosen across the size range so the sample shows what the corpus
actually looks like rather than only its busiest threads.

    ./.venv/bin/python make-sample.py [--threads N] [--out PATH]

The output contains real group content. Author identities are pseudonyms and
@-mentions are rewritten, but free text still names schools, clinicians, places
and children, so the file is identifiable and belongs on disk, not on a host.
"""

import argparse
import datetime as dt
import html
import pathlib
import sqlite3

DB_DEFAULT = "archive/archive.db"
OUT_DEFAULT = "archive/sample-threads.html"

CSS = """
:root{--paper:#f6f7f9;--surface:#fff;--ink:#171b24;--muted:#5d6675;--faint:#858d9c;
--rule:#dde1e8;--rule-soft:#e9ecf1;--accent:#1f6f6a;--accent-dim:#d3e4e2;
--amber:#a86a15;--amber-dim:#f0e3cd;}
@media (prefers-color-scheme:dark){:root{--paper:#12151b;--surface:#191d25;--ink:#e6e9ef;
--muted:#99a2b2;--faint:#7b8494;--rule:#2b313c;--rule-soft:#232833;--accent:#5fb8b0;
--accent-dim:#1d3a38;--amber:#d3a05a;--amber-dim:#3a2f1c;}}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;
font:16px/1.6 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased}
.page{max-width:880px;margin:0 auto;padding:48px 28px 80px;display:flex;
flex-direction:column;gap:34px}
h1{font-family:"IBM Plex Serif",Georgia,serif;font-weight:600;font-size:31px;margin:0;
letter-spacing:-.015em}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:11.5px;
letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}
.lede{color:var(--muted);max-width:64ch;margin:10px 0 0}
.warn{border-left:2px solid var(--amber);padding:6px 0 6px 16px;color:var(--muted);
font-size:14.5px;max-width:64ch;margin-top:14px}
.thread{background:var(--surface);border:1px solid var(--rule);border-radius:5px;
padding:20px 22px;display:flex;flex-direction:column;gap:11px}
.thead{display:flex;justify-content:space-between;align-items:baseline;gap:14px;flex-wrap:wrap}
.tag{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.08em;
text-transform:uppercase;padding:2px 8px;border-radius:3px;background:var(--accent-dim);
color:var(--accent)}
.tag.large{background:var(--amber-dim);color:var(--amber)}
.tmeta,.cwhen{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--faint)}
.pmeta{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;font-size:12.5px}
.who{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--accent)}
.ptext{font-size:16.5px;line-height:1.62}
.comments{display:flex;flex-direction:column;gap:0;margin-top:4px;
border-top:1px solid var(--rule-soft);padding-top:10px}
.cmt{padding:8px 0;border-bottom:1px solid var(--rule-soft)}
.cmt.d1{margin-left:20px;border-left:2px solid var(--rule);padding-left:14px}
.cmt.d2{margin-left:40px;border-left:2px solid var(--rule);padding-left:14px}
.cmt.d3{margin-left:60px;border-left:2px solid var(--rule);padding-left:14px}
.chead{display:flex;gap:12px;align-items:baseline;margin-bottom:2px}
.ctext{font-size:14.5px;line-height:1.58}
a{color:var(--accent)}
"""


def when(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"


def esc(text):
    return html.escape(text or "").replace("\n", "<br>")


def pick(db, per_bucket):
    """A spread across thread sizes, newest first within each band."""
    n_sub = "(SELECT count(*) FROM comments c WHERE c.post_id = p.id)"
    bands = [("large", f"{n_sub} >= 60"),
             ("medium", f"{n_sub} BETWEEN 15 AND 59"),
             ("small", f"{n_sub} BETWEEN 3 AND 14")]
    chosen = []
    for label, cond in bands:
        rows = db.execute(f"""
            SELECT p.id, p.url, p.created_at, p.text, p.author_id,
                   p.reaction_count, p.comment_count, {n_sub} AS n
            FROM posts p
            WHERE p.text IS NOT NULL AND p.text <> '' AND {cond}
            ORDER BY p.created_at DESC LIMIT ?""", (per_bucket,)).fetchall()
        chosen += [(label, r) for r in rows]
    return chosen


def render_thread(db, label, post):
    rows = db.execute("""SELECT id, parent_id, author_id, created_at, text
                         FROM comments WHERE post_id = ? ORDER BY created_at""",
                      (post["id"],)).fetchall()
    children = {}
    for c in rows:
        children.setdefault(c["parent_id"], []).append(c)

    def branch(parent, depth=0):
        out = []
        for c in children.get(parent, []):
            out.append(
                f'<div class="cmt d{min(depth, 3)}">'
                f'<div class="chead"><span class="who">{esc(c["author_id"])}</span>'
                f'<span class="cwhen">{when(c["created_at"])}</span></div>'
                f'<div class="ctext">{esc(c["text"])}</div></div>')
            out.append(branch(c["id"], depth + 1))
        return "".join(out)

    reactions = post["reaction_count"] if post["reaction_count"] is not None else "&mdash;"
    return f"""
<article class="thread">
  <div class="thead">
    <span class="tag {label}">{label} thread</span>
    <span class="tmeta">{post['n']} captured &middot; {post['comment_count'] or 0} reported
      &middot; {reactions} reactions</span>
  </div>
  <div class="pmeta"><span class="who">{esc(post['author_id'])}</span>
    <span class="cwhen">{when(post['created_at'])}</span>
    <a href="{html.escape(post['url'] or '#')}">permalink</a></div>
  <div class="ptext">{esc(post['text'])}</div>
  <div class="comments">{branch(None)}</div>
</article>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--threads", type=int, default=4,
                    help="threads per size band: large, medium, small (default 4)")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    chosen = pick(db, args.threads)
    if not chosen:
        raise SystemExit("no threads with comments found -- has the comments pass run?")
    body = "".join(render_thread(db, label, post) for label, post in chosen)
    total = sum(p["n"] for _, p in chosen)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""<!doctype html><meta charset="utf-8">
<title>Archive sample &mdash; real threads</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="page">
<header>
  <div class="eyebrow">Real archive content &middot; keep local</div>
  <h1>Archive sample &mdash; {len(chosen)} threads</h1>
  <p class="lede">{len(chosen)} real posts with all {total} of their captured comments,
    drawn across the size range. Authors are stable pseudonyms; @-mentions are
    rewritten to the same handles, so you can follow who answered whom.</p>
  <p class="warn"><strong>Real content from a private support group.</strong>
    Names are gone from author fields and mentions, but free text can still name
    schools, clinicians, places and children. Treat this file as identifiable.</p>
</header>
{body}
</div>""", encoding="utf-8")
    print(f"wrote {out} — {len(chosen)} threads, {total} comments, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
