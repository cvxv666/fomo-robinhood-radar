"""Crews: wallets that buy the same tokens in the same minute, again and again.

Six wallets entering one token inside a quarter of an hour is a burst when they are six
people. When they are six keys following one call - a copy-bot's flock, or one person with six
wallets - it is one opinion counted six times. So the tape is read for pairs: two trusted wallets
whose first buys of the same token landed within `crew_gap_s` of each other on at least
`crew_min_shared` tokens over the window, and on at least half of what the smaller of them
bought at all. Those pairs, joined, are the crews; a burst then needs `hot_min_crews` of them, and
the token page names them. Only real fills count, so a seeding wave does not make a crew of its
targets.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

from .. import db
from ..config import settings
from .analyze import TRUSTED

log = logging.getLogger(__name__)


def pairs(conn: sqlite3.Connection, days: int, gap_s: int, min_shared: int, min_share: float) -> list[tuple[str, str, int]]:
    since = db.now() - days * 86400
    rows = conn.execute(
        "SELECT tr.mint, tr.address, MIN(tr.ts) ts FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.side = 'buy' AND tr.ts >= ? AND t.score >= ? AND COALESCE(tr.kind, 'trade') = 'trade' "
        "GROUP BY tr.mint, tr.address", (since, TRUSTED)).fetchall()
    by_mint: dict[str, list[tuple[int, str]]] = defaultdict(list)
    per_wallet: dict[str, int] = defaultdict(int)
    for r in rows:
        by_mint[r["mint"]].append((r["ts"], r["address"]))
        per_wallet[r["address"]] += 1
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for entries in by_mint.values():
        entries.sort()
        for i, (t0, a) in enumerate(entries):
            for t1, b in entries[i + 1:]:
                if t1 - t0 > gap_s:
                    break
                shared[(a, b) if a < b else (b, a)] += 1
    out = []
    for (a, b), n in shared.items():
        if n >= min_shared and n >= min_share * min(per_wallet[a], per_wallet[b]):
            out.append((a, b, n))
    return out


def compute(conn: sqlite3.Connection, days: int | None = None, now: int | None = None) -> dict:
    """The crews of the window, written to `crews` (a wallet in no crew has no row)."""
    now = now or db.now()
    days = days or settings.crew_days
    ps = pairs(conn, days, settings.crew_gap_s, settings.crew_min_shared, settings.crew_min_share)
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in ps:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[rb] = ra
    groups: dict[str, list[str]] = defaultdict(list)
    for w in parent:
        groups[root(w)].append(w)
    with db.tx(conn):
        conn.execute("DELETE FROM crews")
        n = 0
        for i, (_, members) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1):
            for w in members:
                conn.execute("INSERT INTO crews(address, crew, size, updated_at) VALUES(?,?,?,?)", (w, i, len(members), now))
                n += 1
    stats = {"pairs": len(ps), "crews": len(groups), "wallets": n, "largest": max((len(m) for m in groups.values()), default=0)}
    log.info("crews: %s", stats)
    return stats


def of(conn: sqlite3.Connection, addresses: list[str]) -> dict[str, int]:
    """address -> crew number, for the ones in a crew."""
    if not addresses:
        return {}
    marks = ",".join("?" * len(addresses))
    return {r["address"]: r["crew"] for r in conn.execute(f"SELECT address, crew FROM crews WHERE address IN ({marks})", addresses)}


def distinct(conn: sqlite3.Connection, addresses: list[str]) -> int:
    """How many independent opinions these wallets are: each crew counts once, a loner once."""
    crew = of(conn, addresses)
    return len({crew.get(a, f"solo:{a}") for a in addresses})
