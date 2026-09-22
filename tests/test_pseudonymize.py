"""Pseudonymization: stable opaque handles instead of named authors."""

import pytest

import extract
from conftest import make_comment_node, make_post_node


@pytest.fixture
def pseudonymous(monkeypatch, tmp_path):
    monkeypatch.setattr(extract, "PSEUDONYMIZE", True)
    monkeypatch.setattr(extract, "SALT_FILE", tmp_path / "salt")
    monkeypatch.setattr(extract, "_salt_cache", None)
    return extract


class TestPseudonymize:
    def test_post_author_name_dropped_and_id_opaque(self, pseudonymous):
        p = extract.normalize_post(make_post_node())
        assert p["author_name"] is None
        assert p["author_id"].startswith("anon:")
        assert "100001" not in p["author_id"]

    def test_comment_author_also_pseudonymized(self, pseudonymous):
        c = extract.normalize_comment(make_comment_node())
        assert c["author_name"] is None
        assert c["author_id"].startswith("anon:")

    def test_same_author_gets_same_handle(self, pseudonymous):
        a = extract.normalize_post(make_post_node(post_id="1", author=("555", "Ann")))
        b = extract.normalize_post(make_post_node(post_id="2", author=("555", "Ann")))
        assert a["author_id"] == b["author_id"]

    def test_different_authors_differ(self, pseudonymous):
        a = extract.normalize_post(make_post_node(post_id="1", author=("555", "Ann")))
        b = extract.normalize_post(make_post_node(post_id="2", author=("666", "Bo")))
        assert a["author_id"] != b["author_id"]

    def test_salt_is_persisted_so_handles_survive_reparse(self, pseudonymous, tmp_path):
        first = extract.normalize_post(make_post_node())["author_id"]
        extract._salt_cache = None          # simulate a fresh process
        assert extract.normalize_post(make_post_node())["author_id"] == first

    def test_distinct_salts_give_distinct_handles(self, monkeypatch, tmp_path):
        monkeypatch.setattr(extract, "PSEUDONYMIZE", True)
        monkeypatch.setattr(extract, "SALT_FILE", tmp_path / "salt_a")
        monkeypatch.setattr(extract, "_salt_cache", None)
        a = extract.normalize_post(make_post_node())["author_id"]
        monkeypatch.setattr(extract, "SALT_FILE", tmp_path / "salt_b")
        monkeypatch.setattr(extract, "_salt_cache", None)
        assert extract.normalize_post(make_post_node())["author_id"] != a

    def test_unidentified_author_stays_none(self, pseudonymous):
        p = extract.normalize_post({"__typename": "Story", "post_id": "1", "creation_time": 1})
        assert p["author_id"] is None and p["author_name"] is None

    def test_content_is_untouched(self, pseudonymous):
        node = make_post_node()
        p = extract.normalize_post(node)
        assert p["text"] == node["message"]["text"]
        assert p["reaction_count"] == 12


class TestMentionRedaction:
    """@-mentions render as plain text in the body, carrying real names."""

    def _comment(self, text, ranges):
        return {"__typename": "Comment", "id": "c1", "created_time": 1,
                "body": {"text": text, "ranges": ranges},
                "author": {"__typename": "User", "id": "7", "name": "Someone"}}

    def test_mention_span_is_replaced_with_a_handle(self, pseudonymous):
        node = self._comment("Dana Cohen thank you so much", [
            {"offset": 0, "length": 10, "entity": {"__typename": "User", "id": "555"}}])
        out = extract.normalize_comment(node)["text"]
        assert "Dana Cohen" not in out
        assert out.startswith("anon:")
        assert out.endswith(" thank you so much")

    def test_handle_matches_that_persons_author_handle(self, pseudonymous):
        mentioned, _ = extract.pseudonymize("555", "Dana Cohen")
        node = self._comment("Dana Cohen hi", [
            {"offset": 0, "length": 10, "entity": {"id": "555"}}])
        assert extract.normalize_comment(node)["text"].startswith(mentioned)

    def test_multiple_mentions_all_replaced(self, pseudonymous):
        node = self._comment("Ann Lee and Bo Ray both helped", [
            {"offset": 0, "length": 7, "entity": {"id": "1"}},
            {"offset": 12, "length": 6, "entity": {"id": "2"}}])
        out = extract.normalize_comment(node)["text"]
        assert "Ann Lee" not in out and "Bo Ray" not in out
        assert "both helped" in out

    def test_body_without_ranges_is_untouched(self, pseudonymous):
        node = self._comment("no mentions here", [])
        assert extract.normalize_comment(node)["text"] == "no mentions here"

    def test_out_of_bounds_range_is_ignored_not_crashed(self, pseudonymous):
        node = self._comment("short", [{"offset": 0, "length": 999, "entity": {"id": "1"}}])
        assert extract.normalize_comment(node)["text"] == "short"

    def test_disabled_pseudonymization_leaves_names_intact(self, monkeypatch):
        monkeypatch.setattr(extract, "PSEUDONYMIZE", False)
        node = self._comment("Dana Cohen thanks", [
            {"offset": 0, "length": 10, "entity": {"id": "555"}}])
        assert extract.normalize_comment(node)["text"] == "Dana Cohen thanks"
