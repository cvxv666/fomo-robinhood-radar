"""Paper trading over every push the bot sent: what simple exit rules would have made of them.

Entry is the close of the five-minute candle that holds the push (the reader needed a minute),
$100 a push, 1.5% round trip for fees and slippage. Exits are the obvious ones - a fixed hold,
a take-profit with a stop, a trailing stop, and just holding a day - and each is scored on the
same pushes: hit rate, average and median return, total P&L, worst run. Candles come from the
token's real pool and are cached beside the daily reports, so a rerun costs nothing.

    HOURS=240 python scripts/paper.py > paper.jsonl
"""
import json, os, pathlib, sys, time
sys.path.insert(0, "/opt/fomoradar/app")
from fomo_agent import db  # noqa: E402
from fomo_agent.config import settings  # noqa: E402
from fomo_agent.pipeline.new_tokens import lookup_tokens  # noqa: E402
from fomo_agent.sources.geckoterminal import GeckoTerminal  # noqa: E402

CACHE = pathlib.Path(os.environ.get("CANDLE_CACHE", "/opt/fomoradar/daily/candles"))
CACHE.mkdir(parents=True, exist_ok=True)
FEE = 0.015
STAKE = 100.0
SINCE = int(os.environ.get("SINCE", "1789171200"))   # 12 Sep 2026 00:00 UTC: the first day with an audience

conn = db.connect(settings.db_path)
now = db.now()
gt = GeckoTerminal()


def candles(pool: str, ts: int) -> list:
    f = CACHE / f"{pool}_{ts // 3600}.json"
    if f.exists():
        return json.loads(f.read_text())
    c = gt.ohlcv("robinhood", pool, "minute", 5, 300, before_ts=ts + 26 * 3600) or []
    if c and c[0][0] > ts - 3600:
        c = (gt.ohlcv("robinhood", pool, "minute", 5, 300, before_ts=c[0][0]) or []) + c
    if c:
        f.write_text(json.dumps(c))
    return c


def simulate(cs: list, ts: int, px: float) -> dict | None:
    """Every rule on one push. Prices are the candles' own after they are brought to the fill's unit."""
    hold = [c for c in cs if c[0] >= ts - 300]
    if not hold or not px:
        return None
    first = hold[0]
    ratio = first[1] / px
    u = next((f for f in (1.0, 1e3, 1e-3, 1e6, 1e-6) if 0.2 <= ratio / f <= 5), None)
    if u is None:
        return None
    hold = [[c[0], c[1] / u, c[2] / u, c[3] / u, c[4] / u, c[5] if len(c) > 5 else 0] for c in hold]
    entry = hold[0][4]
    if entry <= 0:
        return None
    later = hold[1:]
    if not later:
        return None
    day = [c for c in later if c[0] <= ts + 86400]
    net = lambda exit_px: (exit_px / entry) * (1 - FEE) - 1  # noqa: E731
    out = {}
    # fixed holds: sell at the close of the candle that ends N minutes on
    for n in (15, 30, 60, 120, 240):
        at = [c for c in day if c[0] >= ts + n * 60]
        out[f"hold_{n}m"] = net(at[0][4]) if at else net(day[-1][4]) if day else None
    out["hold_24h"] = net(day[-1][4]) if day else None
    # take-profit / stop: the first of the two the candles reach, else the day's close
    for tp, sl in ((1.5, 0.5), (2.0, 0.5), (3.0, 0.5), (1.5, 0.7), (2.0, 0.7)):
        r = None
        for c in day:
            if c[3] <= entry * sl:
                r = net(entry * sl); break
            if c[2] >= entry * tp:
                r = net(entry * tp); break
        out[f"tp{tp}_sl{sl}"] = r if r is not None else (net(day[-1][4]) if day else None)
    # trailing: out when a close gives back d of the peak close, else the day's close
    for d in (0.25, 0.35, 0.5):
        peak, r = entry, None
        for c in day:
            peak = max(peak, c[4])
            if c[4] <= peak * (1 - d):
                r = net(c[4]); break
        out[f"trail_{int(d * 100)}"] = r if r is not None else (net(day[-1][4]) if day else None)
    out["_peak"] = max(c[2] for c in day) / entry if day else None
    return out


rows = conn.execute("SELECT mint, MIN(ts) ts FROM bot_sent WHERE ts >= ? GROUP BY mint ORDER BY ts", (SINCE,)).fetchall()
pushes = [{"key": r["mint"], "kind": "burst" if r["mint"].startswith("hot:") else "launch", "mint": r["mint"].split(":", 1)[-1], "ts": r["ts"]} for r in rows]
mints = sorted({p["mint"] for p in pushes})
for i in range(0, len(mints), 30):
    found, _ = lookup_tokens("robinhood", mints[i:i + 30])
    with db.tx(conn):
        for t in found:
            db.upsert_token(conn, t.mint, pool_address=t.pool_address, symbol=t.symbol)
for p in pushes:
    tk = conn.execute("SELECT symbol, pool_address FROM tokens WHERE mint=?", (p["mint"],)).fetchone()
    b = conn.execute("SELECT px FROM bursts WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1", (p["mint"], p["ts"] - 600, p["ts"] + 60)).fetchone()
    px = b["px"] if b and b["px"] else None
    if px is None:
        last = conn.execute("SELECT usd_value / token_amount p FROM trades WHERE mint=? AND ts<=? AND usd_value>=1 AND token_amount>0 AND COALESCE(kind,'trade')='trade' ORDER BY ts DESC LIMIT 1", (p["mint"], p["ts"])).fetchone()
        px = last["p"] if last else None
    rec = {"t": time.strftime("%m-%d %H:%M", time.gmtime(p["ts"])), "kind": p["kind"], "sym": (tk["symbol"] if tk else None) or p["mint"][:8], "mint": p["mint"], "ts": p["ts"], "px": px}
    cs = candles(tk["pool_address"], p["ts"]) if tk and tk["pool_address"] else []
    rec["rules"] = simulate(cs, p["ts"], px) if cs and px else None
    print(json.dumps(rec), flush=True)
