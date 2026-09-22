"""GraphQL response capture and the small parsing helpers around it.

These run without Playwright installed -- the response objects are stubs shaped
like the ones Playwright hands to a `page.on("response")` handler.
"""

import re
import pytest

import scrape
from scrape import GROUP_ID_RE, Capture, ext_for


class FakeRequest:
    def __init__(self, post_data=""):
        self.post_data = post_data


class FakeResponse:
    def __init__(self, url, body="{}", post_data="", raises=False):
        self.url = url
        self._body = body
        self._raises = raises
        self.request = FakeRequest(post_data)

    def text(self):
        if self._raises:
            raise RuntimeError("body already discarded")
        return self._body


def capture_with(*responses):
    cap = Capture()
    for r in responses:
        cap._on_response(r)
    return cap


class TestCapture:
    def test_ignores_non_graphql_traffic(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/ajax/bootloader", '{"a":1}'),
            FakeResponse("https://scontent.xx.fbcdn.net/photo.jpg", "binary"),
        )
        assert list(cap.drain()) == []

    def test_captures_graphql_payload(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/api/graphql/", '{"data":{"x":1}}')
        )
        drained = list(cap.drain())
        assert len(drained) == 1
        url, friendly, payload = drained[0]
        assert payload == {"data": {"x": 1}}
        assert "graphql" in url

    def test_splits_streamed_defer_chunks(self):
        """One response can carry several newline-delimited JSON documents."""
        body = '{"data":{"n":1}}\n{"data":{"n":2}}\n{"data":{"n":3}}'
        cap = capture_with(FakeResponse("https://www.facebook.com/api/graphql/", body))
        assert [p["data"]["n"] for _, _, p in cap.drain()] == [1, 2, 3]

    def test_strips_anti_json_hijack_prefix(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/api/graphql/", 'for (;;);{"data":{"n":1}}')
        )
        assert [p for _, _, p in cap.drain()] == [{"data": {"n": 1}}]

    def test_skips_unparseable_lines_but_keeps_the_rest(self):
        body = '{"data":{"n":1}}\n<!DOCTYPE html>\n{"data":{"n":2}}'
        cap = capture_with(FakeResponse("https://www.facebook.com/api/graphql/", body))
        assert [p["data"]["n"] for _, _, p in cap.drain()] == [1, 2]

    def test_blank_lines_ignored(self):
        body = '\n\n{"data":{"n":1}}\n\n'
        cap = capture_with(FakeResponse("https://www.facebook.com/api/graphql/", body))
        assert len(list(cap.drain())) == 1

    def test_unreadable_body_is_skipped_not_fatal(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/api/graphql/", raises=True),
            FakeResponse("https://www.facebook.com/api/graphql/", '{"data":{"n":9}}'),
        )
        assert [p["data"]["n"] for _, _, p in cap.drain()] == [9]

    def test_friendly_name_pulled_from_request(self):
        cap = capture_with(
            FakeResponse(
                "https://www.facebook.com/api/graphql/",
                '{"data":{}}',
                post_data="av=100&fb_api_req_friendly_name=GroupsCometFeedRegularStoriesPaginationQuery&doc_id=123",
            )
        )
        _, friendly, _ = next(iter(cap.drain()))
        assert friendly == "GroupsCometFeedRegularStoriesPaginationQuery"

    def test_friendly_name_absent_is_empty(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/api/graphql/", '{"data":{}}', post_data="av=1")
        )
        _, friendly, _ = next(iter(cap.drain()))
        assert friendly == ""

    def test_drain_empties_the_buffer(self):
        cap = capture_with(
            FakeResponse("https://www.facebook.com/api/graphql/", '{"data":{"n":1}}')
        )
        assert len(list(cap.drain())) == 1
        assert list(cap.drain()) == []


