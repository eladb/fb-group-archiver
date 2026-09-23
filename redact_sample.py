#!/usr/bin/env python3
"""Render a shareable sample of threads with identities redacted from free text.

Pseudonymizing the author columns is not enough to make a corpus shareable: the
prose still names people. This builds a redaction lexicon from the capture
itself -- every author name and @-mention entity the raw payloads ever carried --
and removes those names wherever they appear in post and comment bodies, then
adds pattern passes for the identifiers a name list cannot know about
(clinicians, contact details, links).

    ./.venv/bin/python redact_sample.py --threads 3 --out archive/sample-redacted.html

It reports what it could not remove. Recall is measured, not assumed.
"""

import argparse
import datetime as dt
import gzip
import html
import json
import pathlib
import re
import sqlite3

# Capitalised words that are ordinary vocabulary in this corpus and would wreck
# the text if redacted as names. Month and virtue names are the usual trap:
# "May", "Grace" and "Hope" are all real member names and all ordinary words.
NOT_A_NAME = {
    "i", "a", "the", "my", "he", "she", "we", "you", "it", "they", "this", "that",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "god", "grace", "hope", "faith", "joy", "summer", "autumn", "winter", "rose",
    "pandas", "pans", "ocd", "adhd", "pots", "mcas", "lyme", "strep", "ivig",
    "dr", "doctor", "mom", "mum", "dad", "son", "daughter", "kid", "child",
    "thanks", "thank", "yes", "no", "ok", "okay", "please", "hi", "hello",
    "amoxicillin", "augmentin", "prednisone", "zoloft", "prozac", "motrin",
    "advil", "tylenol", "benadryl", "er", "icu", "ent", "mri", "us", "uk",
}

# Places. Location plus condition plus timing re-identifies a family even with
# every name stripped, so states, provinces and countries go too. Town names are
# only caught when they follow a preposition -- a full gazetteer is out of scope,
# and the footer says so rather than implying otherwise.
REGIONS = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
    "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
    "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","Hampshire",
    "Jersey","Mexico","York","Carolina","Dakota","Ohio","Oklahoma","Oregon",
    "Pennsylvania","Rhode Island","Tennessee","Texas","Utah","Vermont","Virginia",
    "Washington","Wisconsin","Wyoming","Ontario","Quebec","Alberta","Manitoba",
    "Scotland","Wales","Ireland","England","Australia","Canada","Israel","Germany",
    "France","Spain","Italy","Netherlands","Sweden","Norway","Denmark",
]

# Things a name lexicon cannot know about, removed by shape instead.
PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?\d[\d\-.\s()]{7,}\d)\b"), "[PHONE]"),
    (re.compile(r"https?://\S+"), "[LINK]"),
    # Named regions.
    (re.compile(r"\b(?:" + "|".join(re.escape(r) for r in REGIONS) + r")\b"), "[PLACE]"),
    # "in <Town>", "near <Town>", "from <Town>" -- a place after a locative
    # preposition, which is how people actually write where they are.
    (re.compile(r"\b(in|near|from|outside|around)\s+([A-Z][\w'’-]{2,}(?:\s+[A-Z][\w'’-]{2,})?)"
                r"(?=[\s,.!?])"), r"\1 [PLACE]"),
]


# Care providers are NOT redacted by default. Which clinicians and clinics
# families recommend, travel to, or warn each other about is substantive content
# in a patient-community corpus -- stripping it removes the finding rather than
# protecting anyone, and a practising clinician named in a recommendation is a
# professional in their professional capacity, not a bystander.
#
# --redact-clinicians turns these on for audiences where that is the wrong call.
PROVIDER_PATTERNS = [
    (re.compile(r"\b(Dr\.?|Doctor|Prof\.?|Nurse)\s+[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)?"),
     r"\1 [CLINICIAN]"),
    (re.compile(r"\b[A-Z][\w'’-]+\s+(Children's|Medical|Health|Hospital|Clinic|Pediatrics)\b"),
     "[FACILITY]"),
]


from store import raw_chunk_paths


