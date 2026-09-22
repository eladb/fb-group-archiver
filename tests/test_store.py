"""Storage guarantees that matter for a multi-day, resumable crawl:
idempotent upserts, durable raw capture, and a media queue that survives restarts."""

import gzip
import json

import pytest

from conftest import make_post_node
from extract import normalize_post
from store import Store


@pytest.fixture
def post():
    return normalize_post(make_post_node(), group_id="999", raw_ref=1)


class TestPosts:
    def test_first_insert_reports_new(self, store, post):
        assert store.upsert_post(post) is True

    def test_reinsert_reports_not_new(self, store, post):
        store.upsert_post(post)
        assert store.upsert_post(post) is False

    def test_fields_round_trip(self, store, post):
        store.upsert_post(post)
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["author_name"] == "Dana Cohen"
        assert row["created_at"] == 1716200000
        assert row["reaction_count"] == 12
        assert json.loads(row["attachments"])[0]["kind"] == "image"

    def test_engagement_counts_refresh_on_resight(self, store, post):
        store.upsert_post(post)
        store.upsert_post({**post, "reaction_count": 40, "comment_count": 9})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert (row["reaction_count"], row["comment_count"]) == (40, 9)

    def test_partial_resight_does_not_erase_known_text(self, store, post):
        """A truncated re-read must never blank out a body we already captured."""
        store.upsert_post(post)
        store.upsert_post({**post, "text": None, "reaction_count": None})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["text"] == post["text"]
        assert row["reaction_count"] == 12

    def test_empty_string_text_does_not_erase_known_text(self, store, post):
        store.upsert_post(post)
        store.upsert_post({**post, "text": ""})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["text"] == post["text"]

    def test_known_post_ids_drives_resume(self, store, post):
        store.upsert_post(post)
        store.upsert_post({**post, "id": "other"})
        assert store.known_post_ids() == {post["id"], "other"}


class TestComments:
    def _comment(self, **over):
        base = {
            "id": "c1", "post_id": "p1", "parent_id": None, "author_id": "2",
            "author_name": "Yossi Levi", "created_at": 10, "text": "hello",
            "attachments": [], "raw_ref": 1,
        }
        base.update(over)
        return base

    def test_insert_and_dedupe(self, store):
        assert store.upsert_comment(self._comment()) is True
        assert store.upsert_comment(self._comment()) is False
        assert store.counts()["comments"] == 1

    def test_reinsert_can_backfill_post_association(self, store):
        store.upsert_comment(self._comment(post_id=None))
        store.upsert_comment(self._comment(post_id="p1"))
        row = store.db.execute("SELECT post_id FROM comments WHERE id='c1'").fetchone()
        assert row["post_id"] == "p1"

    def test_reinsert_does_not_blank_text(self, store):
        store.upsert_comment(self._comment())
        store.upsert_comment(self._comment(text=None))
        row = store.db.execute("SELECT text FROM comments WHERE id='c1'").fetchone()
        assert row["text"] == "hello"


class TestMediaQueue:
    URL = "https://scontent-tlv3-1.xx.fbcdn.net/v/t39/a.jpg"

    def test_enqueue_then_pending(self, store):
        store.enqueue_media(self.URL, "p1", "image")
        pending = store.pending_media()
        assert [r["src_url"] for r in pending] == [self.URL]

    def test_enqueue_is_idempotent(self, store):
        store.enqueue_media(self.URL, "p1", "image")
        store.enqueue_media(self.URL, "p1", "image")
        assert len(store.pending_media()) == 1

    def test_save_writes_file_and_clears_queue(self, store):
        store.enqueue_media(self.URL, "p1", "image")
        digest = store.save_media(self.URL, "p1", "image", b"binary-bytes", ".jpg")
        dest = store.media_dir / digest[:2] / digest[2:4] / f"{digest}.jpg"
        assert dest.read_bytes() == b"binary-bytes"
        assert store.pending_media() == []
        assert store.counts()["media"] == 1

    def test_identical_bytes_stored_once(self, store):
        a = store.save_media(self.URL, "p1", "image", b"same", ".jpg")
        b = store.save_media(self.URL + "?v=2", "p2", "image", b"same", ".jpg")
        assert a == b
        assert len(list(store.media_dir.rglob("*.jpg"))) == 1

    def test_failures_retry_then_give_up(self, store):
        store.enqueue_media(self.URL, "p1", "image")
        for _ in range(3):
            assert len(store.pending_media()) == 1
            store.fail_media(self.URL, "http 403")
        # Expired signed URLs shouldn't be retried forever.
        assert store.pending_media() == []

    def test_failure_recorded_with_reason(self, store):
        store.enqueue_media(self.URL, "p1", "image")
        for _ in range(3):
            store.fail_media(self.URL, "http 403")
        row = store.db.execute(
            "SELECT error FROM media WHERE src_url=?", (self.URL,)
        ).fetchone()
        assert row["error"] == "http 403"
        assert store.counts()["media"] == 0  # errored rows excluded from the count


