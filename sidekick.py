#!/usr/bin/env python3
"""Drive a Sidekick box -- a long-lived remote Chromium -- instead of local Chrome.

Sidekick (https://github.com/eladb/sidekick) parks a real browser and a shell on
a small server behind a Cloudflare tunnel and hands out a single bearer
credential, `SIDEKICK_TOKEN`, that carries every endpoint. For this archiver it
solves the one thing a sandbox cannot do for itself: keep a Chrome profile
logged into Facebook between runs, on one stable IP, in a window you can watch
and click while the crawl drives it.

The token is base64url(JSON) and every service URL inside it embeds the
credential. Treat it like an SSH key: never commit it, never print it. Nothing
here logs it -- `describe()` is the line meant for logs.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

ENV_VAR = "SIDEKICK_TOKEN"

# Claims the scraper actually needs. `watch_url` and the shell endpoints are
# nice to have and are surfaced when present, but their absence is not fatal.
REQUIRED_CLAIMS = ("base_url", "cdp_url")


class SidekickError(RuntimeError):
    """Bad token, or a box that will not answer."""


def decode(raw: str) -> dict:
    """base64url(JSON) -> claims dict. Padding is optional, as the CLI emits it."""
    raw = (raw or "").strip()
    if not raw:
        raise SidekickError(f"{ENV_VAR} is empty")
    try:
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:  # binascii.Error and friends
        raise SidekickError(f"{ENV_VAR} is not valid base64url: {exc}") from exc
    try:
        claims = json.loads(blob)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SidekickError(f"{ENV_VAR} does not decode to JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise SidekickError(f"{ENV_VAR} decodes to {type(claims).__name__}, expected an object")
    missing = [k for k in REQUIRED_CLAIMS if not claims.get(k)]
    if missing:
        raise SidekickError(f"{ENV_VAR} is missing: {', '.join(missing)}")
    return claims


def load(token: str = None, required: bool = False):
    """Build a Sidekick from the argument or the environment.

    Returns None when no token is set and none was required, so callers can fall
    back to the local Chrome profile without special-casing.
    """
    raw = token if token is not None else os.environ.get(ENV_VAR, "")
    if not (raw or "").strip():
        if required:
            raise SidekickError(
                f"{ENV_VAR} is not set. Provision a box with "
                "`scripts/install.sh` from https://github.com/eladb/sidekick "
                "and export the token it prints."
            )
        return None
    return Sidekick(decode(raw))


def split_auth(url: str):
    """Pull `user:pass@` out of a URL into a Basic header.

    Credentials in the netloc are awkward to pass through Playwright and urllib
    alike, and a URL carrying them is one accidental log line away from leaking.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.username:
        return url, None
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    clean = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    secret = f"{parts.username}:{parts.password or ''}".encode()
    return clean, "Basic " + base64.b64encode(secret).decode()


class Sidekick:
    label = "sidekick"

    def __init__(self, claims: dict):
        self.claims = claims

    def __getattr__(self, name):
        if name in ("base_url", "cdp_url", "watch_url", "shell_url", "exec_url",
                    "server_id", "created_at", "platform", "auth_token"):
            return self.claims.get(name)
        raise AttributeError(name)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).netloc

    @property
    def view_url(self):
        """Where a human watches and clicks: noVNC in a browser tab."""
        return self.watch_url

    def describe(self) -> str:
        """The one line safe to log: box, host, age. No credential."""
        sid = (self.server_id or "?")[:8]
        created = self.created_at or "?"
        return f"sidekick {sid} on {self.platform or '?'} at {self.host} (created {created})"

    # ------------------------------------------------------------- transport

    def _request(self, url: str, data: bytes = None, timeout: float = 20):
        clean, auth = split_auth(url)
        headers = {"Authorization": auth} if auth else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.urlopen(
            urllib.request.Request(clean, data=data, headers=headers), timeout=timeout
        )

    def version(self, timeout: float = 20) -> dict:
        """The CDP handshake (`GET /json/version`) Playwright would do first.

        Doing it up front turns an unreachable box into one actionable line
        instead of a Playwright stack trace. It is the common failure: a
        `trycloudflare.com` quick-tunnel hostname dies with the `cloudflared`
        process that minted it, and the token then points at nothing.
        """
        url = (self.cdp_url or "").rstrip("/") + "/json/version"
        try:
            with self._request(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            hint = " -- token rejected" if exc.code in (401, 403) else ""
            raise SidekickError(f"{self.host} answered HTTP {exc.code}{hint}") from exc
        except Exception as exc:
            raise SidekickError(
                f"cannot reach {self.host}: {exc}. The box may be stopped, or its "
                "tunnel hostname retired -- re-run the sidekick installer and "
                f"export the fresh {ENV_VAR}."
            ) from exc

    def run(self, cmd: str, cwd: str = None, timeout_sec: int = 120) -> dict:
        """Run a shell command on the box. Returns {'exit_code', 'stdout', 'stderr'}.

        The exec API streams newline-delimited JSON events; this collects them,
        which is what a caller wants for a short command.
        """
        if not self.exec_url:
            raise SidekickError("this token carries no exec_url")
        body = json.dumps({"cmd": cmd, "cwd": cwd or "/root", "timeout_sec": timeout_sec}).encode()
        out, err, code = [], [], None
        try:
            with self._request(self.exec_url, data=body, timeout=timeout_sec + 30) as resp:
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if "stdout" in event:
                        out.append(event["stdout"])
                    if "stderr" in event:
                        err.append(event["stderr"])
                    if "exit_code" in event:
                        code = event["exit_code"]
        except urllib.error.HTTPError as exc:
            raise SidekickError(f"exec on {self.host} failed: HTTP {exc.code}") from exc
        except SidekickError:
            raise
        except Exception as exc:
            raise SidekickError(f"exec on {self.host} failed: {exc}") from exc
        return {"exit_code": code, "stdout": "".join(out), "stderr": "".join(err)}

    # --------------------------------------------------------------- browser

    def connect(self, pw):
        """Attach Playwright to the box's Chromium over CDP.

        Returns (browser, context). The context is the one already running on
        the box, so its cookies -- the Facebook session logged in by hand -- are
        the point of the whole exercise. Never close it: closing would discard
        the profile state the next run needs.
        """
        clean, auth = split_auth(self.cdp_url)
        headers = {"Authorization": auth} if auth else None
        browser = pw.chromium.connect_over_cdp(clean, headers=headers)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, ctx

    def close(self, browser=None) -> None:
        """Deliberately nothing.

        The box's browser outlives us -- it holds the profile. Dropping the
        driver is enough to disconnect; closing anything here would discard the
        session the next run needs.
        """


def main() -> int:
    """`python sidekick.py` -- check the token and the box behind it."""
    try:
        sk = load(required=True)
    except SidekickError as exc:
        print(f"error: {exc}")
        return 2
    print(sk.describe())
    try:
        version = sk.version()
    except SidekickError as exc:
        print(f"unreachable: {exc}")
        return 1
    print(f"reachable: {version.get('Browser', 'unknown browser')}")
    if sk.watch_url:
        print(f"watch: {sk.watch_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
