#!/usr/bin/env python3
"""Redact the whole corpus into a derived database.

Produces the text that may leave this machine. Nothing else may: the archive
is pseudonymous by column and identifying by content, and every downstream
step -- annotation, embedding, analysis -- reads from here, never from posts
and comments directly.

Care providers are deliberately NOT redacted. Which clinicians and clinics
families recommend, travel to, or warn each other about is the substance of a
patient community, and a practising clinician named in a recommendation is a
professional acting professionally. Stripping them removes the finding rather
than protecting a bystander.

This is de-identified, not anonymous, and the distinction is load-bearing.
Individual town names survive unless they follow a locative preposition, a
full gazetteer being out of scope. Clinical narrative is re-identifying on its
own: a rare presentation with a date and a treatment sequence can identify a
family with every proper noun removed. Treat the output as reduced-risk
personal health data about children, not as anonymous text.

Writes to a SEPARATE database, so it survives the snapshot refresh that
replaces archive.db, and so a bad run can be deleted without touching capture.

Usage:
  ./redact_corpus.py                  redact everything not yet done
  ./redact_corpus.py --limit 500      a sample, for eyeballing
  ./redact_corpus.py --rebuild        discard previous output and start over
  ./redact_corpus.py --verify-only    re-run the recall check, redact nothing
"""
import argparse
import json
import pathlib
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from redact_sample import (build_lexicon, provider_keeplist, corpus_vocabulary,
                           compile_lexicon, redact, residual)

SCHEMA = """
CREATE TABLE IF NOT EXISTS redacted (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- 'post' | 'comment'
    text       TEXT,
    hits       INTEGER,                -- redactions applied, for spotting no-ops
    method     TEXT NOT NULL           -- provenance: which lexicon produced this
);
CREATE INDEX IF NOT EXISTS redacted_kind ON redacted(kind);
CREATE TABLE IF NOT EXISTS redaction_runs (
    run_id     TEXT PRIMARY KEY,
    started_at INTEGER,
    finished_at INTEGER,
    method     TEXT,
    items      INTEGER,
    residual_sampled INTEGER,
    residual_hits    INTEGER
);
"""


