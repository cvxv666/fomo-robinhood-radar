"""Follow a wallet: its fills, to the chats that asked, within a tick of the chain.

The feeds answer "what is the cohort doing"; this answers "what is *this one* doing", which is the
question a reader has once a name has been right twice. `/follow unipcs` and every fill unipcs
makes from then on lands in that chat - bought what for how much, sold what - twenty seconds
after the block, with the buy link. Only the wallet's own trades: a fill judged dust, direct or
seed (provenance) is somebody pushing a token at the wallet, and is not sent as if the wallet
had acted.

A free chat follows a few, a PRO chat fifty, and no chat gets more than `follow_max_per_hour`
of these in an hour, because a wallet that fills three hundred times an hour is a bot and the
noise sweep will have it by then anyway.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings

log = logging.getLogger(__name__)


def resolve(conn: sqlite3.Connection, who: str) -> sqlite3.Row | None:
    """A handle (any case) or an address -> the trader row, tracked ones only."""
    who = (who or "").strip().lstrip("@")
    if not who:
        return None
    if who.startswith("0x") and len(who) == 42:
        return conn.execute("SELECT address, fomo_handle, score, status FROM traders WHERE address = ?", (who.lower(),)).fetchone()
    return conn.execute("SELECT address, fomo_handle, score, status FROM traders WHERE fomo_handle = ? COLLATE NOCASE "
                        "ORDER BY score DESC NULLS LAST LIMIT 1", (who,)).fetchone()


def limit_for(conn: sqlite3.Connection, chat_id, now: int | None = None) -> int:
    from . import pro
    return settings.follow_pro_max if pro.entitled(conn, chat_id, now) else settings.follow_free_max


def following(conn: sqlite3.Connection, chat_id) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.address, f.since, t.fomo_handle, t.score, t.status FROM follows f LEFT JOIN traders t ON t.address = f.address "
        "WHERE f.chat_id = ? ORDER BY f.since", (str(chat_id),)).fetchall()


def follow(conn: sqlite3.Connection, chat_id, who: str, now: int | None = None) -> tuple[bool, str]:
    now = now or db.now()
    t = resolve(conn, who)
    if t is None:
        return False, f"I do not track anyone called {who!r}. A fomo handle or a 0x address of a wallet on the leaderboard."
    name = t["fomo_handle"] or t["address"][:10]
    have = following(conn, chat_id)
    if any(f["address"] == t["address"] for f in have):
        return True, f"Already following {name}."
    cap = limit_for(conn, chat_id, now)
    if len(have) >= cap:
        more = "" if cap >= settings.follow_pro_max else f" PRO follows {settings.follow_pro_max}: /pro."
        return False, f"This chat follows {len(have)} wallets already, the most it can.{more} /unfollow frees a slot."
    with db.tx(conn):
        conn.execute("INSERT OR IGNORE INTO follows(chat_id, address, since) VALUES(?,?,?)", (str(chat_id), t["address"], now))
    score = f", score {t['score']}" if t["score"] is not None else ""
    return True, f"Following {name}{score}. Every fill from now on, within a tick of the chain. {len(have) + 1} of {cap}."


def unfollow(conn: sqlite3.Connection, chat_id, who: str) -> tuple[bool, str]:
    if who.strip().lower() == "all":
        with db.tx(conn):
            n = conn.execute("DELETE FROM follows WHERE chat_id = ?", (str(chat_id),)).rowcount
        return True, f"Unfollowed {n}."
    t = resolve(conn, who)
    if t is None:
        return False, f"I do not track anyone called {who!r}."
    with db.tx(conn):
        n = conn.execute("DELETE FROM follows WHERE chat_id = ? AND address = ?", (str(chat_id), t["address"])).rowcount
    name = t["fomo_handle"] or t["address"][:10]
    return bool(n), (f"Unfollowed {name}." if n else f"This chat was not following {name}.")


def alerts(conn: sqlite3.Connection, sigs: list[str], now: int | None = None) -> list[tuple[str, dict]]:
    """(chat_id, fill) for every new fill of a followed wallet, real fills only, capped per chat."""
    if not sigs:
        return []
    now = now or db.now()
    out: list[tuple[str, dict]] = []
    marks = ",".join("?" * len(sigs))
    rows = conn.execute(
        "SELECT tr.sig, tr.address, tr.mint, tr.side, tr.usd_value, tr.ts, COALESCE(tr.kind, 'trade') kind, "
        "  t.fomo_handle handle, t.score, COALESCE(tk.symbol, substr(tr.mint, 1, 8)) sym, f.chat_id "
        "FROM trades tr JOIN follows f ON f.address = tr.address JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"WHERE tr.sig IN ({marks}) AND COALESCE(tr.kind, 'trade') = 'trade' ORDER BY tr.ts", sigs).fetchall()
    for r in rows:
        key = f"fill:{r['sig']}"
        if conn.execute("SELECT 1 FROM bot_sent WHERE chat_id = ? AND mint = ?", (r["chat_id"], key)).fetchone():
            continue
        sent_hour = conn.execute("SELECT COUNT(*) FROM bot_sent WHERE chat_id = ? AND mint LIKE 'fill:%' AND ts >= ?",
                                 (r["chat_id"], now - 3600)).fetchone()[0]
        if sent_hour >= settings.follow_max_per_hour:
            continue
        out.append((r["chat_id"], dict(r)))
        with db.tx(conn):
            conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?,?,?)", (r["chat_id"], key, now))
    return out
