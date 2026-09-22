"""Watching a remote browser for a hand-driven login.

With a remote browser the person logging in is usually not the person who ran
the command, so `login` polls for the cookie instead of blocking on Enter. What
matters: it notices the login promptly, it gives up rather than hanging, and it
re-checks once more after the budget runs out -- a login that lands in the last
second still counts.
"""

import pytest

import scrape


class FakeContext:
    """Hands back cookies; c_user appears after `appears_after` reads."""

    def __init__(self, appears_after=None):
        self.appears_after = appears_after
        self.reads = 0

    def cookies(self, url):
        self.reads += 1
        base = [{"name": "datr", "value": "x"}, {"name": "sb", "value": "y"}]
        if self.appears_after is not None and self.reads > self.appears_after:
            return base + [{"name": "c_user", "value": "100001"}]
        return base


class FakeSession:
    def __init__(self, ctx):
        self.ctx = ctx


@pytest.fixture
def no_sleep(monkeypatch):
    """Run the loop on a clock we control, so the tests stay instant."""
    now = {"t": 1000.0}
    monkeypatch.setattr(scrape.time, "time", lambda: now["t"])
    monkeypatch.setattr(scrape.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))
    return now


class TestWaitForLogin:
    def test_returns_at_once_when_already_logged_in(self, no_sleep):
        ctx = FakeContext(appears_after=0)
        assert scrape.wait_for_login(FakeSession(ctx), seconds=600) is True
        assert ctx.reads == 1  # no polling at all

    def test_notices_a_login_that_lands_mid_wait(self, no_sleep):
        ctx = FakeContext(appears_after=3)
        assert scrape.wait_for_login(FakeSession(ctx), seconds=600, poll=5) is True
        assert ctx.reads == 4

    def test_gives_up_instead_of_hanging(self, no_sleep):
        ctx = FakeContext(appears_after=None)
        assert scrape.wait_for_login(FakeSession(ctx), seconds=30, poll=5) is False

    def test_budget_is_respected(self, no_sleep):
        """Roughly seconds/poll attempts, not an unbounded loop."""
        ctx = FakeContext(appears_after=None)
        scrape.wait_for_login(FakeSession(ctx), seconds=30, poll=5)
        assert 6 <= ctx.reads <= 8

    def test_a_login_in_the_final_moment_still_counts(self, no_sleep):
        """The loop re-checks after the deadline rather than reporting failure."""
        ctx = FakeContext(appears_after=6)
        assert scrape.wait_for_login(FakeSession(ctx), seconds=30, poll=5) is True

    def test_zero_budget_still_checks_once(self, no_sleep):
        ctx = FakeContext(appears_after=0)
        assert scrape.wait_for_login(FakeSession(ctx), seconds=0) is True