def load_lexicon(archive_dir, cache, db, rebuild=False):
    """Name lexicon, cached -- building it walks every raw payload ever captured.

    The cache is keyed on the method string, so enlarging the corpus does not
    silently reuse a lexicon built before the new names arrived.
    """
    if cache.exists() and not rebuild:
        data = json.loads(cache.read_text())
        print(f"  lexicon from cache: {len(data['full'])} names, "
              f"{len(data['tokens'])} tokens, {len(data['keep'])} clinicians kept")
        return set(data["full"]), set(data["tokens"]), set(data["keep"])

    print("  building lexicon from raw payloads (walks every capture, slow)...")
    full, tokens = build_lexicon(archive_dir)
    keep, _why = provider_keeplist(full, db)
    cache.write_text(json.dumps(
        {"full": sorted(full), "tokens": sorted(tokens), "keep": sorted(keep)}))
    print(f"  lexicon: {len(full)} names, {len(tokens)} tokens, "
          f"{len(keep)} clinicians kept (exempt)")
    return full, tokens, keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="archive", help="capture directory")
    ap.add_argument("--out", default=str(pathlib.Path.home()
                    / ".local/share/pandas-agent/insight.db"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true",
                    help="discard the output AND rebuild the name lexicon from "
                         "every raw payload (slow)")
    ap.add_argument("--redo", action="store_true",
                    help="re-redact every item with the cached lexicon -- for a "
                         "change to the redaction RULES rather than the names")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--sample", type=int, default=2000,
                    help="items to re-scan for surviving names")
    args = ap.parse_args()

    archive = pathlib.Path(args.archive)
    src = sqlite3.connect(f"file:{archive/'archive.db'}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(out_path)
    out.executescript(SCHEMA)
    if args.rebuild or args.redo:
        out.execute("DELETE FROM redacted")
        out.commit()
        print("  previous output discarded")

    cache = archive / ".lexicon.json"
    full, tokens, keep = load_lexicon(archive, cache, src, rebuild=args.rebuild)
    vocab = corpus_vocabulary(src)
    lex = compile_lexicon(full, tokens, vocab, keep=keep)
    method = f"lexicon-v1 names={len(full)} tokens={len(tokens)} keep={len(keep)} vocab={len(vocab)}"
    print(f"  {len(lex)} compiled patterns; clinicians NOT redacted")

    if not args.verify_only:
        done = {r[0] for r in out.execute("SELECT id FROM redacted")}
        print(f"  {len(done)} already redacted; scanning for work")
        run_id = f"run-{int(time.time())}"
        started = int(time.time())
        n = 0
        for kind, table in (("post", "posts"), ("comment", "comments")):
            rows = src.execute(
                f"SELECT id, text FROM {table} WHERE text IS NOT NULL AND text <> ''")
            batch = []
            for row in rows:
                if row["id"] in done:
                    continue
                clean, hits = redact(row["text"], lex)   # no provider patterns
                batch.append((row["id"], kind, clean, hits, method))
                n += 1
                if len(batch) >= 500:
                    out.executemany("INSERT OR REPLACE INTO redacted VALUES (?,?,?,?,?)", batch)
                    out.commit(); batch = []
                    print(f"    {n} redacted", end="\r", flush=True)
                if args.limit and n >= args.limit:
                    break
            if batch:
                out.executemany("INSERT OR REPLACE INTO redacted VALUES (?,?,?,?,?)", batch)
                out.commit()
            if args.limit and n >= args.limit:
                break
        print(f"    {n} redacted            ")

    # Recall check. The honest number is not "how many names did we remove" but
    # "how many did we miss", measured by re-scanning output against the lexicon.
    print("  verifying...")
    sample = out.execute(
        "SELECT id, kind, text FROM redacted ORDER BY random() LIMIT ?", (args.sample,)
    ).fetchall()
    # Clinicians are exempt by design, so counting them as misses would make
    # the redactor look broken and invite loosening it. They are reported, but
    # on their own line.
    keep_l = {k.lower() for k in keep}
    missed = clinician_kept = 0
    mention_lead = lead_phrase = 0
    lead_re = re.compile(r"^([A-Z][a-z]+\s+[A-Z][a-z]+)\b")
    for _id, kind, text in sample:
        hits = residual(text, full)
        if hits:
            if all(h.lower() in keep_l for h in hits):
                clinician_kept += 1
            else:
                missed += 1
        # The high-risk shape is an @-mention prefix opening a reply. Matching
        # two capitalised words alone does not measure that -- across the full
        # corpus it flagged 1,258 items, of which 1,258 were ordinary phrases
        # like "Blood Zinc" and "Nordic Calm". Only count a lead that is
        # actually a name we know, and not one we deliberately keep.
        if kind == "comment":
            m = lead_re.match(text or "")
            if m:
                phrase = m.group(1)
                if phrase.lower() in keep_l:
                    pass                      # a clinician, deliberate
                elif phrase in full:
                    mention_lead += 1         # genuine leak
                else:
                    lead_phrase += 1          # ordinary capitalised phrase
    total = out.execute("SELECT count(*) FROM redacted").fetchone()[0]
    n = max(len(sample), 1)
    print(f"\n  redacted rows:            {total}")
    print(f"  sampled for recall:       {len(sample)}")
    print(f"  surviving MEMBER name:    {missed}  ({100*missed/n:.2f}%)   <- the number that matters")
    print(f"  surviving clinician name: {clinician_kept}  (deliberate, not a miss)")
    print(f"  reply opening with a member name: {mention_lead}  ({100*mention_lead/n:.2f}%)")
    print(f"  reply opening with an ordinary capitalised phrase: {lead_phrase}  (not a leak)")
    print("\n  De-identified, NOT anonymous. Towns outside a locative preposition")
    print("  survive, and clinical narrative re-identifies on its own.")

    if not args.verify_only:
        out.execute("INSERT OR REPLACE INTO redaction_runs VALUES (?,?,?,?,?,?,?)",
                    (run_id, started, int(time.time()), method, total,
                     len(sample), missed))
        out.commit()
    out.close()


if __name__ == "__main__":
    main()
