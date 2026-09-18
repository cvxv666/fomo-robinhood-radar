"""Which way out would have paid, over every alert on the ledger.

Reads the push ledger, fetches each pool's five-minute candles for the hours after the alert,
and closes a hundred-dollar paper position under each rule in turn: hold N minutes and sell at
the close; take profit at a multiple with a stop under the entry (a candle that touches both is
counted as the stop, the unkind reading); a trailing stop off the running high. Seeded and
unsellable tokens are left out - nobody could have traded them - dead pools too.

    HOURS=4 /opt/fomoradar/venv/bin/python scripts/exit_rule.py [--days 30]

The number that matters is the total over all alerts, not the best trade; a rule that wins on
one ×25 and loses the rest is the rule the record already shows.
"""
from __future__ import annotations

import os
import sys
import time

from fomo_agent import db
from fomo_agent.pipeline import record
from fomo_agent.sources.geckoterminal import GeckoTerminal

HOURS = int(os.environ.get("HOURS", "4"))
days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30
STAKE = 100.0

conn = db.connect()
gt = GeckoTerminal()
now = db.now()
rows = [r for r in record.rows(conn, days=days, now=now) if r["px"] and r["verdict"] in ("2x", "above", "below")
        and now - r["ts"] >= HOURS * 3600]
print(f"{len(rows)} alerts with an entry and {HOURS}h of history, {days} days")

paths: list[tuple[dict, list[list[float]]]] = []
for r in rows:
    tk = conn.execute("SELECT pool_address, chain FROM tokens WHERE mint=?", (r["mint"],)).fetchone()
    if not tk or not tk["pool_address"]:
        continue
    try:
        c = gt.ohlcv(tk["chain"] or "robinhood", tk["pool_address"], "minute", 5, min(1000, HOURS * 12 + 6), before_ts=r["ts"] + HOURS * 3600 + 300) or []
    except Exception as e:  # noqa: BLE001
        print("  candles", r["sym"], e)
        continue
    after = [x for x in c if r["ts"] - 300 <= x[0] <= r["ts"] + HOURS * 3600]
    if len(after) < 3:
        continue
    # in multiples of the entry, and only when the first candle agrees with the entry to a power of ten
    scale = 1.0
    first_close = after[0][4]
    ratio = first_close / r["px"]
    for k in range(-12, 13):
        if 0.5 <= ratio / (10 ** k) <= 2.0:
            scale = 10 ** k
            break
    else:
        continue
    px = r["px"] * scale
    paths.append((r, [[x[0], x[1] / px, x[2] / px, x[3] / px, x[4] / px] for x in after]))
print(f"{len(paths)} with candles")


def hold(path, minutes):
    end = path[0][0] + minutes * 60
    last = [x for x in path if x[0] <= end]
    return (last[-1][4] if last else path[0][4]) - 1


def take_profit(path, tp, sl):
    for x in path[1:]:
        if x[3] <= sl:
            return sl - 1
        if x[2] >= tp:
            return tp - 1
    return path[-1][4] - 1


def trailing(path, drop):
    high = 1.0
    for x in path[1:]:
        high = max(high, x[2])
        if x[3] <= high * (1 - drop):
            return high * (1 - drop) - 1
    return path[-1][4] - 1


RULES = [
    ("hold 15 min", lambda p: hold(p, 15)), ("hold 30 min", lambda p: hold(p, 30)), ("hold 60 min", lambda p: hold(p, 60)),
    ("hold 120 min", lambda p: hold(p, 120)), (f"hold {HOURS * 60} min", lambda p: hold(p, HOURS * 60)),
    ("tp x1.5 / sl -50%", lambda p: take_profit(p, 1.5, 0.5)), ("tp x2 / sl -50%", lambda p: take_profit(p, 2.0, 0.5)),
    ("tp x3 / sl -50%", lambda p: take_profit(p, 3.0, 0.5)), ("tp x2 / sl -30%", lambda p: take_profit(p, 2.0, 0.7)),
    ("trail -20%", lambda p: trailing(p, 0.2)), ("trail -30%", lambda p: trailing(p, 0.3)), ("trail -40%", lambda p: trailing(p, 0.4)),
]
print(f"\n{'rule':22s} {'n':>3s} {'total':>9s} {'per $100':>9s} {'win%':>5s} {'median':>7s} {'best':>7s} {'worst':>7s}   bursts / launches")
for name, fn in RULES:
    res = [(r, fn(p)) for r, p in paths]
    pnl = [STAKE * v for _, v in res]
    wins = sum(1 for v in pnl if v > 0)
    med = sorted(pnl)[len(pnl) // 2] if pnl else 0
    b = [STAKE * v for r, v in res if r["kind"] == "burst"]
    l = [STAKE * v for r, v in res if r["kind"] == "launch"]
    print(f"{name:22s} {len(pnl):3d} {sum(pnl):+9.0f} {sum(pnl) / len(pnl):+9.1f} {100 * wins / len(pnl):5.0f} {med:+7.0f} {max(pnl):+7.0f} {min(pnl):+7.0f}   {sum(b):+.0f} / {sum(l):+.0f}")
print(f"\ngecko requests: {gt.requests}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))})")
