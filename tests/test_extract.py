"""The normalizer is the part most likely to drift when Facebook reshapes its
payloads, so these tests pin the structural recognition rather than exact paths."""

import pytest

from conftest import CDN_IMAGE, CDN_VIDEO, make_comment_node, make_feed_payload, make_post_node
from extract import (
    dig,
    harvest,
    is_comment_node,
    is_post_node,
    media_urls,
    normalize_comment,
    normalize_post,
    typename_census,
    walk,
)


class TestHelpers:
    def test_walk_yields_nested_dicts(self):
        obj = {"a": {"b": 1}, "c": [{"d": 2}, [{"e": 3}]]}
        found = list(walk(obj))
        assert {"b": 1} in found
        assert {"d": 2} in found
        assert {"e": 3} in found

    def test_walk_stops_at_max_depth(self):
        # Build a chain deeper than MAX_DEPTH and confirm it terminates.
        node = {"leaf": True}
        for _ in range(80):
            node = {"next": node}
        assert list(walk(node))  # does not recurse forever

    def test_dig_traverses_keys_and_indices(self):
        obj = {"a": [{"b": "hit"}]}
        assert dig(obj, "a", 0, "b") == "hit"

    @pytest.mark.parametrize("path", [("a", 5, "b"), ("a", 0, "missing"), ("nope",)])
    def test_dig_returns_default_on_miss(self, path):
        assert dig({"a": [{"b": "hit"}]}, *path, default="fallback") == "fallback"

    def test_dig_treats_none_as_miss(self):
        assert dig({"a": None}, "a", default="fallback") == "fallback"


class TestNodeRecognition:
    def test_story_with_post_id_is_a_post(self, post_node):
        assert is_post_node(post_node)

    def test_story_without_post_id_but_with_creation_time_is_a_post(self):
        assert is_post_node({"__typename": "Story", "creation_time": 1716200000})

    def test_post_id_without_typename_still_recognized(self):
        assert is_post_node({"post_id": "1", "message": {"text": "x"}})

    @pytest.mark.parametrize(
        "node",
        [
            {"__typename": "Story"},                       # no id, no timestamp
            {"__typename": "User", "id": "1", "name": "x"},
            {"post_id": "1"},                              # bare id, no content
            "not a dict",
            None,
        ],
    )
    def test_non_posts_rejected(self, node):
        assert not is_post_node(node)

    def test_comment_recognized(self):
        assert is_comment_node(make_comment_node())

    def test_post_is_not_mistaken_for_comment(self, post_node):
        assert not is_comment_node(post_node)


class TestNormalizePost:
    def test_extracts_core_fields(self, post_node):
        p = normalize_post(post_node, group_id="999", raw_ref=7)
        assert p["id"] == "1234567890"
        assert p["group_id"] == "999"
        assert p["author_id"] == "100001"
        assert p["author_name"] == "Dana Cohen"
        assert p["created_at"] == 1716200000
        assert p["text"].startswith("Anyone have a recommendation")
        assert p["raw_ref"] == 7

    def test_counts_unwrapped_from_feedback(self, post_node):
        p = normalize_post(post_node)
        assert (p["reaction_count"], p["comment_count"], p["share_count"]) == (12, 3, 1)

    def test_url_query_string_stripped(self, post_node):
        assert normalize_post(post_node)["url"].endswith("/posts/1234567890/")

    def test_url_synthesized_when_absent(self):
        node = make_post_node()
        del node["wwwURL"]
        p = normalize_post(node, group_id="999")
        assert p["url"] == "https://www.facebook.com/groups/999/posts/1234567890/"

    def test_text_found_via_comet_sections_layout(self):
        """Newer feeds nest the message under comet_sections instead of top level."""
        node = {
            "__typename": "Story",
            "post_id": "555",
            "creation_time": 1716200000,
            "comet_sections": {
                "content": {"story": {"message": {"text": "nested body", "ranges": []}}}
            },
        }
        assert normalize_post(node)["text"] == "nested body"

    def test_actor_found_when_not_in_actors_array(self):
        node = {
            "__typename": "Story",
            "post_id": "556",
            "creation_time": 1,
            "comet_sections": {
                "context_layout": {
                    "actor": {"__typename": "Page", "id": "77", "name": "The Page"}
                }
            },
        }
        p = normalize_post(node)
        assert (p["author_id"], p["author_name"]) == ("77", "The Page")

    def test_missing_fields_degrade_to_none_not_crash(self):
        p = normalize_post({"__typename": "Story", "post_id": "1", "creation_time": 1})
        assert p["id"] == "1"
        assert p["author_name"] is None
        assert p["reaction_count"] is None
        assert p["attachments"] == []

    def test_node_without_usable_id_is_dropped(self):
        assert normalize_post({"__typename": "Story", "creation_time": 1}) is None
        assert normalize_post({"post_id": 12345, "message": {}}) is None  # non-str id


