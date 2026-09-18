"""Measure again the ledger rows the follow-up could not: the ones with no candles at the time.

A row measured by the tape alone carries a `now_x` that is the stored quote against the entry
- today's price, not the hour's close - and a `best` that a single mispriced fill can set. This
asks the screener for the pool's candles around the alert once more (the pool is often known by
now) and rewrites the row from them; where there are still none, `now_x` and `peak_min` are
cleared so the record does not pass a quote off as an hour read.

    cd /opt/fomoradar/app && sudo -u radar bash -c 'set -a; . ./.env; set +a; /opt/fomoradar/venv/bin/python scripts/remeasure_pushes.py'
"""
from __future__ import annotations

import time

from fomo_agent import db
from fomo_agent.pipeline.hot import outcome
from fomo_agent.sources.geckoterminal import GeckoTerminal

conn = db.connect()
gt = GeckoTerminal()
rows = conn.execute(
    "SELECT p.*, tk.pool_address, tk.chain, COALESCE(tk.symbol, substr(p.mint,1,8)) sym FROM pushes p LEFT JOIN tokens tk ON tk.mint = p.mint "
    "WHERE p.followup_at IS NOT NULL AND p.vol_usd IS NULL ORDER BY p.ts").fetchall()
print(f"{len(rows)} rows measured by the tape alone")
fixed = cleared = 0
for r in rows:
    candles = []
    if r["pool_address"]:
        try:
            candles = gt.ohlcv(r["chain"] or "robinhood", r["pool_address"], "minute", 5, 40, before_ts=r["ts"] + 3900) or []
        except Exception as e:  # noqa: BLE001
            print("  candles", r["sym"], e)
    after = [c for c in candles if r["ts"] - 300 <= c[0] <= r["ts"] + 3600]
    if after and r["px"]:
        o = outcome(conn, r["mint"], r["ts"], r["px"], 3600, r["ts"] + 3600, candles)
        hi = max(after, key=lambda c: c[2])
        peak_min = max(0, round((hi[0] + 150 - r["ts"]) / 60)) if o["best"] is not None else None
        vol = round(sum(c[5] for c in after if len(c) > 5))
        with db.tx(conn):
            conn.execute("UPDATE pushes SET best=?, peak_min=?, now_x=?, vol_usd=? WHERE id=?", (o["best"], peak_min, o["now"], vol, r["id"]))
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} {r['kind']:6s} {r['sym']:10s} best {r['best']} -> {o['best']}  hour {r['now_x']} -> {o['now']}  vol ${vol:,}")
        fixed += 1
    else:
        with db.tx(conn):
            conn.execute("UPDATE pushes SET now_x=NULL, peak_min=NULL WHERE id=?", (r["id"],))
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} {r['kind']:6s} {r['sym']:10s} no candles: hour read cleared, tape best {r['best']} kept")
        cleared += 1
print(f"re-measured {fixed}, cleared {cleared}")
