#!/usr/bin/env python3
"""Drive a Browserbase session -- a rented cloud Chromium -- instead of local Chrome.

Browserbase (https://browserbase.com) hands out a headful Chromium per session,
reachable over CDP, plus a *context*: a profile blob it restores into the next
session. That context is what makes this usable here at all, because it carries
the Facebook cookies you log in with by hand, once, through the session's live
view.

Two things separate it from a Sidekick box, and both argue for keeping it to
short runs:

- **The IP is a datacenter IP** (us-west-2 on this account) and, without the
  paid proxy add-on, there is nothing to do about it. For a logged-in personal
  account that is the loudest automation signal there is -- see the ban-vector
  note in README.md.
- **It is metered by the browser-minute**, so an hours-long sweep is the wrong
  shape of job for it. A `crawl --max-posts 50` that tells you whether
  extract.py still matches live payloads is the right one.

Nothing here is selected automatically: the scraper uses Browserbase only when
you ask for it with --browserbase.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.browserbase.com/v1"
KEY_VAR = "BROWSERBASE_API_KEY"
PROJECT_VAR = "BROWSERBASE_PROJECT_ID"
CONTEXT_VAR = "BROWSERBASE_CONTEXT_ID"

# Where the context id is remembered between runs. Losing it means losing the
# logged-in profile, so it is written next to the local Chrome profile rather
# than held only in the environment.
CONTEXT_FILE = ".browserbase-context"

# The session cap. Browserbase's own default is 300s on this account, which a
# crawl trips almost immediately; the crawl is resumable, so the cost of hitting
# even this longer one is a re-run, not lost work.
DEFAULT_TIMEOUT = 3600


class BrowserbaseError(RuntimeError):
    """Bad credentials, a plan that does not cover the request, or a dead session."""


def load(api_key: str = None, required: bool = False):
    """Build a Browserbase client from the argument or the environment.

    Returns None when no key is set and none was required, so callers can fall
    back to another browser without special-casing.
    """
    key = api_key if api_key is not None else os.environ.get(KEY_VAR, "")
    if not (key or "").strip():
        if required:
            raise BrowserbaseError(
                f"{KEY_VAR} is not set. Create a key at "
                "https://browserbase.com/settings and export it."
            )
        return None
    return Browserbase(key.strip())


class Browserbase:
    label = "browserbase"

    def __init__(self, api_key: str, project_id: str = None, context_file: str = CONTEXT_FILE):
        self.api_key = api_key
        self.context_file = Path(context_file)
        self._project = project_id or os.environ.get(PROJECT_VAR) or None
        self.session = None
        self.context_id = None

    # ------------------------------------------------------------- transport

    def _call(self, path: str, data=None, method: str = None):
        req = urllib.request.Request(
            API + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"X-BB-API-Key": self.api_key, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("message", "")
            except Exception:
                pass
            if exc.code in (401, 403):
                raise BrowserbaseError(f"{KEY_VAR} rejected: {detail or exc.reason}") from exc
            if exc.code == 402:
                raise BrowserbaseError(f"plan does not cover this: {detail or exc.reason}") from exc
            raise BrowserbaseError(f"browserbase HTTP {exc.code}: {detail or exc.reason}") from exc
        except BrowserbaseError:
            raise
        except Exception as exc:
            raise BrowserbaseError(f"cannot reach browserbase: {exc}") from exc

    # --------------------------------------------------------------- account

    @property
    def project_id(self) -> str:
        """The project to bill sessions to, resolved once and cached.

        With one project there is nothing to choose; with several, refusing to
        guess is better than silently spending minutes on the wrong one.
        """
        if self._project:
            return self._project
        projects = self._call("/projects")
        if not isinstance(projects, list) or not projects:
            raise BrowserbaseError("this key has no projects")
        if len(projects) > 1:
            names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in projects)
            raise BrowserbaseError(f"several projects -- set {PROJECT_VAR} to one of: {names}")
        self._project = projects[0]["id"]
        return self._project

    def usage(self) -> dict:
        return self._call(f"/projects/{self.project_id}/usage")

    def describe(self) -> str:
        """The line that goes into logs. No credential in it."""
        used = ""
        try:
            used = f", {self.usage().get('browserMinutes', '?')} browser-minutes used"
        except BrowserbaseError:
            pass
        return f"browserbase project {self.project_id[:8]}{used}"

    # --------------------------------------------------------------- context

    def load_context(self) -> str:
        """The persisted profile id: env, then file, then a fresh one.

        A new context starts logged out, so a lost id costs a re-login, not an
        archive -- but it is worth not losing.
        """
        if self.context_id:
            return self.context_id
        from_env = (os.environ.get(CONTEXT_VAR) or "").strip()
        if from_env:
            self.context_id = from_env
            return self.context_id
        if self.context_file.exists():
            saved = self.context_file.read_text(encoding="utf-8").strip()
            if saved:
                self.context_id = saved
                return self.context_id
        self.context_id = self._call("/contexts", {"projectId": self.project_id})["id"]
        self.context_file.write_text(self.context_id + "\n", encoding="utf-8")
        return self.context_id

    # --------------------------------------------------------------- session

    def start(self, timeout: int = DEFAULT_TIMEOUT, proxy: bool = False,
              viewport=(1280, 900)) -> dict:
        body = {
            "projectId": self.project_id,
            "browserSettings": {
                "context": {"id": self.load_context(), "persist": True},
                "viewport": {"width": viewport[0], "height": viewport[1]},
            },
            "timeout": timeout,
        }
        if proxy:
            body["proxies"] = True
        try:
            self.session = self._call("/sessions", body)
        except BrowserbaseError as exc:
            if proxy and "plan does not cover" in str(exc):
                raise BrowserbaseError(
                    "proxies are not on this plan, so the session would run from a "
                    "datacenter IP anyway -- drop --bb-proxy, or upgrade at "
                    "https://browserbase.com/plans"
                ) from exc
            raise
        return self.session

    @property
    def session_id(self):
        return (self.session or {}).get("id")

    @property
    def view_url(self):
        """Live view: the browser in a tab, for the one manual login."""
        if not self.session_id:
            return None
        try:
            debug = self._call(f"/sessions/{self.session_id}/debug")
        except BrowserbaseError:
            return None
        return debug.get("debuggerFullscreenUrl") or debug.get("debuggerUrl")

    # --------------------------------------------------------------- browser

    def connect(self, pw):
        """Attach Playwright to the rented browser over CDP.

        Starts a session if one is not already running. Returns (browser,
        context); the context is the restored profile, cookies and all.
        """
        if not self.session:
            self.start()
        browser = pw.chromium.connect_over_cdp(self.session["connectUrl"])
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, ctx

    def close(self, browser=None) -> None:
        """Close the browser, then release the session so the meter stops.

        Order matters: the context is persisted when the browser closes
        cleanly, so releasing first would throw away the login.
        """
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if not self.session_id:
            return
        try:
            self._call(f"/sessions/{self.session_id}",
                       {"projectId": self.project_id, "status": "REQUEST_RELEASE"},
                       method="POST")
        except BrowserbaseError:
            pass  # it will time out on its own; nothing useful to do here
        self.session = None


def main() -> int:
    """`python browserbase.py` -- check the key, the project and the plan."""
    try:
        bb = load(required=True)
        print(bb.describe())
        print(f"context: {bb.load_context()}")
    except BrowserbaseError as exc:
        print(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
