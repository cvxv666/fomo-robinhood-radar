"""X, as a place the radar posts.

OAuth 2.0 user context with PKCE: the app belongs to the author's developer account, the bot's
own account authorises it once (scripts/x_auth.py), and from then on the refresh token - which X
rotates on every use - lives in a file the server owns, never in the repo, never in a message.
Two calls are all the radar needs: post a text (optionally as a reply), and attach one image.
Pay-per-use since February 2026: a post is $0.015, a post carrying a URL $0.20, which is why
nothing the radar posts contains a link - the address of a token is not a URL.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import pathlib
import secrets
import time
import urllib.parse

import httpx

from ..config import settings

log = logging.getLogger(__name__)

AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read offline.access media.write"


class XError(RuntimeError):
    pass


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for one authorisation."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


class XClient:
    """The bot account's voice: refreshes its own token, posts, uploads an image."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 token_file: str | pathlib.Path | None = None, http: httpx.Client | None = None):
        self.client_id = client_id or settings.x_client_id
        self.client_secret = client_secret if client_secret is not None else settings.x_client_secret
        self.token_file = pathlib.Path(token_file or settings.x_token_file)
        self.http = http or httpx.Client(timeout=30)
        self.requests = 0
        self._tokens: dict | None = None

    # ---------- tokens ----------

    def _load(self) -> dict:
        if self._tokens is None:
            if not self.token_file.exists():
                raise XError(f"no X token at {self.token_file}; run scripts/x_auth.py once")
            self._tokens = json.loads(self.token_file.read_text(encoding="utf-8"))
        return self._tokens

    def _save(self, tokens: dict) -> None:
        self._tokens = tokens
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens), encoding="utf-8")
        tmp.replace(self.token_file)

    def _auth_header(self) -> dict:
        """The app's own credentials on the token endpoint: a confidential client sends them as
        HTTP Basic, a public one sends client_id in the body."""
        if self.client_secret:
            raw = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        return {}

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> dict:
        """The one-time trade of an authorisation code for the first token pair."""
        r = self.http.post(TOKEN_URL, headers=self._auth_header(), data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier, "client_id": self.client_id})
        if r.status_code != 200:
            raise XError(f"token exchange failed: {r.status_code} {r.text[:200]}")
        tokens = r.json()
        tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 7200))
        self._save(tokens)
        return tokens

    def refresh(self) -> dict:
        """A new access token, and - X rotates it - a new refresh token, saved before use."""
        t = self._load()
        r = self.http.post(TOKEN_URL, headers=self._auth_header(), data={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": self.client_id})
        self.requests += 1
        if r.status_code != 200:
            raise XError(f"token refresh failed: {r.status_code} {r.text[:200]}")
        fresh = r.json()
        fresh["expires_at"] = time.time() + float(fresh.get("expires_in", 7200))
        fresh.setdefault("refresh_token", t["refresh_token"])
        self._save(fresh)
        return fresh

    def access_token(self) -> str:
        t = self._load()
        if time.time() > float(t.get("expires_at", 0)) - 60:
            t = self.refresh()
        return t["access_token"]

    # ---------- the two calls ----------

    def post(self, text: str, media_ids: list[str] | None = None, reply_to: str | None = None) -> str:
        """Publish. Returns the post id."""
        body: dict = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": list(media_ids)}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = self.http.post(f"{API}/tweets", headers={"Authorization": f"Bearer {self.access_token()}"}, json=body)
        self.requests += 1
        if r.status_code == 401:
            # an access token that died early; once more with a fresh one
            self.refresh()
            r = self.http.post(f"{API}/tweets", headers={"Authorization": f"Bearer {self.access_token()}"}, json=body)
            self.requests += 1
        if r.status_code not in (200, 201):
            raise XError(f"post failed: {r.status_code} {r.text[:300]}")
        return str(r.json()["data"]["id"])

    def upload(self, path: str | pathlib.Path) -> str:
        """One image, up to 5 MB, as a media id to attach to a post."""
        path = pathlib.Path(path)
        with open(path, "rb") as fh:
            r = self.http.post(f"{API}/media/upload", headers={"Authorization": f"Bearer {self.access_token()}"},
                               data={"media_category": "tweet_image"},
                               files={"media": (path.name, fh, "image/png" if path.suffix == ".png" else "image/jpeg")})
        self.requests += 1
        if r.status_code not in (200, 201):
            raise XError(f"upload failed: {r.status_code} {r.text[:300]}")
        j = r.json()
        return str((j.get("data") or j).get("id") or (j.get("data") or j).get("media_id_string") or (j.get("data") or j).get("media_id"))
