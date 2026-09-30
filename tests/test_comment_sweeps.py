"""The comment queue must terminate.

`have < comment_count` alone does not: Facebook's comment_count includes replies,
hidden and deleted comments that cannot be fetched, so a fully-swept post stays
short of its own count for ever. On 2026-09-28 a run walked 2,430 posts and
collected nothing while that residue sat at the head of the queue.
"""
import importlib.util
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("store", _root / "store.py")
store_mod = importlib.util.module_from_spec(_spec)
sys.modules["store"] = store_mod
_spec.loader.exec_module(store_mod)

MAX = 3  # keep in step with scrape.MAX_SWEEP_ATTEMPTS


def _queue(st):
    """The eligibility half of cmd_comments' query, without the ordering."""
    return [r["id"] for r in st.db.execute(
        f"""SELECT p.id FROM posts p
            LEFT JOIN comment_sweeps s ON s.post_id = p.id
            WHERE p.comment_count > 0
              AND p.url IS NOT NULL AND p.url LIKE '%/groups/%'
              AND (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) < p.comment_count
              AND (s.attempts IS NULL OR s.attempts < {MAX} OR s.last_gain > 0)""")]


def _post(st, pid, count):
    st.db.execute(
        "INSERT INTO posts (id, url, comment_count, created_at) VALUES (?,?,?,?)",
        (pid, f"https://www.facebook.com/groups/1/posts/{pid}/", count, 0))
    st.commit()


def test_unfetchable_post_leaves_the_queue(tmp_path):
    st = store_mod.Store(tmp_path)
    _post(st, "p1", 5)              # claims 5 comments, none obtainable
    assert _queue(st) == ["p1"]
    for attempt in range(MAX):
        assert _queue(st) == ["p1"], f"should still be eligible at attempt {attempt}"
        st.record_comment_sweep("p1", 0, 0)
        st.commit()
    assert _queue(st) == [], "a post that yields nothing must stop blocking the queue"


def test_a_productive_post_stays_eligible_forever(tmp_path):
    # Retirement must never apply to a post that is still giving up comments,
    # however many times it has been opened.
    st = store_mod.Store(tmp_path)
    _post(st, "p2", 100)
    for i in range(MAX + 4):
        st.record_comment_sweep("p2", i + 1, 1)   # one new comment each time
        st.commit()
        assert _queue(st) == ["p2"], f"still productive at attempt {i}"


def test_retry_retired_requeues(tmp_path):
    st = store_mod.Store(tmp_path)
    _post(st, "p3", 5)
    for _ in range(MAX):
        st.record_comment_sweep("p3", 0, 0)
    st.commit()
    assert _queue(st) == []
    assert st.reset_comment_sweeps() == 1
    assert _queue(st) == ["p3"], "--retry-retired must put it back"


def test_a_complete_post_never_enters_the_queue(tmp_path):
    st = store_mod.Store(tmp_path)
    _post(st, "p4", 2)
    for cid in ("c1", "c2"):
        st.db.execute(
            "INSERT INTO comments (id, post_id, text) VALUES (?,?,?)", (cid, "p4", "x"))
    st.commit()
    assert _queue(st) == []