class TestRawCapture:
    def test_append_returns_incrementing_refs(self, store):
        a = store.append_raw("u1", "FeedQuery", {"n": 1})
        b = store.append_raw("u2", "FeedQuery", {"n": 2})
        assert b > a

    def test_payloads_round_trip_verbatim(self, store):
        payload = {"data": {"unicode": "שלום", "nested": [1, 2, {"x": None}]}}
        store.append_raw("u", "F", payload)
        assert [p for _, p in store.iter_raw()] == [payload]

    def test_written_as_gzipped_ndjson(self, store):
        store.append_raw("u", "F", {"a": 1})
        store.append_raw("u", "F", {"b": 2})
        with gzip.open(store.raw_path, "rt", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l]
        assert [json.loads(l) for l in lines] == [{"a": 1}, {"b": 2}]

    def test_offsets_continue_across_reopen(self, tmp_path):
        """A resumed run must not overwrite offsets from the previous session."""
        first = Store(tmp_path / "a")
        first.append_raw("u", "F", {"n": 1})
        first.close()

        second = Store(tmp_path / "a")
        second.append_raw("u", "F", {"n": 2})
        offsets = [off for off, _ in second.iter_raw()]
        payloads = [p for _, p in second.iter_raw()]
        second.close()

        assert offsets == [0, 1]
        assert payloads == [{"n": 1}, {"n": 2}]

    def test_iter_raw_empty_before_any_capture(self, store):
        assert list(store.iter_raw()) == []


class TestState:
    def test_get_returns_default_when_unset(self, store):
        assert store.get_state("group_id") is None
        assert store.get_state("group_id", "fallback") == "fallback"

    def test_set_then_get(self, store):
        store.set_state("group_id", "123456")
        assert store.get_state("group_id") == "123456"

    def test_set_overwrites(self, store):
        store.set_state("group_id", "a")
        store.set_state("group_id", "b")
        assert store.get_state("group_id") == "b"

    def test_group_id_survives_reopen(self, tmp_path):
        s = Store(tmp_path / "a")
        s.set_state("group_id", "123456")
        s.commit()
        s.close()
        s2 = Store(tmp_path / "a")
        assert s2.get_state("group_id") == "123456"
        s2.close()


def test_counts_reports_every_table(store, post):
    store.upsert_post(post)
    store.append_raw("u", "F", {})
    store.enqueue_media("https://scontent.xx.fbcdn.net/x.jpg", post["id"], "image")
    counts = store.counts()
    assert counts["posts"] == 1
    assert counts["raw"] == 1
    assert counts["media_pending"] == 1


class TestReparseBackfill:
    """Reparse must be able to correct fields on rows that already exist.

    The tool's contract is "fix extract.py and reparse, never re-crawl", which
    only holds if the upsert path actually rewrites existing rows.
    """

    def test_parent_id_backfilled_on_existing_comment(self, store):
        store.upsert_comment({"id": "c1", "post_id": "p1", "text": "hi"})
        store.upsert_comment({"id": "c1", "post_id": "p1", "text": "hi", "parent_id": "c0"})
        row = store.db.execute("SELECT * FROM comments WHERE id='c1'").fetchone()
        assert row["parent_id"] == "c0"

    def test_pseudonymization_clears_existing_real_name(self, store, post):
        store.upsert_post(post)
        assert store.db.execute("SELECT author_name FROM posts WHERE id=?",
                                (post["id"],)).fetchone()[0] == "Dana Cohen"
        store.upsert_post({**post, "author_id": "anon:abc", "author_name": None})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["author_name"] is None
        assert row["author_id"] == "anon:abc"

    def test_identity_restored_when_pseudonymization_disabled(self, store, post):
        store.upsert_post({**post, "author_id": "anon:abc", "author_name": None})
        store.upsert_post(post)
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert (row["author_id"], row["author_name"]) == ("100001", "Dana Cohen")

    def test_unresolved_author_does_not_wipe_stored_identity(self, store, post):
        store.upsert_post(post)
        store.upsert_post({**post, "author_id": None, "author_name": None})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["author_name"] == "Dana Cohen"

    def test_null_text_still_never_blanks_stored_text(self, store, post):
        store.upsert_post(post)
        store.upsert_post({**post, "text": None})
        row = store.db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
        assert row["text"] == post["text"]

    def test_empty_attachments_do_not_clobber_stored_ones(self, store, post):
        store.upsert_post(post)
        before = store.db.execute("SELECT attachments FROM posts WHERE id=?",
                                  (post["id"],)).fetchone()[0]
        store.upsert_post({**post, "attachments": []})
        after = store.db.execute("SELECT attachments FROM posts WHERE id=?",
                                 (post["id"],)).fetchone()[0]
        assert after == before