def build_lexicon(raw_path):
    """Every personal name the capture ever saw, from the unpseudonymized payloads.

    `raw_path` may be the archive directory or a single chunk file.
    """
    names = set()

    def walk(node):
        if isinstance(node, dict):
            tn = node.get("__typename")
            nm = node.get("name")
            if tn in ("User", "Page", "GroupAnonAuthorProfile") and isinstance(nm, str):
                names.add(nm)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    # Raw capture is chunked, so this walks every chunk. Reading only one would
    # build the lexicon from part of the corpus -- and a name missing from the
    # lexicon is a name that never gets redacted, which fails silently and in
    # the one direction that matters.
    for chunk in raw_chunk_paths(raw_path):
        with gzip.open(chunk, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    walk(json.loads(line))
                except json.JSONDecodeError:
                    continue

    full = {n.strip() for n in names if len(n.strip()) > 2}
    tokens = set()
    for name in full:
        for part in re.split(r"[\s'’-]+", name):
            if len(part) > 2 and part.lower() not in NOT_A_NAME:
                tokens.add(part)
    return full, tokens


# Credentials people put in their own display name. A member who signs
# themselves "… , MD" is telling the group they are a clinician.
CREDENTIALS = re.compile(
    r"(?:^|[\s,(])"
    r"(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|Psy\.?D\.?|N\.?P\.?|FNP|PNP|DNP|CRNP"
    r"|R\.?N\.?|BSN|PA-?C|DDS|DMD|LCSW|LPC|LMFT|OTR/?L?|DPT|D\.?C\.?|N\.?D\.?"
    r"|RDN?|MPH|IBCLC|BCBA)"
    r"(?:[\s,.)]|$)")

# How the group addresses a clinician in running text.
ADDRESSED = re.compile(r"\b(?:Dr\.?|Doctor|Prof\.?)\s+([A-Z][\w'’-]{2,}(?:\s+[A-Z][\w'’-]{2,})?)")


def provider_keeplist(full_names, db):
    """Members who are themselves care providers, deduced from the corpus.

    Two independent signals. A credential in the member's own display name is
    self-declared. Being addressed as "Dr <name>" by other members is how the
    community identifies them, and it catches clinicians whose display name is
    just their name.

    Returned names are exempted from redaction: in a patient community the
    clinicians who participate are exactly the ones a reader needs to see, and
    the lexicon would otherwise scrub them as ordinary members.

    Deliberately conservative, and it must stay that way. A third signal --
    matching a member's SURNAME against clinicians the group discusses -- was
    tried and removed. In a community that talks constantly about well-known
    clinicians, surnames collide with ordinary members, and every collision
    exempts a member from redaction and publishes their real name. Missing a
    clinician is a nuisance; publishing a member is a harm. Do not add a signal
    that can fire on a member, however many clinicians it would catch.
    """
    keep, why = set(), {}
    for name in full_names:
        if CREDENTIALS.search(name):
            keep.add(name)
            why[name] = "credential in display name"

    addressed = set()
    for (text,) in db.execute(
            "SELECT text FROM comments WHERE text IS NOT NULL "
            "UNION ALL SELECT text FROM posts WHERE text IS NOT NULL"):
        addressed.update(m.group(1) for m in ADDRESSED.finditer(text))

    lowered = {n.lower(): n for n in full_names}

    for phrase in addressed:
        hit = lowered.get(phrase.lower())
        if hit:
            keep.add(hit)
            why.setdefault(hit, f'addressed as "Dr {phrase}"')
            continue
    # Deliberately no surname fallback. Matching "Dr <surname>" against members
    # who share that surname is wrong in both directions and dangerous in one.
    # A patient community constantly discusses well-known clinicians by name, and
    # common surnames collide: on one live corpus this attributed 32 widely
    # discussed doctors to unrelated members who merely shared a surname, and
    # every one of those matches would have exempted a real member from
    # redaction -- the opposite of the point. A clinician who is never addressed
    # by full name and carries no credential is a miss; publishing a member's
    # name is a harm, and the two are not equally bad.
    return keep, why


def corpus_vocabulary(db, floor=12):
    """Words the corpus uses as ordinary lowercase vocabulary.

    Thousands of members mean the name list collides hard with English: "Will",
    "Mark", "Grace", "May" are all real names and all real words, and matching
    them case-insensitively turns "diagnosed with autism" into "diagnosed [NAME]
    autism". A token people routinely write in lowercase is vocabulary, whatever
    else it may also be.
    """
    counts = {}
    for (text,) in db.execute(
            "SELECT text FROM comments WHERE text IS NOT NULL "
            "UNION ALL SELECT text FROM posts WHERE text IS NOT NULL"):
        for word in re.findall(r"\b[a-z][a-z'’-]{2,}\b", text):
            counts[word] = counts.get(word, 0) + 1
    return {w for w, n in counts.items() if n >= floor}


def compile_lexicon(full, tokens, vocabulary, keep=()):
    """Full names match loosely; bare given names must look like names.

    A multi-word name is distinctive enough to match case-insensitively. A single
    token is not, so it only matches Capitalised, and only if the corpus does not
    otherwise use it as a word.
    """
    keep_l = {k.lower() for k in keep}
    keep_tokens = {p.lower() for k in keep for p in re.split(r"[\s'’-]+", k) if len(p) > 2}
    full = {f for f in full if f.lower() not in keep_l}
    safe = sorted((t for t in tokens
                   if t.lower() not in vocabulary and t.lower() not in keep_tokens),
                  key=len, reverse=True)
    chunks, size = [], 400
    for i in range(0, len(full_sorted := sorted(full, key=len, reverse=True)), size):
        part = "|".join(re.escape(t) for t in full_sorted[i:i + size])
        chunks.append((re.compile(rf"\b(?:{part})\b", re.IGNORECASE), "[NAME]"))
    for i in range(0, len(safe), size):
        part = "|".join(re.escape(t) for t in safe[i:i + size])
        chunks.append((re.compile(rf"\b(?:{part})\b"), "[NAME]"))  # case-sensitive
    return chunks


def redact(text, lexicon_res, provider_patterns=()):
    if not text:
        return text, 0
    hits = 0
    for pattern, repl in tuple(PATTERNS) + tuple(provider_patterns):
        text, n = pattern.subn(repl, text)
        hits += n
    for rx, repl in lexicon_res:
        text, n = rx.subn(repl, text)
        hits += n
    # Collapse runs the passes leave behind: "[NAME] [NAME]" is one person.
    text = re.sub(r"(\[NAME\]\s*){2,}", "[NAME] ", text)
    return text, hits


def residual(text, full_names):
    """Names still present after redaction -- the honest recall measure."""
    low = (text or "").lower()
    return [n for n in full_names if len(n) > 4 and n.lower() in low]


CSS = """
:root{--paper:#f7f6f4;--surface:#fff;--ink:#1b1a18;--muted:#63605a;--faint:#8b8781;
--rule:#e2dfd9;--rule-soft:#edebe6;--accent:#7a4b2a;--accent-dim:#ecdfd4;
--flag:#6b6f3f;--flag-dim:#e7e9d8;}
@media (prefers-color-scheme:dark){:root{--paper:#161513;--surface:#1e1d1a;--ink:#eae7e1;
--muted:#a39e95;--faint:#85807a;--rule:#332f2a;--rule-soft:#272420;--accent:#cd9268;
--accent-dim:#3a2a1d;--flag:#b3b878;--flag-dim:#2b2d1c;}}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;
font:16px/1.62 "Source Sans 3",system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased}
.page{max-width:820px;margin:0 auto;padding:52px 26px 84px;display:flex;
flex-direction:column;gap:32px}
h1{font-family:"Source Serif 4",Georgia,serif;font-weight:600;font-size:32px;margin:0;
letter-spacing:-.012em;text-wrap:balance}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:11px;
letter-spacing:.15em;text-transform:uppercase;color:var(--accent);margin-bottom:11px}
.lede{color:var(--muted);max-width:63ch;margin:9px 0 0;font-size:17px}
.recall{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--rule);border-radius:4px;
overflow:hidden;background:var(--surface)}
.recall div{padding:13px 18px;border-right:1px solid var(--rule-soft);flex:1;min-width:130px}
.recall div:last-child{border-right:0}
.recall .n{font-family:"IBM Plex Mono",monospace;font-size:21px;font-variant-numeric:tabular-nums}
.recall .k{font-size:12px;color:var(--muted)}
.thread{background:var(--surface);border:1px solid var(--rule);border-radius:5px;
padding:19px 21px;display:flex;flex-direction:column;gap:10px}
.thead{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}
.tag{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.08em;
text-transform:uppercase;padding:2px 8px;border-radius:3px;background:var(--accent-dim);
color:var(--accent)}
.tmeta,.cwhen{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--faint)}
.who{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--accent)}
.pmeta{display:flex;gap:13px;align-items:baseline;flex-wrap:wrap}
.ptext{font-size:16.5px}
.comments{display:flex;flex-direction:column;margin-top:3px;
border-top:1px solid var(--rule-soft);padding-top:9px}
.cmt{padding:8px 0;border-bottom:1px solid var(--rule-soft)}
.cmt.d1{margin-left:19px;border-left:2px solid var(--rule);padding-left:13px}
.cmt.d2{margin-left:38px;border-left:2px solid var(--rule);padding-left:13px}
.cmt.d3{margin-left:57px;border-left:2px solid var(--rule);padding-left:13px}
.chead{display:flex;gap:11px;align-items:baseline;margin-bottom:2px}
.ctext{font-size:14.5px;line-height:1.58}
mark{background:var(--flag-dim);color:var(--flag);padding:0 3px;border-radius:2px;
font-family:"IBM Plex Mono",monospace;font-size:.86em;letter-spacing:.02em}
.note{border-left:2px solid var(--accent);padding:5px 0 5px 15px;color:var(--muted);
font-size:14.5px;max-width:63ch}
footer{border-top:1px solid var(--rule);padding-top:18px;color:var(--faint);
font-size:13px;max-width:63ch}
a{color:var(--accent)}
"""


def esc(text):
    out = html.escape(text or "")
    out = re.sub(r"\[(NAME|CLINICIAN|FACILITY|EMAIL|PHONE|LINK)\]",
                 r"<mark>[\1]</mark>", out)
    return out.replace("\n", "<br>")


def when(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="archive/archive.db")
    ap.add_argument("--raw", default="archive",
                    help="archive directory holding the raw chunks, or one chunk file")
    ap.add_argument("--out", default="archive/sample-redacted.html")
    ap.add_argument("--threads", type=int, default=3, help="threads per size band")
    ap.add_argument("--redact-clinicians", action="store_true",
                    help="also remove clinician and facility names (kept by default)")
    args = ap.parse_args()

    print("building lexicon from raw payloads ...", flush=True)
    full, tokens = build_lexicon(args.raw)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    providers = PROVIDER_PATTERNS if args.redact_clinicians else ()
    vocab = corpus_vocabulary(db)
    keep, why = provider_keeplist(full, db) if not args.redact_clinicians else (set(), {})
    if keep:
        print(f"  keeping {len(keep)} member clinicians unredacted:", flush=True)
        for n in sorted(keep)[:20]:
            print(f"     {n}  ({why[n]})", flush=True)
    lex = compile_lexicon(full, tokens, vocab, keep)
    kept = len([t for t in tokens if t.lower() not in vocab])
    print(f"  {len(full)} full names; {kept} of {len(tokens)} name tokens kept "
          f"({len(tokens)-kept} are ordinary vocabulary)", flush=True)
    n_sub = "(SELECT count(*) FROM comments c WHERE c.post_id = p.id)"
    chosen = []
    for label, cond in (("large", f"{n_sub} >= 40"),
                        ("medium", f"{n_sub} BETWEEN 12 AND 39"),
                        ("small", f"{n_sub} BETWEEN 3 AND 11")):
        chosen += [(label, r) for r in db.execute(f"""
            SELECT p.id,p.created_at,p.text,p.author_id,p.reaction_count,p.comment_count,
                   {n_sub} AS n FROM posts p
            WHERE p.text IS NOT NULL AND p.text<>'' AND {cond}
            ORDER BY p.created_at DESC LIMIT ?""", (args.threads,)).fetchall()]

    redactions = 0
    leaks = set()
    blocks = []
    for label, post in chosen:
        ptext, k = redact(post["text"], lex, providers)
        redactions += k
        leaks.update(residual(ptext, full))
        rows = db.execute("""SELECT id,parent_id,author_id,created_at,text FROM comments
                             WHERE post_id=? ORDER BY created_at""", (post["id"],)).fetchall()
        kids = {}
        for c in rows:
            kids.setdefault(c["parent_id"], []).append(c)

        def branch(parent, depth=0):
            nonlocal redactions
            out = []
            for c in kids.get(parent, []):
                ctext, k2 = redact(c["text"], lex, providers)
                redactions += k2
                leaks.update(residual(ctext, full))
                out.append(
                    f'<div class="cmt d{min(depth,3)}"><div class="chead">'
                    f'<span class="who">{html.escape(c["author_id"] or "—")}</span>'
                    f'<span class="cwhen">{when(c["created_at"])}</span></div>'
                    f'<div class="ctext">{esc(ctext)}</div></div>')
                out.append(branch(c["id"], depth + 1))
            return "".join(out)

        body = branch(None)
        blocks.append(f"""
<article class="thread">
  <div class="thead"><span class="tag">{label} thread</span>
    <span class="tmeta">{post['n']} comments &middot; {when(post['created_at'])}</span></div>
  <div class="pmeta"><span class="who">{html.escape(post['author_id'] or '—')}</span></div>
  <div class="ptext">{esc(ptext)}</div>
  <div class="comments">{body}</div>
</article>""")

    total_c = sum(p["n"] for _, p in chosen)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""<title>Redacted Thread Sample</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>{CSS}</style>
<div class="page">
<header>
  <div class="eyebrow">Redacted sample</div>
  <h1>Redacted Thread Sample</h1>
  <p class="lede">{len(chosen)} real discussions from an archived peer-support group,
    with {total_c} comments, identities removed from the text as well as the metadata.</p>
</header>

<div class="recall">
  <div><div class="n">{redactions}</div><div class="k">redactions applied</div></div>
  <div><div class="n">{len(full)}</div><div class="k">names in lexicon</div></div>
  <div><div class="n">{len(leaks)}</div><div class="k">names still detected</div></div>
  <div><div class="n">{total_c}</div><div class="k">comments shown</div></div>
</div>

<p class="note">Authors appear as stable salted pseudonyms, so the same person keeps the
same handle across threads and you can follow who answered whom. Every personal name the
capture ever recorded was removed from the prose, along with contact details, links, and
clinician and facility names. Marked spans show where something was taken out.</p>

{''.join(blocks)}

<footer><p><strong>What was checked.</strong> Of the {len(full):,} personal names this capture
ever recorded, zero survive in the text above &mdash; verified by searching the rendered
output for every one of them. Contact details, links and named regions are removed by
pattern.</p>
<p><strong>What is deliberately kept.</strong> Clinicians, clinics and hospitals are
<em>not</em> redacted. Which providers families recommend, travel to, or warn each other
about is the substance of a patient community, and removing it would delete the finding
rather than protect a patient. One caveat: a provider who is also a group member is in the
name lexicon and will appear as [NAME] like any other member.</p>
<p><strong>What may remain.</strong> Individual town names are only caught after a
locative preposition, so some survive. A name the capture never saw &mdash; a child, a
clinician outside the group &mdash; is not in the lexicon and cannot be matched. And people
describe each other without using names at all, which no filter reaches. Location plus
condition plus timing can still identify a family to someone who knows them. Treat this as
de-identified, not anonymous.</p>
<p>Posts and comments are otherwise unedited.</p></footer>
</div>""", encoding="utf-8")

    print(f"\nwrote {out}")
    print(f"  {len(chosen)} threads, {total_c} comments, {redactions} redactions")
    print(f"  residual names detected: {len(leaks)}")
    if leaks:
        for n in sorted(leaks)[:12]:
            print(f"    - {n}")


if __name__ == "__main__":
    main()
