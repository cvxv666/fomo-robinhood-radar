"""The push ledger, and the hour-later look back at each one.

Every message the bot pushed used to exist only as rows in `bot_sent` - chat, key, time - and
the number that mattered, the price the cohort was at when the message went out, had to be
reconstructed after the fact from bursts and the tape. Now a push is one row here: kind, token,
time, price, the heat or conviction that earned it, the pool's depth, how many chats got it.

An hour after each push the radar looks at what the market did with it and tells the same
chats: the peak so far and when it came, where the price is now, how much traded since. No
advice in it - a fact, the same fact every time - so that in a fortnight the distribution of
"when was the peak" over a few hundred pushes is on the books, and an exit rule can be read off
the data rather than guessed. Four of five pushes traded above their price; one in five is
still above it a day later. The hour between is where the product is decided.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import db
from ..config import settings

log = logging.getLogger(__name__)


def last_trusted_px(conn: sqlite3.Connection, mint: str, ts: int) -> float | None:
    row = conn.execute(
        "SELECT tr.usd_value / tr.token_amount p FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint=? AND tr.ts<=? AND tr.usd_value >= 1 AND tr.token_amount > 0 AND COALESCE(tr.kind,'trade')='trade' "
        "AND t.score >= 60 ORDER BY tr.ts DESC LIMIT 1", (mint, ts)).fetchone()
    return row["p"] if row else None


def record(conn: sqlite3.Connection, kind: str, item: dict, chats: int, now: int | None = None) -> int | None:
    """One row per push event. The same token pushed the same way inside the re-alert window is
    the same event and is not written twice. Returns the row id, or None when nothing was."""
    now = now or db.now()
    mint = item["mint"]
    quiet = settings.telegram_realert_hours * 3600
    if conn.execute("SELECT 1 FROM pushes WHERE kind=? AND mint=? AND ts >= ?", (kind, mint, now - quiet)).fetchone():
        return None
    px = item.get("px") if kind == "burst" else None
    if not px:
        px = last_trusted_px(conn, mint, now) or item.get("price")
    due = now + settings.telegram_followup_min * 60 if settings.telegram_followup_min > 0 else None
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO pushes(kind, mint, ts, px, heat, conviction, wallets, liq, chats, followup_due) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, mint, now, px, item.get("heat"), item.get("conviction"), item.get("wallets") or item.get("buyers"),
             item.get("liq"), chats, due))
    return cur.lastrowid


def due(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    """Pushes whose hour is up and whose look-back has not been sent. One that could not be
    measured for half an hour past its time is let go - the candles were not coming."""
    now = now or db.now()
    return conn.execute(
        "SELECT p.*, COALESCE(tk.symbol, substr(p.mint, 1, 8)) sym, tk.pool_address, tk.chain FROM pushes p "
        "LEFT JOIN tokens tk ON tk.mint = p.mint "
        "WHERE p.followup_due IS NOT NULL AND p.followup_at IS NULL AND p.followup_due <= ? AND p.followup_due > ? "
        "ORDER BY p.followup_due", (now, now - 1800)).fetchall()


def measure(conn: sqlite3.Connection, row: sqlite3.Row, gt=None, now: int | None = None) -> dict | None:
    """What the market did in the hour: peak and when, now, volume since. None when the pool's
    candles could not be had this time (the caller tries again next minute)."""
    from .hot import outcome

    now = now or db.now()
    ts, px = row["ts"], row["px"]
    candles = []
    if row["pool_address"]:
        try:
            if gt is None:
                from ..sources.geckoterminal import GeckoTerminal
                gt = GeckoTerminal(patient=False)
            candles = gt.ohlcv(row["chain"] or "robinhood", row["pool_address"], "minute", 5, 40) or []
        except Exception as e:  # noqa: BLE001 - the screener is busy; next minute then
            log.debug("followup candles for %s: %s", row["mint"][:10], e)
            return None
        if not candles:
            return None
    if not px:
        return {"best": None, "peak_min": None, "now": None, "vol": None, "witness": "none"}
    o = outcome(conn, row["mint"], ts, px, settings.telegram_followup_min * 60, now, candles)
    after = [c for c in candles if ts - 300 <= c[0] <= now]
    peak_min = None
    if after and o["best"] is not None:
        hi = max(after, key=lambda c: c[2])
        peak_min = max(0, round((hi[0] + 150 - ts) / 60))   # the middle of the candle that held the high
    vol = round(sum(c[5] for c in after if len(c) > 5)) if after else None
    return {"best": o["best"], "peak_min": peak_min, "now": o["now"], "vol": vol,
            "witness": "candles" if candles else ("tape" if o["best"] is not None else "none")}


def recipients(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """The chats that got the push, still subscribed."""
    key = f"hot:{row['mint']}" if row["kind"] == "burst" else row["mint"]
    return [r["chat_id"] for r in conn.execute(
        "SELECT DISTINCT s.chat_id FROM bot_sent s JOIN bot_subscribers b ON b.chat_id = s.chat_id "
        "WHERE s.mint = ? AND s.ts BETWEEN ? AND ? AND b.active = 1", (key, row["ts"] - 120, row["ts"] + 3600))]


def close(conn: sqlite3.Connection, row_id: int, m: dict, now: int | None = None) -> None:
    now = now or db.now()
    with db.tx(conn):
        conn.execute("UPDATE pushes SET followup_at=?, best=?, peak_min=?, now_x=?, vol_usd=? WHERE id=?",
                     (now, m.get("best"), m.get("peak_min"), m.get("now"), m.get("vol"), row_id))


def fmt_time(ts: int) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))