class TestGroupIdRegex:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.facebook.com/groups/1234567890/", "1234567890"),
            ("https://www.facebook.com/groups/my.group.slug/", "my.group.slug"),
            ("https://www.facebook.com/groups/123?ref=share", "123"),
            ("https://www.facebook.com/groups/123/posts/456/", "123"),
            ("https://m.facebook.com/groups/789/", "789"),
        ],
    )
    def test_extracts_id(self, url, expected):
        assert GROUP_ID_RE.search(url).group(1) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.facebook.com/share/g/1c44FosMra/",  # unresolved share link
            "https://www.facebook.com/login/",
        ],
    )
    def test_no_match_on_unresolved_urls(self, url):
        assert GROUP_ID_RE.search(url) is None


class TestExtForFilename:
    @pytest.mark.parametrize(
        "url,ctype,expected",
        [
            ("https://scontent.xx.fbcdn.net/v/t39/a.jpg?oh=1", "image/jpeg", ".jpg"),
            ("https://scontent.xx.fbcdn.net/v/t39/a.png", "image/png", ".png"),
            ("https://scontent.xx.fbcdn.net/v/t39/a.jpeg", "image/jpeg", ".jpeg"),
            ("https://video.xx.fbcdn.net/v/t42/clip.mp4", "video/mp4", ".mp4"),
            ("https://scontent.xx.fbcdn.net/v/t39/noext", "image/webp", ".webp"),
            ("https://video.xx.fbcdn.net/v/t42/noext", "video/mp4", ".mp4"),
            ("https://scontent.xx.fbcdn.net/v/t39/noext", "", ".jpg"),
        ],
    )
    def test_extension_choice(self, url, ctype, expected):
        assert ext_for(url, ctype) == expected


class TestExpandPattern:
    """The comment-expander regex decides whether the second pass sees replies."""

    @pytest.mark.parametrize(
        "label",
        [
            "View more comments",
            "View 24 more comments",
            "See more comments",
            "View all 31 comments",
            "3 replies",
            "1 reply",
            "View previous comments",
        ],
    )
    def test_matches_english_expanders(self, label):
        import re
        assert re.search(scrape.DEFAULT_EXPAND, label, re.I)

    @pytest.mark.parametrize("label", ["Like", "Share", "Write a comment", "Comment"])
    def test_does_not_match_ordinary_buttons(self, label):
        import re
        assert not re.search(scrape.DEFAULT_EXPAND, label, re.I)


class TestCommentSortPatterns:
    """The 'All comments' switch is what makes small posts capture at all."""

    def test_sort_button_matches_english_labels(self):
        pat = re.compile(scrape.DEFAULT_SORT_BUTTON, re.I)
        for label in ("Most relevant", "Newest﻿", "All comments", "Top comments"):
            assert pat.search(label), label

    def test_sort_choice_matches_all_comments_menu_item(self):
        pat = re.compile(scrape.DEFAULT_SORT_CHOICE, re.I)
        item = "All comments\nShow all comments, including potential spam."
        assert pat.search(item)

    def test_sort_choice_does_not_match_the_narrower_orderings(self):
        pat = re.compile(scrape.DEFAULT_SORT_CHOICE, re.I)
        assert not pat.search("Newest\nShow all comments with the newest comments first.")
        assert not pat.search("Most relevant\nShow friends' comments and the most engaging comments first.")


class TestInGroupFilter:
    """Permalink pages carry Facebook's recommendations; those are not group content."""

    def test_group_permalink_is_in_group(self):
        p = {"url": "https://www.facebook.com/groups/mygroup/posts/123/"}
        assert scrape.in_group(p, "mygroup")

    def test_reel_is_not_in_group(self):
        p = {"url": "https://www.facebook.com/reel/1630791632167210/"}
        assert not scrape.in_group(p, "mygroup")

    def test_other_group_is_not_in_group(self):
        p = {"url": "https://www.facebook.com/groups/someothergroup/posts/9/"}
        assert not scrape.in_group(p, "mygroup")

    def test_missing_url_is_not_in_group(self):
        assert not scrape.in_group({}, "mygroup")
        assert not scrape.in_group({"url": None}, "mygroup")

    def test_no_group_id_keeps_everything(self):
        # Without a group to compare against, filtering would silently drop data.
        assert scrape.in_group({"url": "https://www.facebook.com/reel/1/"}, None)
