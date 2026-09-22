"""Browserbase session and context handling.

Everything here stubs the HTTP seam (`Browserbase._call`), because the real
thing rents a metered browser. What's worth pinning is the part that costs
something when it goes wrong: the context id -- the persisted login -- must be
reused rather than recreated, a session must be released even when the run
fails, and the browser must close *before* the release or the profile is lost.
"""

import json

import pytest

import browserbase
from browserbase import Browserbase, BrowserbaseError

PROJECT = "4af0d8fa-0376-4bc8-bf69-429db7f2eb3a"
CONTEXT = "66554216-d0c0-440a-958b-f0969d0e670f"


class FakeAPI:
    """Records calls and answers them the way the v1 API does."""

    def __init__(self, projects=None, fail=None):
        self.calls = []
        self.projects = projects if projects is not None else [{"id": PROJECT, "name": "Production"}]
        self.fail = fail or {}
        self.released = []

    def __call__(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if path in self.fail:
            raise self.fail[path]
        if path == "/projects":
            return self.projects
        if path.endswith("/usage"):
            return {"browserMinutes": 7, "proxyBytes": 0}
        if path == "/contexts":
            return {"id": CONTEXT}
        if path == "/sessions":
            return {"id": "sess-1", "status": "RUNNING",
                    "connectUrl": "wss://connect.usw2.browserbase.com/?sessionId=sess-1"}
        if path.endswith("/debug"):
            return {"debuggerFullscreenUrl": "https://browserbase.com/devtools-fullscreen/sess-1"}
        if data and data.get("status") == "REQUEST_RELEASE":
            self.released.append(path)
            return {}
        return {}


@pytest.fixture
def bb(tmp_path, monkeypatch):
    monkeypatch.delenv(browserbase.PROJECT_VAR, raising=False)
    monkeypatch.delenv(browserbase.CONTEXT_VAR, raising=False)
    client = Browserbase("bb_test_key", context_file=str(tmp_path / ".browserbase-context"))
    client._call = FakeAPI()
    return client


class TestLoad:
    def test_returns_none_without_a_key(self, monkeypatch):
        monkeypatch.delenv(browserbase.KEY_VAR, raising=False)
        assert browserbase.load() is None

    def test_required_without_a_key_explains_where_to_get_one(self, monkeypatch):
        monkeypatch.delenv(browserbase.KEY_VAR, raising=False)
        with pytest.raises(BrowserbaseError) as exc:
            browserbase.load(required=True)
        assert "browserbase.com/settings" in str(exc.value)

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv(browserbase.KEY_VAR, "bb_live_abc")
        assert browserbase.load().api_key == "bb_live_abc"


class TestProjectResolution:
    def test_single_project_needs_no_configuration(self, bb):
        assert bb.project_id == PROJECT

    def test_resolved_once_then_cached(self, bb):
        assert bb.project_id == bb.project_id
        assert [c[0] for c in bb._call.calls].count("/projects") == 1

    def test_several_projects_refuses_to_guess(self, bb):
        bb._call.projects = [{"id": "a", "name": "One"}, {"id": "b", "name": "Two"}]
        with pytest.raises(BrowserbaseError) as exc:
            bb.project_id
        assert browserbase.PROJECT_VAR in str(exc.value) and "One" in str(exc.value)

    def test_environment_overrides_the_lookup(self, bb, monkeypatch):
        monkeypatch.setenv(browserbase.PROJECT_VAR, "from-env")
        client = Browserbase("bb_test_key")
        client._call = FakeAPI()
        assert client.project_id == "from-env"
        assert client._call.calls == []

    def test_no_projects_at_all_is_an_error(self, bb):
        bb._call.projects = []
        with pytest.raises(BrowserbaseError):
            bb.project_id


class TestContext:
    """The context is the login. Recreating one silently means a logged-out run."""

    def test_created_once_and_remembered_on_disk(self, bb):
        assert bb.load_context() == CONTEXT
        assert bb.context_file.read_text().strip() == CONTEXT

    def test_a_later_run_reuses_the_saved_id(self, bb):
        bb.load_context()
        again = Browserbase("bb_test_key", context_file=str(bb.context_file))
        again._call = FakeAPI()
        assert again.load_context() == CONTEXT
        assert "/contexts" not in [c[0] for c in again._call.calls]

    def test_environment_wins_over_the_file(self, bb, monkeypatch):
        bb.context_file.write_text("from-file\n")
        monkeypatch.setenv(browserbase.CONTEXT_VAR, "from-env")
        assert Browserbase("k", context_file=str(bb.context_file)).load_context() == "from-env"

    def test_blank_file_is_ignored(self, bb):
        bb.context_file.write_text("   \n")
        assert bb.load_context() == CONTEXT


class TestSession:
    def test_start_persists_the_context_and_caps_the_session(self, bb):
        bb.start(timeout=1800)
        path, body, _ = next(c for c in bb._call.calls if c[0] == "/sessions")
        assert body["browserSettings"]["context"] == {"id": CONTEXT, "persist": True}
        assert body["timeout"] == 1800
        assert "proxies" not in body  # absent, not False -- the API reads it as opt-in

    def test_proxy_is_requested_only_when_asked(self, bb):
        bb.start(proxy=True)
        _, body, _ = next(c for c in bb._call.calls if c[0] == "/sessions")
        assert body["proxies"] is True

    def test_proxy_on_a_free_plan_says_what_to_do(self, bb):
        bb._call.fail["/sessions"] = BrowserbaseError("plan does not cover this: Proxies are not included")
        with pytest.raises(BrowserbaseError) as exc:
            bb.start(proxy=True)
        assert "--bb-proxy" in str(exc.value) and "datacenter IP" in str(exc.value)

    def test_live_view_url_for_the_manual_login(self, bb):
        bb.start()
        assert bb.view_url.endswith("sess-1")

    def test_no_view_url_before_a_session_exists(self, bb):
        assert bb.view_url is None

    def test_close_releases_the_session(self, bb):
        bb.start()
        bb.close()
        assert bb._call.released == ["/sessions/sess-1"]
        assert bb.session_id is None

    def test_close_without_a_session_is_harmless(self, bb):
        bb.close()
        assert bb._call.released == []

    def test_browser_is_closed_before_release(self, bb):
        """Order matters: the context is persisted on a clean browser close."""
        order = []

        class Recorder(FakeAPI):
            def __call__(self, path, data=None, method=None):
                if data and data.get("status") == "REQUEST_RELEASE":
                    order.append("release")
                return FakeAPI.__call__(self, path, data, method)

        bb._call = Recorder()
        bb.start()

        class FakeBrowser:
            def close(self_inner):
                order.append("browser.close")

        bb.close(FakeBrowser())
        assert order == ["browser.close", "release"]

    def test_a_browser_that_will_not_close_still_releases(self, bb):
        """Otherwise a crashed run leaves the meter running."""
        bb.start()

        class Stuck:
            def close(self_inner):
                raise RuntimeError("websocket already gone")

        bb.close(Stuck())
        assert bb._call.released == ["/sessions/sess-1"]


class TestDescribe:
    def test_names_the_project_and_usage_without_the_key(self, bb):
        line = bb.describe()
        assert PROJECT[:8] in line and "7 browser-minutes" in line
        assert "bb_test_key" not in line

    def test_survives_an_unreachable_usage_endpoint(self, bb):
        bb._call.fail["/projects/%s/usage" % PROJECT] = BrowserbaseError("down")
        assert PROJECT[:8] in bb.describe()
