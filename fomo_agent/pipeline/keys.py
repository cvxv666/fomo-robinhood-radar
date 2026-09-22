"""API keys: a PRO chat's, or one bought on the site.

Eight hundred addresses read the public API and six agents make a third of its traffic - copy
traders, paper bots, terminals. The public allowance stays what it is, 120 a minute per address
with no key and no sign-up. A PRO chat can ask the bot for a key and read five times as fast;
the key is the chat's, it lives as long as the chat's PRO does, and asking again replaces it.
Nothing about the reader is stored beyond the chat id the bot already had.

A key bought on the site (pipeline/checkout.py) carries its own term in `paid_until` instead,
and belongs to an order rather than to a chat - nothing about that reader is stored at all.
"""
from __future__ import annotations

import secrets
import sqlite3
import time

from .. import db
from . import pro

KEY_LEN = 24   # bytes of entropy; the key is its hex, 48 characters


def issue(conn: sqlite3.Connection, chat_id, now: int | None = None) -> str:
    """A fresh key for the chat; whatever it had before is revoked."""
    now = now or db.now()
    key = secrets.token_hex(KEY_LEN)
    with db.tx(conn):
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE chat_id=? AND revoked_at IS NULL", (now, str(chat_id)))
        conn.execute("INSERT INTO api_keys(key, chat_id, created_at) VALUES(?,?,?)", (key, str(chat_id), now))
    return key


def current(conn: sqlite3.Connection, chat_id) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM api_keys WHERE chat_id=? AND revoked_at IS NULL", (str(chat_id),)).fetchone()


def by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """The row behind a key the caller has just been authenticated with."""
    return conn.execute("SELECT * FROM api_keys WHERE key=? AND revoked_at IS NULL", (key,)).fetchone()


def live(row, now: int | None = None) -> bool:
    """Whether this key's own term is still running (a chat's key carries none and is not this)."""
    return bool(row) and (row["paid_until"] or 0) > (now or db.now())


class Keyring:
    """What the API asks on every request: is this key live, and is its chat PRO. Answered
    from memory for a minute per key, so a busy client costs one read a minute, not one a call."""

    def __init__(self, ttl_s: float = 60.0):
        self.ttl = ttl_s
        self._seen: dict[str, tuple[float, bool, str | None]] = {}
        self._counts: dict[str, int] = {}

    def check(self, conn: sqlite3.Connection, key: str) -> tuple[bool, str | None]:
        """(live and entitled, chat_id)."""
        now = time.monotonic()
        hit = self._seen.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1], hit[2]
        row = conn.execute("SELECT chat_id, paid_until FROM api_keys WHERE key=? AND revoked_at IS NULL", (key,)).fetchone()
        ok = bool(row) and (((row["paid_until"] or 0) > db.now()) or pro.entitled(conn, row["chat_id"]))
        self._seen[key] = (now, ok, row["chat_id"] if row else None)
        if len(self._seen) > 5000:
            self._seen.clear()
        return ok, row["chat_id"] if row else None

    def used(self, conn: sqlite3.Connection, key: str) -> None:
        """Counted in memory, written once a minute: a key that reads ten times a second is not
        ten database writes a second."""
        self._counts[key] = self._counts.get(key, 0) + 1
        hit = self._seen.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl and self._counts[key] < 200:
            return
        n = self._counts.pop(key, 0)
        with db.tx(conn):
            conn.execute("UPDATE api_keys SET last_used=?, requests=requests+? WHERE key=?", (db.now(), n, key))
