"""Local storage: raw GraphQL capture (gzipped NDJSON) + normalized SQLite index."""

import gzip
import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    group_id      TEXT,
    author_id     TEXT,
    author_name   TEXT,
    created_at    INTEGER,
    url           TEXT,
    text          TEXT,
    reaction_count   INTEGER,
    comment_count    INTEGER,
    share_count      INTEGER,
    attachments   TEXT,
    raw_ref       INTEGER,
    first_seen    INTEGER,
    last_seen     INTEGER
);
CREATE INDEX IF NOT EXISTS posts_created ON posts(created_at);

CREATE TABLE IF NOT EXISTS comments (
    id            TEXT PRIMARY KEY,
    post_id       TEXT,
    parent_id     TEXT,
    author_id     TEXT,
    author_name   TEXT,
    created_at    INTEGER,
    text          TEXT,
    attachments   TEXT,
    raw_ref       INTEGER,
    first_seen    INTEGER
);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);

CREATE TABLE IF NOT EXISTS media (
    sha256        TEXT PRIMARY KEY,
    post_id       TEXT,
    kind          TEXT,
    src_url       TEXT,
    path          TEXT,
    bytes         INTEGER,
    downloaded_at INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS media_post ON media(post_id);

-- Queue of CDN URLs awaiting download. Signed URLs expire within hours, so the
-- crawler drains this continuously rather than at the end of the run.
CREATE TABLE IF NOT EXISTS media_queue (
    src_url    TEXT PRIMARY KEY,
    post_id    TEXT,
    kind       TEXT,
    queued_at  INTEGER,
    attempts   INTEGER DEFAULT 0,
    done       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS media_queue_pending ON media_queue(done, attempts);

CREATE TABLE IF NOT EXISTS raw (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER,
    url      TEXT,
    friendly TEXT,
    offset   INTEGER
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, outdir: Path):
        self.dir = Path(outdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.dir / "media"
        self.media_dir.mkdir(exist_ok=True)
        self.raw_path = self.dir / "raw.ndjson.gz"
        self.db = sqlite3.connect(self.dir / "archive.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._raw_lines = self._count_raw_lines()

    def _count_raw_lines(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(offset), -1) FROM raw").fetchone()
        return row[0] + 1

    # ---- raw capture -------------------------------------------------

    def append_raw(self, url: str, friendly: str, payload: dict) -> int:
        """Persist one GraphQL payload verbatim. Returns its raw_ref."""
        offset = self._raw_lines
        with gzip.open(self.raw_path, "at", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._raw_lines += 1
        cur = self.db.execute(
            "INSERT INTO raw (ts, url, friendly, offset) VALUES (?,?,?,?)",
            (int(time.time()), url, friendly, offset),
        )
        return cur.lastrowid

    def iter_raw(self):
        """Replay every captured payload, for re-parsing without re-crawling."""
        if not self.raw_path.exists():
            return
        with gzip.open(self.raw_path, "rt", encoding="utf-8") as fh:
            for offset, line in enumerate(fh):
                line = line.strip()
                if line:
                    yield offset, json.loads(line)

    # ---- posts & comments --------------------------------------------

    def _rewrite_identity(self, table: str, row: dict) -> None:
        """Rewrite author fields outright whenever this parse resolved an author.

        COALESCE is wrong here: turning pseudonymization on sets author_name to
        NULL, and COALESCE would keep the stale real name forever. Reparse is
        the documented way to change how identities are stored, so it has to be
        able to clear them -- and to restore them when it is turned back off.
        """
        if row.get("author_id") is None and row.get("author_name") is None:
            return
        self.db.execute(
            f"UPDATE {table} SET author_id=?, author_name=? WHERE id=?",
            (row.get("author_id"), row.get("author_name"), row["id"]),
        )

    def upsert_post(self, p: dict) -> bool:
        """Insert or refresh a post. Returns True if this id was not seen before."""
        now = int(time.time())
        existing = self.db.execute("SELECT id FROM posts WHERE id=?", (p["id"],)).fetchone()
        if existing:
            # Counts drift upward as a post accrues engagement; keep the latest,
            # but never overwrite text we already have with a null re-read.
            atts = json.dumps(p.get("attachments") or [], ensure_ascii=False)
            self.db.execute(
                """UPDATE posts SET
                     reaction_count=COALESCE(?, reaction_count),
                     comment_count =COALESCE(?, comment_count),
                     share_count   =COALESCE(?, share_count),
                     text          =COALESCE(NULLIF(?, ''), text),
                     created_at    =COALESCE(?, created_at),
                     url           =COALESCE(?, url),
                     attachments   =CASE WHEN ? = '[]' THEN attachments ELSE ? END,
                     last_seen=?
                   WHERE id=?""",
                (p.get("reaction_count"), p.get("comment_count"), p.get("share_count"),
                 p.get("text"), p.get("created_at"), p.get("url"), atts, atts,
                 now, p["id"]),
            )
            self._rewrite_identity("posts", p)
            return False
        self.db.execute(
            """INSERT INTO posts
               (id, group_id, author_id, author_name, created_at, url, text,
                reaction_count, comment_count, share_count, attachments, raw_ref,
                first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["id"], p.get("group_id"), p.get("author_id"), p.get("author_name"),
             p.get("created_at"), p.get("url"), p.get("text"), p.get("reaction_count"),
             p.get("comment_count"), p.get("share_count"),
             json.dumps(p.get("attachments") or [], ensure_ascii=False),
             p.get("raw_ref"), now, now),
        )
        return True

    def upsert_comment(self, c: dict) -> bool:
        now = int(time.time())
        try:
            self.db.execute(
                """INSERT INTO comments
                   (id, post_id, parent_id, author_id, author_name, created_at, text,
                    attachments, raw_ref, first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (c["id"], c.get("post_id"), c.get("parent_id"), c.get("author_id"),
                 c.get("author_name"), c.get("created_at"), c.get("text"),
                 json.dumps(c.get("attachments") or [], ensure_ascii=False),
                 c.get("raw_ref"), now),
            )
            return True
        except sqlite3.IntegrityError:
            atts = json.dumps(c.get("attachments") or [], ensure_ascii=False)
            self.db.execute(
                """UPDATE comments SET
                     text       =COALESCE(NULLIF(?, ''), text),
                     post_id    =COALESCE(?, post_id),
                     parent_id  =COALESCE(?, parent_id),
                     created_at =COALESCE(?, created_at),
                     attachments=CASE WHEN ? = '[]' THEN attachments ELSE ? END
                   WHERE id=?""",
                (c.get("text"), c.get("post_id"), c.get("parent_id"),
                 c.get("created_at"), atts, atts, c["id"]),
            )
            self._rewrite_identity("comments", c)
            return False

    # ---- media -------------------------------------------------------

    def enqueue_media(self, src_url: str, post_id: str, kind: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO media_queue (src_url, post_id, kind, queued_at) VALUES (?,?,?,?)",
            (src_url, post_id, kind, int(time.time())),
        )

    def pending_media(self, limit: int = 40):
        return self.db.execute(
            "SELECT src_url, post_id, kind FROM media_queue WHERE done=0 AND attempts < 3 "
            "ORDER BY queued_at LIMIT ?",
            (limit,),
        ).fetchall()

    def save_media(self, src_url: str, post_id: str, kind: str, body: bytes, ext: str) -> str:
        digest = hashlib.sha256(body).hexdigest()
        rel = Path(digest[:2]) / digest[2:4] / f"{digest}{ext}"
        dest = self.media_dir / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
        self.db.execute(
            """INSERT OR REPLACE INTO media
               (sha256, post_id, kind, src_url, path, bytes, downloaded_at, error)
               VALUES (?,?,?,?,?,?,?,NULL)""",
            (digest, post_id, kind, src_url, str(rel), len(body), int(time.time())),
        )
        self.db.execute("UPDATE media_queue SET done=1 WHERE src_url=?", (src_url,))
        return digest

    def fail_media(self, src_url: str, err: str) -> None:
        self.db.execute(
            "UPDATE media_queue SET attempts = attempts + 1 WHERE src_url=?", (src_url,)
        )
        row = self.db.execute(
            "SELECT attempts FROM media_queue WHERE src_url=?", (src_url,)
        ).fetchone()
        if row and row["attempts"] >= 3:
            self.db.execute(
                "INSERT OR REPLACE INTO media (sha256, src_url, error, downloaded_at) VALUES (?,?,?,?)",
                (f"failed:{hashlib.sha256(src_url.encode()).hexdigest()[:16]}", src_url,
                 err, int(time.time())),
            )

    # ---- misc --------------------------------------------------------

    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, str(value))
        )

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "posts": q("SELECT COUNT(*) FROM posts"),
            "comments": q("SELECT COUNT(*) FROM comments"),
            "media": q("SELECT COUNT(*) FROM media WHERE error IS NULL"),
            "media_pending": q("SELECT COUNT(*) FROM media_queue WHERE done=0"),
            "raw": q("SELECT COUNT(*) FROM raw"),
        }

    def known_post_ids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT id FROM posts")}

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