class TestMediaUrls:
    def test_cdn_image_collected(self, post_node):
        assert media_urls(post_node) == {CDN_IMAGE: "image"}

    def test_video_urls_labelled(self):
        node = {"attachments": [{"media": {"playable_url": CDN_VIDEO}}]}
        assert media_urls(node) == {CDN_VIDEO: "video"}

    def test_non_cdn_urls_ignored(self):
        node = {"uri": "https://example.com/tracking.gif", "src": "https://evil.test/a.jpg"}
        assert media_urls(node) == {}

    def test_urls_under_unrelated_keys_ignored(self):
        # Only known media-bearing keys count, so profile/permalink URLs don't leak in.
        assert media_urls({"some_other_key": CDN_IMAGE}) == {}

    def test_attachments_flow_into_normalized_post(self, post_node):
        atts = normalize_post(post_node)["attachments"]
        assert atts == [{"url": CDN_IMAGE, "kind": "image"}]


class TestNormalizeComment:
    def test_extracts_fields(self):
        c = normalize_comment(make_comment_node(), post_id="1234567890", raw_ref=3)
        assert c["id"] == "Y29tbWVudDox"
        assert c["post_id"] == "1234567890"
        assert c["author_name"] == "Yossi Levi"
        assert c["created_at"] == 1716200100
        assert c["text"] == "I used Moshe last month, he was great"

    def test_post_id_recovered_from_associated_story(self):
        node = make_comment_node(post_id="999888")
        assert normalize_comment(node)["post_id"] == "999888"

    def test_reply_records_parent(self):
        node = make_comment_node()
        node["parent_comment"] = {"id": "parent-1"}
        assert normalize_comment(node)["parent_id"] == "parent-1"

    def test_non_string_body_becomes_none(self):
        node = make_comment_node()
        node["body"] = {"text": None}
        assert normalize_comment(node)["text"] is None


class TestHarvest:
    def test_finds_posts_in_feed_envelope(self, feed_payload):
        posts, comments = harvest(feed_payload, group_id="999")
        assert {p["id"] for p in posts} == {"1234567890", "222"}
        assert comments == []

    def test_finds_comments_alongside_posts(self):
        post = make_post_node()
        post["feedback"]["comment_rendering_instance"] = {
            "comments": {"edges": [{"node": make_comment_node(post_id="1234567890")}]}
        }
        posts, comments = harvest(make_feed_payload(post))
        assert len(posts) == 1
        assert len(comments) == 1
        assert comments[0]["post_id"] == "1234567890"

    def test_duplicate_nodes_deduped_within_payload(self):
        node = make_post_node()
        payload = make_feed_payload(node, node)
        posts, _ = harvest(payload)
        assert len(posts) == 1

    def test_empty_payload_is_safe(self):
        assert harvest({"data": {"node": None}}) == ([], [])

    def test_error_payload_is_safe(self):
        payload = {"errors": [{"message": "rate limited"}], "data": None}
        assert harvest(payload) == ([], [])


def test_typename_census_counts_occurrences(feed_payload):
    census = typename_census(feed_payload)
    assert census["Story"] == 2
    assert census["User"] == 2
    assert census["Photo"] == 2
