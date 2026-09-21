"""The cohort against the crowd, over every alert on the ledger: did the pushes where the cohort
was most of the pool's volume do better than the ones where it was a sliver?

For each measured push: the cohort's dollars in the half hour before it (trusted buys on the
tape), the pool's dollars in the same half hour (minute candles), and the share. Then the
outcomes by share bucket - the hour read, the trail read, the peak - each as $100 paper.

    /opt/fomoradar/venv/bin/python scripts/crowd_share.py [--days 30]

The number that decides is the difference between the buckets' totals, not any one alert.
If the low buckets lose and the high ones pay, HOT_MIN_COHORT_SHARE gets the line between them;
if they look alike, the gate stays off and the share stays a fact in the message.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

from fomo_agent import db
from fomo_agent.pipeline import record
from fomo_agent.sources.geckoterminal import GeckoTerminal

days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30
WINDOW = 1800
STAKE = 100.0

conn = db.connect()
gt = GeckoTerminal()
now = db.now()
# the candles once fetched are kept next to the output: a rerun with new buckets costs nothing
CACHE = os.environ.get("CANDLE_CACHE", "/tmp/crowd_candles.json")
try:
    cache = json.load(open(CACHE))
except (OSError, ValueError):
    cache = {}
rows = [r for r in record.rows(conn, days=days, now=now) if r["px"] and r["hour"] is not None
        and r["verdict"] in ("2x", "above", "below", "dead")]
print(f"{len(rows)} measured alerts, {days} days")

out = []
for r in rows:
    cohort = conn.execute(
        "SELECT COALESCE(SUM(tr.usd_value), 0) usd, COUNT(DISTINCT tr.address) n FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint = ? AND tr.side = 'buy' AND COALESCE(tr.kind, 'trade') = 'trade' AND t.score >= 60 AND tr.ts BETWEEN ? AND ?",
        (r["mint"], r["ts"] - WINDOW, r["ts"])).fetchone()
    tk = conn.execute("SELECT pool_address, chain FROM tokens WHERE mint=?", (r["mint"],)).fetchone()
    if not tk or not tk["pool_address"] or not cohort["usd"]:
        continue
    key = str(r["id"])
    c = cache.get(key)
    if c is None:
        try:
            c = gt.ohlcv(tk["chain"] or "robinhood", tk["pool_address"], "minute", 1, 60, before_ts=r["ts"] + 60) or []
        except Exception as e:  # noqa: BLE001
            print("  candles", r["sym"], e, flush=True)
            continue
        if c:
            cache[key] = c
            json.dump(cache, open(CACHE, "w"))
        time.sleep(0.2)
    pool = sum(x[5] for x in c if len(x) > 5 and r["ts"] - WINDOW <= x[0] <= r["ts"])
    if pool <= 0:
        continue
    share = min(1.0, cohort["usd"] / pool)
    out.append({**r, "cohort_usd": cohort["usd"], "pool_usd": pool, "share": share})
print(f"{len(out)} with the pool's half hour on record\n", flush=True)
json.dump(out, open(os.environ.get("ROWS_OUT", "/tmp/crowd_rows.json"), "w"), default=str)

BUCKETS = [(0.0, 0.01, "under 1%"), (0.01, 0.02, "1-2%"), (0.02, 0.03, "2-3%"), (0.03, 0.05, "3-5%"), (0.05, 0.1, "5-10%"),
           (0.1, 0.33, "10-33%"), (0.33, 1.01, "33-100%")]


def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


print(f"{'cohort share':<14}{'n':>4}{'hour $':>10}{'trail $':>10}{'above 1x':>10}{'med peak':>10}{'med hour':>10}{'med share':>11}")
for lo, hi, label in BUCKETS:
    b = [x for x in out if lo <= x["share"] < hi]
    if not b:
        print(f"{label:<14}{0:>4}")
        continue
    hour = sum((x["hour"] - 1) * STAKE for x in b)
    trail = sum((x["trail"] - 1) * STAKE for x in b if x["trail"] is not None)
    above = sum(1 for x in b if x["hour"] > 1) / len(b)
    print(f"{label:<14}{len(b):>4}{money(hour):>10}{money(trail):>10}{above:>10.0%}"
          f"{statistics.median(x['best'] or 0 for x in b):>10.2f}{statistics.median(x['hour'] for x in b):>10.2f}"
          f"{statistics.median(x['share'] for x in b):>11.0%}")
print()
# the floor, if there were one: what a gate at each line would have kept and what it would have cut
print(f"{'floor':<8}{'kept':>6}{'kept $':>10}{'kept >1x':>10}{'cut':>6}{'cut $':>10}{'cut >1x':>9}")
for floor in (0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.1):
    kept = [x for x in out if x["share"] >= floor]
    cut = [x for x in out if x["share"] < floor]
    k_usd = sum((x["hour"] - 1) * STAKE for x in kept)
    c_usd = sum((x["hour"] - 1) * STAKE for x in cut)
    k_up = (sum(1 for x in kept if x["hour"] > 1) / len(kept)) if kept else 0
    c_up = (sum(1 for x in cut if x["hour"] > 1) / len(cut)) if cut else 0
    print(f"{floor:<8.1%}{len(kept):>6}{money(k_usd):>10}{k_up:>10.0%}{len(cut):>6}{money(c_usd):>10}{c_up:>9.0%}")
print()
for kind in ("burst", "launch"):
    b = [x for x in out if x["kind"] == kind]
    if len(b) < 4:
        continue
    b.sort(key=lambda x: x["share"])
    half = len(b) // 2
    low, high = b[:half], b[half:]
    print(f"{kind}s: lower half of shares (median {statistics.median(x['share'] for x in low):.0%}) "
          f"hour {money(sum((x['hour'] - 1) * STAKE for x in low))} · "
          f"upper half (median {statistics.median(x['share'] for x in high):.0%}) "
          f"hour {money(sum((x['hour'] - 1) * STAKE for x in high))}")
print()
print("the ten smallest shares:")
for x in sorted(out, key=lambda x: x["share"])[:10]:
    print(f"  {x['sym']:<10} {x['kind']:<7} share {x['share']:>5.0%}  cohort ${x['cohort_usd']:>9,.0f}  pool ${x['pool_usd']:>11,.0f}  "
          f"peak x{x['best'] or 0:.2f}  hour x{x['hour']:.2f}")
