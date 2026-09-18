"""The record: every alert the bot ever sent, and what came of each, as one page that never ages.

The week-one board said 82% of calls traded above the entry. A board is a photograph; this is
the window. It reads the push ledger (`pushes`: what went out, at what price, to how many chats,
and the hour-later read the same chats got) and adds the one thing the ledger cannot hold, where
the price is now. Nothing on it is recomputed to look better: the entry is the last trusted fill
before the alert, the peak is the pool's own candles inside the hour, and a token later found to
be seeded or unsellable stays on the list with that word on it, because the alert did go out.

The paper run is the same rows read as trades: a hundred dollars into every alert at the entry,
out at the hour read. Not a strategy - the hour is where the follow-up happens to look - but a
number anyone can check against the messages in their own chat.
"""
from __future__ import annotations

import sqlite3

from .. import db
from ..config import settings

STAKE = 100.0
DEAD_USD = 2000.0          # a pool that traded less than this in the hour after the push had nobody in it


def verdict(r: dict, now: int) -> str:
    if r["unsellable"]:
        return "honeypot"
    if r["seeded"]:
        return "seeded"
    if r["followup_at"] is None:
        return "open" if now - r["ts"] < settings.telegram_followup_min * 60 + 1800 else "unmeasured"
    if r["best"] is None:
        return "unmeasured"
    # no volume figure is not a dead pool, it is a read the candles did not cover
    if r["vol_usd"] is not None and r["vol_usd"] < DEAD_USD:
        return "dead"
    if r["best"] >= 2:
        return "2x"
    return "above" if r["best"] > 1.0 else "below"


def rows(conn: sqlite3.Connection, days: int = 30, kind: str | None = None, now: int | None = None) -> list[dict]:
    now = now or db.now()
    out = []
    for r in conn.execute(
            "SELECT p.*, COALESCE(tk.symbol, substr(p.mint, 1, 8)) sym, tk.price_usd, tk.sellable, "
            "  EXISTS(SELECT 1 FROM trades s WHERE s.mint = p.mint AND s.kind = 'seed') seeded "
            "FROM pushes p LEFT JOIN tokens tk ON tk.mint = p.mint "
            "WHERE p.ts >= ?" + (" AND p.kind = ?" if kind else "") + " ORDER BY p.ts DESC",
            [now - days * 86400, *([kind] if kind else [])]):
        d = {
            "id": r["id"], "ts": r["ts"], "kind": r["kind"], "mint": r["mint"], "sym": r["sym"],
            "px": r["px"], "wallets": r["wallets"], "conviction": r["conviction"], "heat": r["heat"],
            "liq": r["liq"], "chats": r["chats"], "followup_at": r["followup_at"],
            "best": r["best"], "peak_min": r["peak_min"], "hour": r["now_x"], "vol_usd": r["vol_usd"], "trail": r["trail_x"],
            # where it sits now, in the same entry price: the stored quote against the entry
            "now": (r["price_usd"] / r["px"]) if r["price_usd"] and r["px"] else None,
            "seeded": bool(r["seeded"]), "unsellable": r["sellable"] == 0,
        }
        d["verdict"] = verdict(d, now)
        # a honeypot's candles can print anything; the stake is gone the moment it is bought
        closed = d["verdict"] not in ("open", "unmeasured")
        d["paper"] = (-STAKE if d["verdict"] == "honeypot" else round(STAKE * (d["hour"] - 1), 2)) \
            if (d["hour"] is not None or d["verdict"] == "honeypot") and closed else None
        d["paper_trail"] = (-STAKE if d["verdict"] == "honeypot" else round(STAKE * (d["trail"] - 1), 2)) \
            if (d["trail"] is not None or d["verdict"] == "honeypot") and closed else None
        out.append(d)
    return out


def totals(rs: list[dict]) -> dict:
    measured = [r for r in rs if r["best"] is not None and r["verdict"] not in ("open", "unmeasured")]
    # a dead pool's 1.02x on $995 is not a price anybody could have taken: it is listed, it
    # counts in the paper run, and it is out of the "above entry" share
    clean = [r for r in measured if r["verdict"] not in ("seeded", "honeypot", "dead")]
    bests = sorted(r["best"] for r in clean)
    paper = [r for r in rs if r["paper"] is not None]
    wins = [r for r in paper if r["paper"] > 0]
    trail = [r for r in rs if r["paper_trail"] is not None]
    twins = [r for r in trail if r["paper_trail"] > 0]
    return {
        "pushes": len(rs), "bursts": sum(r["kind"] == "burst" for r in rs), "launches": sum(r["kind"] == "launch" for r in rs),
        "measured": len(measured), "clean": len(clean),
        "above_entry": sum(r["best"] > 1.0 for r in clean), "reached_2x": sum(r["best"] >= 2 for r in clean),
        "median_best": bests[len(bests) // 2] if bests else None,
        "seeded": sum(r["verdict"] == "seeded" for r in rs), "dead": sum(r["verdict"] == "dead" for r in rs),
        "honeypot": sum(r["verdict"] == "honeypot" for r in rs),
        "paper": {
            "stake": STAKE, "trades": len(paper), "staked": STAKE * len(paper),
            "pnl": round(sum(r["paper"] for r in paper), 2), "wins": len(wins),
            "win_rate": round(len(wins) / len(paper), 3) if paper else None,
            "best": max(paper, key=lambda r: r["paper"])["paper"] if paper else None,
            "worst": min(paper, key=lambda r: r["paper"])["paper"] if paper else None,
        },
        # the same alerts, out at the first pullback under the running high instead of at the hour
        "paper_trail": {
            "stake": STAKE, "drop": settings.trail_drop, "trades": len(trail), "staked": STAKE * len(trail),
            "pnl": round(sum(r["paper_trail"] for r in trail), 2), "wins": len(twins),
            "win_rate": round(len(twins) / len(trail), 3) if trail else None,
            "best": max(trail, key=lambda r: r["paper_trail"])["paper_trail"] if trail else None,
            "worst": min(trail, key=lambda r: r["paper_trail"])["paper_trail"] if trail else None,
        },
    }


def curve(rs: list[dict], key: str = "paper") -> list[dict]:
    """A paper run as an equity curve, oldest first: cumulative P&L after each closed trade."""
    total = 0.0
    out = []
    for r in sorted((r for r in rs if r[key] is not None), key=lambda r: r["ts"]):
        total += r[key]
        out.append({"ts": r["ts"], "sym": r["sym"], "mint": r["mint"], "kind": r["kind"], "pnl": r[key], "total": round(total, 2)})
    return out


def report(conn: sqlite3.Connection, days: int = 30, kind: str | None = None, now: int | None = None) -> dict:
    rs = rows(conn, days, kind, now)
    return {"days": days, "kind": kind or "all", "stake": STAKE, "followup_min": settings.telegram_followup_min,
            "trail_drop": settings.trail_drop, "totals": totals(rs), "curve": curve(rs), "curve_trail": curve(rs, "paper_trail"), "pushes": rs}
