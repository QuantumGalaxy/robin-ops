"""Credential-free tests for the standalone OAuth boundaries."""

import time
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit

import pytest

from optionsagent.mcp import oauth
from optionsagent.mcp.client import HttpToolCaller, McpAuthError


class MemoryStore:
    def __init__(self, value=None):
        self.value = value or {}

    def read(self):
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)


@pytest.fixture(autouse=True)
def local_lock(monkeypatch):
    monkeypatch.setattr(oauth, "credential_lock", nullcontext)


def test_pkce_known_rfc7636_vector():
    url = oauth.authorization_url(
        "own-client", "state", "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]
    assert q["client_id"] == ["own-client"]
    assert q["resource"] == [oauth.RESOURCE]
    assert q["redirect_uri"] == [oauth.REDIRECT]


@pytest.mark.parametrize(
    "path",
    [
        "/callback?state=wrong&code=secret",
        "/callback?state=ok&state=ok&code=secret",
        "/callback?state=ok&code=a&code=b",
        "/other?state=ok&code=secret",
        "/callback?state=ok&code=secret&iss=https://evil.example",
        "/callback?state=ok&error=access_denied",
    ],
)
def test_untrusted_callback_never_returns_code(path):
    with pytest.raises(oauth.AuthError):
        oauth.callback_code(path, "ok")


def test_valid_callback():
    assert oauth.callback_code("/callback?state=ok&code=secret", "ok") == "secret"


def test_registration_uses_own_identity(monkeypatch):
    def request(url, data):
        assert url == oauth.REGISTER
        assert data["client_name"] == "robin-ops"
        assert data["token_endpoint_auth_method"] == "none"
        return {"client_id": "own", **data}

    monkeypatch.setattr(oauth, "request_json", request)
    assert oauth.register_client()["client_id"] == "own"


@pytest.mark.parametrize(
    "changes", [{"client_name": "ChatGPT"}, {"redirect_uris": []}, {"client_secret": "secret"}]
)
def test_registration_cannot_silently_substitute_another_client(monkeypatch, changes):
    result = {
        "client_id": "own",
        "client_name": "robin-ops",
        "redirect_uris": [oauth.REDIRECT],
        **changes,
    }
    monkeypatch.setattr(oauth, "request_json", lambda *a, **kw: result)
    with pytest.raises(oauth.AuthError):
        oauth.register_client()


def test_refresh_rotates_and_saves_before_return(monkeypatch):
    store = MemoryStore(
        {
            "resource": oauth.RESOURCE,
            "client_id": "own",
            "access_token": "old",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        }
    )

    def request(url, data, *, form):
        assert url == oauth.TOKEN
        assert form and data["refresh_token"] == "old-refresh"
        assert data["client_id"] == "own" and data["resource"] == oauth.RESOURCE
        return {
            "token_type": "Bearer",
            "access_token": "new",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "request_json", request)
    assert oauth.access_token(store) == "new"
    assert store.value["refresh_token"] == "new-refresh"
    assert store.value["expires_at"] > time.time()


def test_valid_token_never_refreshes(monkeypatch):
    store = MemoryStore({"access_token": "valid", "expires_at": time.time() + 3600})
    monkeypatch.setattr(oauth, "request_json", lambda *a, **kw: pytest.fail("unexpected network"))
    assert oauth.access_token(store) == "valid"


def test_expired_token_without_refresh_stops():
    with pytest.raises(oauth.AuthError, match="expired"):
        oauth.access_token(MemoryStore({"access_token": "expired", "expires_at": 0}))


@pytest.mark.parametrize("expires", [None, -1, float("nan"), float("inf"), True])
def test_invalid_expiry_never_persists(expires):
    with pytest.raises(oauth.AuthError):
        oauth.token_record(
            {}, {"access_token": "secret", "token_type": "bearer", "expires_in": expires}
        )


def test_token_cannot_be_sent_to_different_resource():
    with pytest.raises(McpAuthError, match="official"):
        HttpToolCaller(url="https://evil.example", token="test-secret")


def test_keyring_supplies_transport_without_ai_host(monkeypatch):
    monkeypatch.delenv("ROBINHOOD_MCP_TOKEN", raising=False)
    monkeypatch.setattr(oauth, "access_token", lambda: "own-token")
    caller = HttpToolCaller()
    assert caller.token == "own-token"
    assert caller._use_keyring
    assert "own-token" not in repr(caller)


def test_oauth_redirect_never_forwards_secrets():
    with pytest.raises(oauth.AuthError, match="redirect"):
        oauth.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")


def test_untrusted_metadata_fails_closed(monkeypatch):
    monkeypatch.setattr(oauth, "request_json", lambda *a: {"resource": "https://evil.example"})
    with pytest.raises(oauth.AuthError, match="resource"):
        oauth.discover()


def test_server_assigned_generic_robinhood_name_is_preserved(monkeypatch):
    result = {
        "client_id": "registered",
        "client_name": "Robinhood Trading",
        "redirect_uris": [oauth.REDIRECT],
    }
    monkeypatch.setattr(oauth, "request_json", lambda *a, **kw: result)
    assert oauth.register_client()["client_name"] == "Robinhood Trading"
