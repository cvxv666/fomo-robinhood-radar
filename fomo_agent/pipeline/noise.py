"""Addresses that are not traders, found by the one thing a trader cannot do: trade everything.

On 2026-09-13 wallet resolution attributed a fomo profile to 0x8f10b4…, and the tape went from
four thousand fills a day to three hundred thousand. The address traded 5,315 tokens in a day -
a router, an aggregator, an arbitrage bot; whatever it is, it is in every window of every token,
so the intersection that resolves a trader hands every unresolved profile to it, and its fills
put a mispriced row into every token's tape, which read as 700x on the scorecard.

So: a wallet with more fills in an hour than a person could place is quarantined - dropped,
flagged, its fills removed, its address remembered so resolution never picks it again. The
watcher runs this every tick, on the hour it has just written.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .. import db
from ..config import settings

log = logging.getLogger(__name__)


def hyperactive(conn: sqlite3.Connection, now: int | None = None, per_hour: int | None = None,
                window_s: int = 3600) -> list[dict]:
    """Tracked addresses with more fills in the window than the ceiling allows."""
    now = now or db.now()
    per_hour = per_hour or settings.noise_fills_per_hour
    ceiling = per_hour * window_s / 3600
    return [dict(r) for r in conn.execute(
        "SELECT tr.address, COUNT(*) n, COUNT(DISTINCT tr.mint) tokens, t.fomo_handle handle, t.status "
        "FROM trades tr LEFT JOIN traders t ON t.address = tr.address "
        "WHERE tr.ts >= ? GROUP BY tr.address HAVING n > ?", (now - window_s, ceiling))]


def quarantine(conn: sqlite3.Connection, address: str, reason: str) -> int:
    """Drop the trader, flag it, forget its fills, remember the address. Returns fills removed."""
    address = address.lower()
    with db.tx(conn):
        row = db.get_trader(conn, address)
        if row is not None:
            try:
                tags = json.loads(row["tags"] or "{}")
            except ValueError:
                tags = {}
            flags = list(tags.get("red_flags") or [])
            if "bot" not in flags:
                flags.append("bot")
            tags["red_flags"] = flags
            conn.execute("UPDATE traders SET status='dropped', tags=?, ai_summary=? WHERE address=?",
                         (json.dumps(tags), f"Not a trader: {reason}. " + (row["ai_summary"] or ""), address))
        conn.execute("INSERT INTO noise_addresses(address, reason, ts) VALUES(?,?,?) "
                     "ON CONFLICT(address) DO UPDATE SET reason=excluded.reason, ts=excluded.ts",
                     (address, reason, db.now()))
        # a profile that resolved to it is unresolved again, and says why
        conn.execute("UPDATE fomo_users SET onchain_address=NULL, onchain_note=? WHERE onchain_address=?",
                     (f"resolved to noise {address}: {reason}", address))
        removed = conn.execute("DELETE FROM trades WHERE address=?", (address,)).rowcount
        conn.execute("DELETE FROM holdings WHERE address=?", (address,))
    log.warning("noise: %s quarantined (%s), %d fills removed", address[:10], reason, removed)
    return removed


def sweep(conn: sqlite3.Connection, now: int | None = None) -> list[dict]:
    """Find and quarantine, in one call. What the watcher runs each tick."""
    out = []
    for h in hyperactive(conn, now):
        reason = f"{h['n']} fills across {h['tokens']} tokens in an hour"
        h["removed"] = quarantine(conn, h["address"], reason)
        out.append(h)
    return out
