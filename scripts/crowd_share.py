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
    try:
        c = gt.ohlcv(tk["chain"] or "robinhood", tk["pool_address"], "minute", 1, 60, before_ts=r["ts"] + 60) or []
    except Exception as e:  # noqa: BLE001
        print("  candles", r["sym"], e)
        continue
    pool = sum(x[5] for x in c if len(x) > 5 and r["ts"] - WINDOW <= x[0] <= r["ts"])
    if pool <= 0:
        continue
    share = min(1.0, cohort["usd"] / pool)
    out.append({**r, "cohort_usd": cohort["usd"], "pool_usd": pool, "share": share})
    time.sleep(0.2)
print(f"{len(out)} with the pool's half hour on record\n")

BUCKETS = [(0.0, 0.1, "under 10%"), (0.1, 0.33, "10-33%"), (0.33, 0.66, "33-66%"), (0.66, 1.01, "66-100%")]


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
