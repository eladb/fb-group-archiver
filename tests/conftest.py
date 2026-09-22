import sys
from pathlib import Path

import pytest

# The scraper modules import each other by bare name and are run from the
# package directory, so put that directory on the path for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402  (needs the path insert above)


@pytest.fixture(autouse=True)
def identities_visible(monkeypatch):
    """Extraction tests assert on real author fields.

    Pseudonymization is a separate, opt-out-able concern covered by its own
    tests, so it is disabled for everything else rather than baked into every
    expected value.
    """
    monkeypatch.setattr(extract, "PSEUDONYMIZE", False)


CDN_IMAGE = "https://scontent-tlv3-1.xx.fbcdn.net/v/t39.30808-6/photo_a.jpg"
CDN_VIDEO = "https://video-tlv3-1.xx.fbcdn.net/v/t42.1790-2/clip_b.mp4"


def make_post_node(
    post_id="1234567890",
    text="Anyone have a recommendation for a plumber in the area?",
    created=1716200000,
    author=("100001", "Dana Cohen"),
    reactions=12,
    comments=3,
    shares=1,
    media=(CDN_IMAGE,),
):
    """A Story node shaped the way the group feed delivers them."""
    return {
        "__typename": "Story",
        "id": "UzpfSTEwMDAwMTox",
        "post_id": post_id,
        "creation_time": created,
        "message": {"text": text, "ranges": []},
        "actors": [{"__typename": "User", "id": author[0], "name": author[1]}],
        "wwwURL": f"https://www.facebook.com/groups/999/posts/{post_id}/?ref=feed",
        "feedback": {
            "reaction_count": {"count": reactions},
            "total_comment_count": comments,
            "share_count": {"count": shares},
        },
        "attachments": [
            {"media": {"__typename": "Photo", "image": {"uri": url}}} for url in media
        ],
    }


def make_comment_node(
    cid="Y29tbWVudDox",
    text="I used Moshe last month, he was great",
    created=1716200100,
    author=("100002", "Yossi Levi"),
    post_id=None,
):
    node = {
        "__typename": "Comment",
        "id": cid,
        "created_time": created,
        "body": {"text": text},
        "author": {"__typename": "User", "id": author[0], "name": author[1]},
    }
    if post_id:
        node["feedback"] = {"associated_story": {"post_id": post_id}}
    return node


def make_feed_payload(*post_nodes):
    """Wrap Story nodes in the edge/connection envelope the feed uses."""
    return {
        "data": {
            "node": {
                "group_feed": {
                    "edges": [{"node": n} for n in post_nodes],
                    "page_info": {"has_next_page": True, "end_cursor": "abc123"},
                }
            }
        }
    }


@pytest.fixture
def post_node():
    return make_post_node()


@pytest.fixture
def feed_payload():
    return make_feed_payload(make_post_node(), make_post_node(post_id="222", text="Second"))


@pytest.fixture
def store(tmp_path):
    from store import Store

    s = Store(tmp_path / "archive")
    yield s
    s.close()
