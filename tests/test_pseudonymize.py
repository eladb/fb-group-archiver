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
