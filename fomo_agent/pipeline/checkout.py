"""Buying a key on the site, without Telegram in the way.

The burn rail already needs nobody: a quote reserves an amount of the token that no other open
quote has, the watcher sees a burn of exactly that amount land in a block, and the payment is
matched to the quote by the amount alone. The only thing that tied it to Telegram was who the
access was granted to - a chat id.

So a web order is an account of its own: `web:<secret>`, where the secret is what the page keeps
and nothing else knows. A quote is taken against that account exactly as a chat's is; when the
burn lands, the key is issued to the account instead of PRO being granted to a chat, and the
page - polling with the secret - shows the key and takes the webhook URL.

A thousand addresses poll this API every day and not one of them could buy a webhook without
first installing a messenger. This is the other door.
"""
from __future__ import annotations

import secrets
import sqlite3

from .. import db
from ..config import settings
from . import keys, pro

PREFIX = "web:"
SECRET_BYTES = 16   # 32 hex characters; the page's bearer for its own order


def is_web(account: object) -> bool:
    return str(account or "").startswith(PREFIX)


def start(conn: sqlite3.Connection, now: int | None = None) -> dict | None:
    """A new order: its secret and the quote to pay. None when there is no price to quote from."""
    if not pro.enabled():
        return None
    account = PREFIX + secrets.token_hex(SECRET_BYTES)
    q = pro.quote(conn, account, now)
    if q is None:
        return None
    return {"secret": account[len(PREFIX):], **_quote_view(q, now)}


def renew(conn: sqlite3.Connection, secret: str, now: int | None = None) -> dict | None:
    """Another term on the same order, so that the key a bot already carries is kept. The page
    calls this rather than starting a fresh order, and pays the same way."""
    if not _plausible(secret) or not pro.enabled():
        return None
    account = PREFIX + secret.strip().lower()
    if conn.execute("SELECT 1 FROM pro_quotes WHERE chat_id = ?", (account,)).fetchone() is None:
        return None
    q = pro.quote(conn, account, now)
    return None if q is None else {"secret": secret, **_quote_view(q, now)}


def _quote_view(q, now: int | None = None) -> dict:
    now = now or db.now()
    status = q["status"]
    if status == "open" and q["expires_at"] <= now:
        status = "expired"
    return {"code": q["code"], "usd": q["usd"], "price": q["price"], "tokens": q["tokens"],
            "symbol": settings.pro_token_symbol, "token": settings.pro_token,
            "burn_address": settings.pro_burn_address, "days": settings.pro_days,
            "created_at": q["created_at"], "expires_at": q["expires_at"], "status": status,
            "tx": q["tx"], "now": now}


def redeem(conn: sqlite3.Connection, account: str, days: int, now: int | None = None) -> int:
    """The burn for a web order landed: issue its key, or extend the one it has. Returns the end.

    The key is the account's for as long as it is paid for, and a renewal keeps it - a buyer who
    has wired it into a running bot should not have to re-deploy to pay again.
    """
    now = now or db.now()
    row = keys.current(conn, account)
    end = max(now, (row["paid_until"] or 0) if row else 0) + days * 86400
    with db.tx(conn):
        if row is None:
            conn.execute("INSERT INTO api_keys(key, chat_id, created_at, paid_until) VALUES(?,?,?,?)",
                         (secrets.token_hex(keys.KEY_LEN), account, now, end))
        else:
            conn.execute("UPDATE api_keys SET paid_until=? WHERE key=?", (end, row["key"]))
    return end


def status(conn: sqlite3.Connection, secret: str, now: int | None = None) -> dict | None:
    """What the page polls: the quote, and once it is paid, the key and the webhook on it.

    The secret is the bearer for this one order and is as good as the key it bought, so the key
    comes back on every look rather than once: a page reloaded is not a key lost.
    """
    now = now or db.now()
    account = PREFIX + (secret or "").strip().lower()
    if not _plausible(secret):
        return None
    q = conn.execute("SELECT * FROM pro_quotes WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (account,)).fetchone()
    if q is None:
        return None
    out = _quote_view(q, now)
    row = keys.current(conn, account)
    if row is not None:
        out.update({"key": row["key"], "paid_until": row["paid_until"],
                    "webhook_url": row["webhook_url"], "requests": row["requests"],
                    "live": (row["paid_until"] or 0) > now})
        if out["status"] != "paid" and out["live"]:
            out["status"] = "paid"   # a renewal whose new quote is still open, on a key that is live
    return out


def _plausible(secret: str | None) -> bool:
    s = (secret or "").strip()
    return len(s) == SECRET_BYTES * 2 and all(c in "0123456789abcdef" for c in s.lower())
