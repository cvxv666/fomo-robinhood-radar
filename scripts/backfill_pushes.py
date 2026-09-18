"""Write the alerts sent before the push ledger existed (11-17 Sep 2026) into it, measured the
same way the follow-ups measure: entry = last trusted fill before the message, the hour's peak and
close from the pool's five-minute candles, volume since. Run once, on the server, as radar:

    cd /opt/fomoradar/app && sudo -u radar bash -c 'set -a; . ./.env; set +a; \\
        /opt/fomoradar/venv/bin/python scripts/backfill_pushes.py'

Idempotent: a (kind, mint, minute) already in the ledger is skipped. The rows are closed
(`followup_at` set) so the bot never sends a follow-up for an alert from last week.
"""
from __future__ import annotations

import logging
import sys
import time

from fomo_agent import db
from fomo_agent.pipeline.hot import outcome
from fomo_agent.pipeline.pushes import last_trusted_px
from fomo_agent.sources.geckoterminal import GeckoTerminal

logging.basicConfig(level=logging.WARNING)
conn = db.connect()
now = db.now()
gt = GeckoTerminal()
dry = "--dry" in sys.argv

first_ledger = conn.execute("SELECT MIN(ts) FROM pushes").fetchone()[0] or now
rows = conn.execute(
    "SELECT CASE WHEN mint LIKE 'hot:%' THEN 'burst' ELSE 'launch' END kind, REPLACE(mint, 'hot:', '') m, "
    "MIN(ts) ts, COUNT(DISTINCT chat_id) chats FROM bot_sent WHERE ts < ? GROUP BY kind, m ORDER BY ts", (first_ledger,)).fetchall()
print(f"{len(rows)} alerts before the ledger ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(first_ledger))})")
done = skipped = unmeasured = 0
for r in rows:
    kind, mint, ts, chats = r["kind"], r["m"], r["ts"], r["chats"]
    if conn.execute("SELECT 1 FROM pushes WHERE kind=? AND mint=? AND ABS(ts - ?) < 600", (kind, mint, ts)).fetchone():
        skipped += 1
        continue
    b = conn.execute("SELECT px, conviction, wallets, usd FROM bursts WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
                     (mint, ts - 900, ts + 120)).fetchone() if kind == "burst" else None
    px = (b["px"] if b and b["px"] else None) or last_trusted_px(conn, mint, ts)
    tk = conn.execute("SELECT symbol, pool_address, chain, liquidity_usd FROM tokens WHERE mint=?", (mint,)).fetchone()
    candles = []
    if tk and tk["pool_address"]:
        try:
            candles = gt.ohlcv(tk["chain"] or "robinhood", tk["pool_address"], "minute", 5, 40, before_ts=ts + 3900) or []
        except Exception as e:  # noqa: BLE001
            print("  candles", mint[:10], e)
    m = {"best": None, "peak_min": None, "now": None, "vol": None}
    if px:
        o = outcome(conn, mint, ts, px, 3600, ts + 3600, candles)
        after = [c for c in candles if ts - 300 <= c[0] <= ts + 3600]
        peak_min = None
        if after and o["best"] is not None:
            hi = max(after, key=lambda c: c[2])
            peak_min = max(0, round((hi[0] + 150 - ts) / 60))
        m = {"best": o["best"], "peak_min": peak_min, "now": o["now"], "vol": round(sum(c[5] for c in after if len(c) > 5)) if after else None}
    if m["best"] is None:
        unmeasured += 1
    sym = (tk["symbol"] if tk else None) or mint[:8]
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts))} {kind:6s} {sym:12s} chats={chats} px={px} -> {m}")
    if not dry:
        with db.tx(conn):
            conn.execute(
                "INSERT INTO pushes(kind, mint, ts, px, conviction, wallets, liq, chats, followup_due, followup_at, best, peak_min, now_x, vol_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, mint, ts, px, b["conviction"] if b else None, b["wallets"] if b else None,
                 tk["liquidity_usd"] if tk else None, chats, ts + 3600, ts + 3600, m["best"], m["peak_min"], m["now"], m["vol"]))
    done += 1
print(f"done {done}, skipped {skipped}, unmeasured {unmeasured}, gecko requests {gt.requests if hasattr(gt, 'requests') else '?'}")
