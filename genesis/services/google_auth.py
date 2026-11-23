"""Google OAuth 2.0 for installed (desktop) applications.

Implements the "loopback IP address" flow with PKCE (RFC 7636), which is what
Google recommends for desktop apps:

1. Genesis starts a one-shot HTTP listener on 127.0.0.1:<random port>.
2. The user's browser opens Google's consent screen.
3. Google redirects back to the loopback listener with an authorization code.
4. Genesis exchanges the code (+ PKCE verifier) for access/refresh tokens.

The user never gives Genesis their Google password. Tokens are stored only in
the encrypted :class:`SecretStore` and are refreshed automatically.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import requests

from ..core.logging_setup import get_logger
from ..security.secrets import SecretStore

log = get_logger("google")

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts + send
    "https://www.googleapis.com/auth/gmail.readonly",  # read/search mail
    "https://www.googleapis.com/auth/calendar.events",  # create/read/move/cancel calendar events
]
TOKEN_KEY = "google_oauth_token"


class GoogleAuthError(RuntimeError):
    """Not connected, consent denied, or refresh token expired/revoked."""


class GoogleNotConfigured(GoogleAuthError):
    pass


def load_client_config(client_id: Optional[str], client_secret: Optional[str], secrets_file: Optional[str]) -> Optional[dict]:
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    if secrets_file and Path(secrets_file).expanduser().exists():
        data = json.loads(Path(secrets_file).expanduser().read_text(encoding="utf-8"))
        inner = data.get("installed") or data.get("web") or {}
        if inner.get("client_id"):
            return {"client_id": inner["client_id"], "client_secret": inner.get("client_secret", "")}
    return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result = {k: v[0] for k, v in qs.items()}
        ok = "code" in qs
        body = (
            "<html><body style='font-family:Segoe UI,sans-serif;background:#0d0d10;color:#eee;text-align:center;padding-top:80px'>"
            f"<h2 style='color:#e0314b'>Genesis</h2><p>{'Google account connected. You can close this tab.' if ok else 'Authorization failed or was cancelled.'}</p>"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence
        pass


class GoogleAuth:
    def __init__(self, client_config: Optional[dict], store: SecretStore, *, session: Optional[requests.Session] = None,
                 on_change: Optional[Callable[[bool, Optional[str]], None]] = None) -> None:
        self.client = client_config
        self.store = store
        self.session = session or requests.Session()
        self.on_change = on_change
        self._lock = threading.RLock()

    # ------------------------------------------------------------ state
    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.get("client_id"))

    def _load(self) -> Optional[dict]:
        raw = self.store.get(TOKEN_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _save(self, tok: dict) -> None:
        self.store.set(TOKEN_KEY, json.dumps(tok))

    @property
    def connected(self) -> bool:
        tok = self._load()
        return bool(tok and tok.get("refresh_token"))

    def account_email(self) -> Optional[str]:
        tok = self._load() or {}
        return tok.get("email")

    def granted_scopes(self) -> list[str]:
        return ((self._load() or {}).get("scope") or "").split()

    # ------------------------------------------------------------ connect
    def connect(self, open_browser: Callable[[str], bool] = webbrowser.open, timeout: float = 300.0) -> str:
        """Run the interactive consent flow. Returns the connected account e-mail."""
        if not self.configured:
            raise GoogleNotConfigured(
                "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (or "
                "GOOGLE_CLIENT_SECRETS_FILE) in .env — see docs/SETUP.md#gmail."
            )
        assert self.client is not None
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        handler = type("Handler", (_CallbackHandler,), {"result": {}})
        server = http.server.HTTPServer(("127.0.0.1", port), handler)
        server.timeout = 1.0

        params = {
            "client_id": self.client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent",
        }
        url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"
        log.info("opening Google consent screen")
        open_browser(url)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and not handler.result:
                server.handle_request()
        finally:
            server.server_close()
        result = handler.result
        if not result:
            raise GoogleAuthError("Timed out waiting for Google authorization.")
        if result.get("state") != state:
            raise GoogleAuthError("OAuth state mismatch — possible CSRF; aborted.")
        if "error" in result:
            raise GoogleAuthError(f"Google authorization was denied ({result['error']}).")
        tok = self._exchange({
            "code": result["code"], "client_id": self.client["client_id"], "client_secret": self.client.get("client_secret", ""),
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        })
        email = self._fetch_email(tok["access_token"])
        tok["email"] = email
        self._save(tok)
        if self.on_change:
            self.on_change(True, email)
        return email or "your Google account"

    def _exchange(self, form: dict) -> dict:
        try:
            resp = self.session.post(TOKEN_URI, data=form, timeout=30)
        except requests.RequestException as exc:
            raise GoogleAuthError(f"Could not reach Google ({type(exc).__name__}).") from exc
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", "error")
            except ValueError:
                err = f"HTTP {resp.status_code}"
            raise GoogleAuthError(f"Google token request failed: {err}.")
        data = resp.json()
        data["expires_at"] = time.time() + float(data.get("expires_in", 3600)) - 60
        return data

    def _fetch_email(self, access_token: str) -> Optional[str]:
        try:
            r = self.session.get(USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
            if r.status_code == 200:
                return r.json().get("email")
        except requests.RequestException:
            pass
        return None

    # ------------------------------------------------------------ tokens
    def access_token(self) -> str:
        with self._lock:
            tok = self._load()
            if not tok or not tok.get("refresh_token"):
                raise GoogleAuthError("Your Google account isn't connected. Connect it in Genesis → Services.")
            if tok.get("access_token") and time.time() < float(tok.get("expires_at", 0)):
                return tok["access_token"]
            if not self.configured:
                raise GoogleNotConfigured("Google OAuth client is not configured.")
            assert self.client is not None
            try:
                fresh = self._exchange({
                    "client_id": self.client["client_id"], "client_secret": self.client.get("client_secret", ""),
                    "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
                })
            except GoogleAuthError as exc:
                if "invalid_grant" in str(exc):
                    raise GoogleAuthError(
                        "Your Google authorization has expired or was revoked. Please reconnect your Google account."
                    ) from exc
                raise
            tok.update({k: v for k, v in fresh.items() if k != "refresh_token" or v})
            self._save(tok)
            return tok["access_token"]

    def invalidate_access_token(self) -> None:
        with self._lock:
            tok = self._load()
            if tok:
                tok["expires_at"] = 0
                self._save(tok)

    def disconnect(self) -> None:
        tok = self._load()
        if tok:
            for t in (tok.get("refresh_token"), tok.get("access_token")):
                if t:
                    try:
                        self.session.post(REVOKE_URI, data={"token": t}, timeout=10)
                    except requests.RequestException:
                        pass
        self.store.delete(TOKEN_KEY)
        if self.on_change:
            self.on_change(False, None)
