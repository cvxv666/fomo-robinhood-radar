"""Invites: a link of your own, and a week of PRO for both of you when somebody new joins by it.

The chats that stay are the ones that were brought by another reader, and a reader who brings
one is worth a week. The code on the link is a short digest of the chat id with the bot's token
- not the id itself, so a link says nothing about who sent it. Only a chat the bot has never
seen counts, one reward per new chat, and no inviter is rewarded more than
`pro_referral_max_per_month` times a month, because a Telegram account is cheap and a week of
PRO is not.
"""
from __future__ import annotations

import hashlib
import sqlite3

from .. import db
from ..config import settings
from . import pro


def code_for(chat_id) -> str:
    seed = f"{chat_id}:{settings.telegram_bot_token or 'radar'}".encode()
    return hashlib.sha256(seed).hexdigest()[:8]


def ensure_code(conn: sqlite3.Connection, chat_id) -> str:
    row = conn.execute("SELECT ref_code FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    if row and row["ref_code"]:
        return row["ref_code"]
    code = code_for(chat_id)
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET ref_code=? WHERE chat_id=?", (code, str(chat_id)))
    return code


def link(conn: sqlite3.Connection, chat_id) -> str:
    return f"https://t.me/{settings.telegram_bot_name.lstrip('@')}?start=r{ensure_code(conn, chat_id)}"


def stats(conn: sqlite3.Connection, chat_id) -> dict:
    row = conn.execute("SELECT COUNT(*) n, SUM(rewarded) rewarded FROM referrals WHERE referrer=?", (str(chat_id),)).fetchone()
    return {"joined": row["n"] or 0, "rewarded": row["rewarded"] or 0}


def join(conn: sqlite3.Connection, chat_id, code: str, was_new: bool, now: int | None = None) -> dict | None:
    """A /start with an invite code, from a chat the bot had not seen. Returns what happened,
    or None when the code is nobody's, the chat is not new, or the chat invited itself."""
    now = now or db.now()
    if not was_new or not code:
        return None
    ref = conn.execute("SELECT chat_id FROM bot_subscribers WHERE ref_code=?", (code,)).fetchone()
    if ref is None or ref["chat_id"] == str(chat_id):
        return None
    if conn.execute("SELECT 1 FROM referrals WHERE chat_id=?", (str(chat_id),)).fetchone():
        return None
    month = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer=? AND rewarded=1 AND at >= ?",
                         (ref["chat_id"], now - 30 * 86400)).fetchone()[0]
    reward = pro.enabled() and settings.pro_referral_days > 0 and month < settings.pro_referral_max_per_month
    with db.tx(conn):
        conn.execute("INSERT INTO referrals(chat_id, referrer, at, rewarded) VALUES(?,?,?,?)", (str(chat_id), ref["chat_id"], now, int(reward)))
    if reward:
        pro.grant(conn, chat_id, settings.pro_referral_days, now)
        pro.grant(conn, ref["chat_id"], settings.pro_referral_days, now)
    return {"referrer": ref["chat_id"], "rewarded": bool(reward), "days": settings.pro_referral_days}
