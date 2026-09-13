"""One message a day: what the cohort did while you were not looking.

Everything else this product publishes waits to be asked. The signal feed is worth most in the
minutes after a launch and the bot already pushes those, but a day has a shape the individual
alerts cannot show — that the same three names keep appearing, that the wallets which bought on
Monday were gone by Tuesday, that nothing happened at all.

Deliberately one message. A digest people scroll past is worse than no digest, so it carries the
five things that change a decision and nothing else: what came in, what went out, what launched,
who joined the roster, and whether the machine collecting all of it is still running.
"""
from __future__ import annotations

import sqlite3

from .. import db
from . import analyze
from .health import report as health_report


def daily(conn: sqlite3.Connection, hours: int = 24, chain: str | None = None) -> dict:
    since = db.now() - hours * 3600
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM traders WHERE score IS NOT NULL GROUP BY status")}

    # Wallets that got their first verdict in the window — the roster growing, not being rescored.
    joined = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, t.status FROM traders t "
        "WHERE t.address IN ("
        "  SELECT address FROM score_history GROUP BY address HAVING MIN(ts) >= ?) "
        "AND t.score IS NOT NULL ORDER BY t.score DESC LIMIT 5", (since,))]
    joined_n = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT address FROM score_history GROUP BY address "
        "HAVING MIN(ts) >= ?)", (since,)).fetchone()["c"]

    theses = [dict(r) for r in conn.execute(
        "SELECT th.text, th.mint, COALESCE(tk.symbol, substr(th.mint,1,8)) sym, u.handle, t.score "
        "FROM theses th JOIN fomo_users u ON u.user_id = th.user_id "
        "LEFT JOIN traders t ON t.address = u.onchain_address "
        "LEFT JOIN tokens tk ON tk.mint = th.mint "
        "WHERE th.first_seen_at >= ? AND t.score >= ? "
        "ORDER BY t.score DESC LIMIT 3", (since, analyze.TRUSTED))]

    # The bursts the watcher wrote down, and what each did since. Only one older than a few
    # hours has had time to do anything, so the scorecard counts those and says how many.
    from .hot import recent

    bursts = recent(conn, chain, hours=hours, candles_for=_pool_candles(conn))
    settled = [b for b in bursts if b["age_at_read_h"] >= 3 and b["best"] is not None
               and not b.get("seeded") and not b.get("unsellable")]
    best = sorted(b["best"] for b in settled)
    scorecard = {
        "n": len(bursts), "measured": len(settled), "seeded": sum(1 for b in bursts if b.get("seeded")),
        "reached_2x": sum(b["best"] >= 2 for b in settled),
        "median_best": best[len(best) // 2] if best else None,
        "below_half": sum((b["last"] or 0) < 0.5 for b in settled),
        "top": sorted(settled, key=lambda b: -b["best"])[:3],
    }

    return {
        "hours": hours,
        "counts": counts,
        "joined": joined,
        "joined_n": joined_n,
        "bursts": scorecard,
        "signals": analyze.signals(conn, chain, hours=hours, limit=3),
        "fresh": (analyze.fresh(conn, chain, hours=hours, limit=3) or {}).get("tokens", [])[:3],
        "exits": analyze.exits(conn, chain, hours=hours, min_sellers=2, limit=3),
        "theses": theses,
        "health": health_report(conn),
    }


def _pool_candles(conn: sqlite3.Connection):
    """One day's hourly candles per burst, from GeckoTerminal. Once a day, a handful of requests,
    and a pool it cannot answer for simply leaves the tape as the only witness."""
    def candles(mint: str):
        row = conn.execute("SELECT pool_address, chain FROM tokens WHERE mint=?", (mint,)).fetchone()
        if not row or not row["pool_address"]:
            return None
        try:
            from ..sources.geckoterminal import GeckoTerminal
            return GeckoTerminal().ohlcv(row["chain"] or "robinhood", row["pool_address"],
                                         "hour", 1, 24) or None
        except Exception:  # noqa: BLE001 - the tape still answers
            return None
    return candles


def is_quiet(d: dict) -> bool:
    """Nothing moved. Worth saying in one line rather than dressing up as a report."""
    return not (d["signals"] or d["fresh"] or d["exits"] or d["joined_n"] or d["theses"]
                or d.get("bursts", {}).get("n"))
