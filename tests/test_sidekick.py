"""Token handling for the Sidekick box.

The token is the whole credential for a machine, so the things worth pinning
are: it decodes the way the sidekick CLI emits it, a malformed one fails with a
sentence rather than a traceback from inside base64, and nothing that prints
carries the secret.
"""

import base64
import json

import pytest

import sidekick
from sidekick import Sidekick, SidekickError

SECRET = "KcQVwbMV68gbaHmbW0LasApam0tiPnrVugwXUni9NO8"
HOST = "theatre-encouraged-cottages-but.trycloudflare.com"


def make_claims(**overrides):
    """The shape the sidekick CLI mints: credentials embedded per endpoint."""
    claims = {
        "v": 1,
        "server_id": "c47d1f2e3a4b5c6d",
        "base_url": f"https://{HOST}",
        "auth_token": SECRET,
        "cdp_url": f"https://sidekick:{SECRET}@{HOST}",
        "watch_url": f"https://{HOST}/vnc.html?token={SECRET}",
        "shell_url": f"https://sidekick:{SECRET}@{HOST}/shell",
        "exec_url": f"https://sidekick:{SECRET}@{HOST}/exec",
        "platform": "fly",
        "created_at": "2026-08-16T09:54:06Z",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def encode(claims, pad=True) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return raw if pad else raw.rstrip("=")


class TestDecode:
    def test_round_trips_the_cli_format(self):
        claims = make_claims()
        assert sidekick.decode(encode(claims)) == claims

    def test_unpadded_token_is_accepted(self):
        """base64url in the wild arrives with the padding stripped."""
        claims = make_claims()
        assert sidekick.decode(encode(claims, pad=False)) == claims

    def test_surrounding_whitespace_is_ignored(self):
        """Shell exports and copy-paste both pick up a trailing newline."""
        assert sidekick.decode("\n  " + encode(make_claims()) + "  \n")["platform"] == "fly"

    @pytest.mark.parametrize(
        "raw,fragment",
        [
            ("", "empty"),
            ("not base64 !!", "base64url"),
            (base64.urlsafe_b64encode(b"not json at all").decode(), "JSON"),
            (base64.urlsafe_b64encode(b'["a","list"]').decode(), "expected an object"),
        ],
    )
    def test_malformed_tokens_explain_themselves(self, raw, fragment):
        with pytest.raises(SidekickError) as exc:
            sidekick.decode(raw)
        assert fragment in str(exc.value)

    @pytest.mark.parametrize("claim", ["base_url", "cdp_url"])
    def test_missing_required_claim_names_it(self, claim):
        with pytest.raises(SidekickError) as exc:
            sidekick.decode(encode(make_claims(**{claim: None})))
        assert claim in str(exc.value)

    def test_optional_claims_may_be_absent(self):
        """Only the endpoints the scraper drives are mandatory."""
        sk = Sidekick(sidekick.decode(encode(make_claims(watch_url=None, exec_url=None))))
        assert sk.watch_url is None and sk.exec_url is None


class TestLoad:
    def test_returns_none_without_a_token(self, monkeypatch):
        """No token set is the ordinary local case, not an error."""
        monkeypatch.delenv(sidekick.ENV_VAR, raising=False)
        assert sidekick.load() is None

    def test_blank_token_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv(sidekick.ENV_VAR, "   ")
        assert sidekick.load() is None

    def test_required_without_a_token_points_at_the_installer(self, monkeypatch):
        monkeypatch.delenv(sidekick.ENV_VAR, raising=False)
        with pytest.raises(SidekickError) as exc:
            sidekick.load(required=True)
        assert "install.sh" in str(exc.value)

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv(sidekick.ENV_VAR, encode(make_claims()))
        assert sidekick.load().host == HOST

    def test_explicit_argument_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv(sidekick.ENV_VAR, encode(make_claims()))
        other = encode(make_claims(base_url="https://other.example", cdp_url="https://other.example"))
        assert sidekick.load(other).host == "other.example"


class TestSplitAuth:
    def test_credentials_move_from_url_to_header(self):
        clean, auth = sidekick.split_auth(f"https://sidekick:{SECRET}@{HOST}/exec")
        assert clean == f"https://{HOST}/exec"
        assert base64.b64decode(auth.split()[1]).decode() == f"sidekick:{SECRET}"

    def test_port_and_query_survive(self):
        clean, _ = sidekick.split_auth(f"https://u:p@{HOST}:8443/exec?trace=1")
        assert clean == f"https://{HOST}:8443/exec?trace=1"

    def test_url_without_credentials_is_untouched(self):
        clean, auth = sidekick.split_auth(f"https://{HOST}/vnc.html")
        assert clean == f"https://{HOST}/vnc.html" and auth is None


class TestDescribe:
    """`describe()` is the line that goes into logs, so it must be safe."""

    def test_names_the_box_without_the_secret(self):
        line = Sidekick(make_claims()).describe()
        assert HOST in line and "fly" in line and "2026-08-16T09:54:06Z" in line
        assert SECRET not in line

    def test_survives_a_sparse_token(self):
        sk = Sidekick(make_claims(server_id=None, platform=None, created_at=None))
        assert HOST in sk.describe()
