"""Standalone Robinhood OAuth. Credentials belong to robin-ops, never an AI host.

Uses public-client registration, authorization-code + S256 PKCE, a loopback
callback, and native OS credential storage. No plaintext credential fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import certifi

RESOURCE = "https://agent.robinhood.com/mcp/trading"
METADATA = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
PROTECTED = "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
AUTHORIZATION = "https://robinhood.com/oauth"
REGISTER = "https://agent.robinhood.com/oauth/trading/register"
TOKEN = "https://api.robinhood.com/oauth2/token/"
SERVICE = "robin-ops.robinhood.oauth"
ACCOUNT = "standalone-v1"
REDIRECT = "http://127.0.0.1:8765/callback"
_lock = threading.Lock()


class AuthError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthError("Unexpected OAuth HTTP redirect; refusing to forward credentials")


def request_json(url, data=None, *, form=False):
    if url not in {METADATA, PROTECTED, REGISTER, TOKEN}:
        raise AuthError("Untrusted OAuth endpoint")
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    opener = urllib.request.build_opener(
        NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
    )
    try:
        with opener.open(
            urllib.request.Request(url, data=body, headers=headers), timeout=30
        ) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise AuthError("OAuth response too large")
            value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise AuthError(
            f"Robinhood OAuth returned HTTP {exc.code}; login/registration may require "
            f"provider approval"
        ) from None
    except (OSError, ValueError) as exc:
        raise AuthError(
            f"OAuth connection failed ({type(exc).__name__}); credentials were not logged"
        ) from None
    if not isinstance(value, dict) or value.get("error"):
        raise AuthError("Robinhood returned an invalid OAuth response")
    return value


def discover():
    protected = request_json(PROTECTED)
    meta = request_json(METADATA)
    if protected.get("resource") != RESOURCE or RESOURCE not in protected.get(
        "authorization_servers", []
    ):
        raise AuthError("Unexpected OAuth resource or issuer")
    expected = {
        "issuer": RESOURCE,
        "authorization_endpoint": AUTHORIZATION,
        "registration_endpoint": REGISTER,
        "token_endpoint": TOKEN,
    }
    if any(meta.get(k) != v for k, v in expected.items()):
        raise AuthError("Robinhood OAuth endpoints changed; review required")
    if "S256" not in meta.get("code_challenge_methods_supported", []):
        raise AuthError("Server does not advertise S256 PKCE")
    if "none" not in meta.get("token_endpoint_auth_methods_supported", []):
        raise AuthError("Server does not support a public OAuth client")
    return meta


class CredentialStore:
    """Explicit native keyring only; never reuse ChatGPT/Codex credentials."""

    def __init__(self):
        try:
            import keyring

            backend = keyring.get_keyring()
            module = type(backend).__module__
            if module not in {
                "keyring.backends.macOS",
                "keyring.backends.Windows",
                "keyring.backends.SecretService",
            }:
                raise AuthError(
                    "A native OS keyring is required; plaintext/fallback storage is disabled"
                )
            self.backend = backend
        except ImportError:
            raise AuthError('Install OAuth dependencies: pip install -e ".[oauth]"') from None

    def read(self):
        raw = self.backend.get_password(SERVICE, ACCOUNT)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("resource") != RESOURCE:
                raise ValueError()
            return value
        except ValueError:
            raise AuthError(
                "Stored credentials are invalid; use auth-logout and sign in again"
            ) from None

    def save(self, value):
        self.backend.set_password(SERVICE, ACCOUNT, json.dumps(value, allow_nan=False))

    def clear(self):
        if self.backend.get_password(SERVICE, ACCOUNT):
            self.backend.delete_password(SERVICE, ACCOUNT)


@contextmanager
def credential_lock():
    """Serialize login/refresh across local processes to protect rotating tokens."""
    import fcntl

    folder = Path.home() / ".local" / "state" / "robin-ops"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077:
        raise AuthError("OAuth lock directory must be private and owned by this user")
    with _lock:
        fd = os.open(folder / "oauth.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AuthError(
                    "Another login or refresh is running; try again after it finishes"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def register_client():
    result = request_json(
        REGISTER,
        {
            "client_name": "robin-ops",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = result.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise AuthError("Registration did not return a client ID")
    if result.get("client_secret") or result.get("token_endpoint_auth_method", "none") != "none":
        raise AuthError("Registration requires unsupported confidential-client authentication")
    if result.get("client_name") not in {"robin-ops", "Robinhood Trading"}:
        raise AuthError("Provider returned a different client identity; do not authorize it")
    if REDIRECT not in result.get("redirect_uris", []):
        raise AuthError("Provider did not register the requested local callback")
    return {
        "resource": RESOURCE,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "client_name": result["client_name"],
    }


def authorization_url(client_id, state, verifier):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return (
        AUTHORIZATION
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "scope": "internal",
                "resource": RESOURCE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def callback_code(path, state):
    parsed = urllib.parse.urlsplit(path)
    if parsed.path != "/callback":
        raise AuthError("Unexpected callback path")
    params = urllib.parse.parse_qs(parsed.query)
    if len(params.get("state", [])) != 1 or not secrets.compare_digest(params["state"][0], state):
        raise AuthError("Invalid OAuth state")
    if "iss" in params and params["iss"] != [RESOURCE]:
        raise AuthError("Unexpected callback issuer")
    if "error" in params:
        raise AuthError("Robinhood authorization was declined or failed")
    if len(params.get("code", [])) != 1:
        raise AuthError("Missing or ambiguous authorization code")
    return params["code"][0]


def token_record(previous, response):
    token = response.get("access_token")
    if (
        not isinstance(token, str)
        or not token
        or response.get("token_type", "").lower() != "bearer"
    ):
        raise AuthError("Invalid token response")
    expires = response.get("expires_in")
    if (
        isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or expires <= 0
    ):
        raise AuthError("Token response has no valid expiry")
    refresh = response.get("refresh_token", previous.get("refresh_token"))
    if refresh is not None and (not isinstance(refresh, str) or not refresh):
        raise AuthError("Invalid refresh token")
    return {
        **previous,
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": time.time() + expires,
    }


def login(*, open_browser=True, announce=print, store=None, timeout=300):
    store = store or CredentialStore()
    with credential_lock():
        discover()
        current = store.read()
        if not current:
            current = register_client()
            store.save(current)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        outcome = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # callback includes a secret authorization code

            def do_GET(self):
                try:
                    code = callback_code(self.path, state)
                except AuthError:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(
                        b"Authorization not completed. Return to robin-ops or retry login."
                    )
                    return
                outcome["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(
                    b"Authorization received. Return to robin-ops to confirm sign-in completed."
                )

        try:
            server = HTTPServer(("127.0.0.1", 8765), Callback)
        except OSError:
            raise AuthError(
                "Local callback port 8765 unavailable; close the other login and retry"
            ) from None
        with server:
            server.timeout = 1
            url = authorization_url(current["client_id"], state, verifier)
            announce(
                "Authorize your own robin-ops client. Robinhood grants broad "
                "account/trading permissions; this release blocks live trading."
            )
            announce(
                "Robinhood registered this display name: "
                f"{current.get('client_name', 'robin-ops')}. "
                "If consent names ChatGPT, Codex or another unrelated app, do not approve it."
            )
            announce(url)
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while "code" not in outcome and time.monotonic() < deadline:
                server.handle_request()
            if "code" not in outcome:
                raise AuthError("Authorization timed out; run auth-login again")
        response = request_json(
            TOKEN,
            {
                "grant_type": "authorization_code",
                "client_id": current["client_id"],
                "code": outcome["code"],
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "resource": RESOURCE,
            },
            form=True,
        )
        store.save(token_record(current, response))
    announce(
        "Signed in as robin-ops. Credentials saved to the native OS keyring. Live trading "
        "remains disabled."
    )


def access_token(store=None):
    store = store or CredentialStore()
    with credential_lock():
        current = store.read()
        if not current.get("access_token"):
            raise AuthError("Run optionsagent auth-login to connect your own agent")
        if current.get("expires_at", 0) > time.time() + 60:
            return current["access_token"]
        if not current.get("refresh_token"):
            raise AuthError("Session expired; run optionsagent auth-login again")
        response = request_json(
            TOKEN,
            {
                "grant_type": "refresh_token",
                "client_id": current["client_id"],
                "refresh_token": current["refresh_token"],
                "resource": RESOURCE,
            },
            form=True,
        )
        updated = token_record(current, response)
        store.save(updated)
        return updated["access_token"]
