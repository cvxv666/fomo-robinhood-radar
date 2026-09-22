"""The four Bot API calls we need, and nothing else.

Deliberately dependency-free: the Bot API is plain HTTP and we already ship httpx. Long polling
rather than webhooks, so the bot runs from a laptop behind NAT exactly as well as from a server
with a public address - which keeps the hosting decision open.
"""
from __future__ import annotations

import logging
import pathlib

import httpx

from ..config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


# ---------------------------------------------------------------- transport

class Telegram:
    """The four Bot API calls we need, and nothing else."""

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token or settings.telegram_bot_token
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set (talk to @BotFather, see .env.example)")
        # api.telegram.org is blocked by some ISPs, Russian ones included: DNS resolves, the TCP
        # connection then times out. A proxy fixes it locally; on a server outside that jurisdiction
        # none is needed, which is the real reason the bot belongs on the server.
        # The read timeout has to outlast a long poll, or every idle poll looks like a failure.
        self.http = client or httpx.Client(
            timeout=settings.telegram_poll_timeout + 15,
            proxy=settings.telegram_proxy or None,
        )
        self.requests = 0
        self._file_ids: dict[str, str] = {}

    def call(self, method: str, **params) -> object:
        self.requests += 1
        r = self.http.post(API.format(token=self.token, method=method), json=params)
        if r.status_code == 409:
            raise TelegramError("another copy of this bot is already polling — stop it first")
        if r.status_code == 401:
            raise TelegramError("TELEGRAM_BOT_TOKEN rejected — check it, or /revoke a new one")
        # Telegram puts the reason in the body - "Forbidden: bot was blocked by the user",
        # "Bad Request: chat not found" - and raise_for_status would throw it away in favour of
        # the bare status line. The reason is what the caller decides on: a chat that blocked us
        # is dropped, and without it eleven blocked chats were retried on every broadcast for a
        # day, seven thousand warnings' worth.
        if r.status_code in (400, 403):
            try:
                reason = r.json().get("description")
            except ValueError:
                reason = None
            raise TelegramError(f"{method}: {reason or r.reason_phrase}")
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result")

    def send(self, chat_id, text: str, preview: bool = False) -> object:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=not preview)

    def photo(self, chat_id, path: pathlib.Path, caption: str) -> object:
        """Send a local image with a caption, uploading it at most once.

        Telegram hands back a file_id for anything it has stored, and accepts that id in place of
        the bytes forever after — so the banner crosses the wire on the first /start of a process
        and never again.
        """
        self.requests += 1
        url = API.format(token=self.token, method="sendPhoto")
        params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        known = self._file_ids.get(str(path))
        if known:
            r = self.http.post(url, json={**params, "photo": known})
        else:
            with open(path, "rb") as fh:
                r = self.http.post(url, data=params, files={"photo": fh})
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise TelegramError(f"sendPhoto: {body.get('description')}")
        result = body.get("result") or {}
        sizes = result.get("photo") or []
        if sizes and not known:
            self._file_ids[str(path)] = sizes[-1]["file_id"]
        return result

    def updates(self, offset: int, timeout: int | None = None) -> list:
        return self.call("getUpdates", offset=offset, allowed_updates=["message"],
                         timeout=timeout if timeout is not None else settings.telegram_poll_timeout)

    def me(self) -> dict:
        return self.call("getMe")

    def set_commands(self, commands: list[tuple[str, str]]) -> object:
        """The list under the menu button. Idempotent, so it runs on every start."""
        return self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
