"""Paper run, second pass on the candles scripts/paper.py cached: the entry is the variable.

`push` is the cohort's own price (the burst's px, or the last trusted fill), `open` the push
candle's open (a bound: as early as the cohort), `close` its close (a person reading Telegram,
a minute or two on), `dipN` a pullback of N% from that close inside half an hour, or no trade.
Run after scripts/paper.py; reads /tmp/paper.jsonl and the candle cache.

Found 17 Sep over 67 pushes since 12 Sep: at the cohort's price the bursts pay (hold 2h +$2,088
on 17 x $100, trailing 25% +$1,429, and still positive without the single best ticket); at the
candle's close - where a reader actually is - every rule on launches loses and the bursts only
pay through one ticket. The edge is in the first minutes, which is the case for an engine.
"""
import json, pathlib, sys, statistics
sys.path.insert(0, "/opt/fomoradar/app")
rows = [json.loads(l) for l in open("/tmp/paper.jsonl") if l.strip()]
CACHE = pathlib.Path("/opt/fomoradar/daily/candles")
FEE = 0.015
from fomo_agent import db
from fomo_agent.config import settings
conn = db.connect(settings.db_path)

def load(mint, ts):
    tk = conn.execute("SELECT pool_address FROM tokens WHERE mint=?", (mint,)).fetchone()
    if not tk or not tk["pool_address"]: return []
    f = CACHE / f"{tk['pool_address']}_{ts // 3600}.json"
    return json.loads(f.read_text()) if f.exists() else []

def run(cs, ts, px, entry_mode):
    hold = [c for c in cs if c[0] >= ts - 300]
    if not hold or not px: return None
    ratio = hold[0][1] / px
    u = next((f for f in (1.0, 1e3, 1e-3, 1e6, 1e-6) if 0.2 <= ratio / f <= 5), None)
    if u is None: return None
    hold = [[c[0], c[1]/u, c[2]/u, c[3]/u, c[4]/u, c[5] if len(c) > 5 else 0] for c in hold]
    day = [c for c in hold[1:] if c[0] <= ts + 86400]
    if not day: return None
    if entry_mode == "close": entry, start = hold[0][4], day
    elif entry_mode == "push": entry, start = px, day
    elif entry_mode == "open": entry, start = hold[0][1], day
    elif entry_mode.startswith("dip"):   # wait for a pullback of d% from the push candle close, inside 30 min
        d = float(entry_mode[3:]) / 100; lvl = hold[0][4] * (1 - d); entry = None
        for i, c in enumerate(day[:6]):
            if c[3] <= lvl: entry, start = lvl, day[i + 1:]; break
        if entry is None: return None
    if entry <= 0 or not start: return None
    net = lambda p: (p / entry) * (1 - FEE) - 1
    out = {}
    for n in (30, 60, 120, 240):
        at = [c for c in start if c[0] >= ts + n * 60]
        out[f"hold_{n}m"] = net(at[0][4]) if at else net(start[-1][4])
    out["hold_24h"] = net(start[-1][4])
    for tp, sl in ((1.5, 0.7), (2.0, 0.7), (2.0, 0.5), (1.3, 0.8)):
        r = None
        for c in start:
            if c[3] <= entry * sl: r = net(entry * sl); break
            if c[2] >= entry * tp: r = net(entry * tp); break
        out[f"tp{tp}_sl{sl}"] = r if r is not None else net(start[-1][4])
    for d in (0.25, 0.35):
        peak, r = entry, None
        for c in start:
            peak = max(peak, c[4])
            if c[4] <= peak * (1 - d): r = net(c[4]); break
        out[f"trail_{int(d*100)}"] = r if r is not None else net(start[-1][4])
    return out

for mode in ("push", "open", "close", "dip15", "dip25"):
    res = []
    for r in rows:
        cs = load(r["mint"], r["ts"])
        o = run(cs, r["ts"], r["px"], mode) if cs and r["px"] else None
        if o: res.append((r, o))
    print(f"\n== entry = {mode}: n={len(res)}")
    keys = list(res[0][1].keys()) if res else []
    for k in keys:
        for label, sel in (("all", res), ("burst", [x for x in res if x[0]["kind"] == "burst"]), ("launch", [x for x in res if x[0]["kind"] == "launch"])):
            v = [o[k] for _, o in sel]
            if not v: continue
            srt = sorted(v)
            trimmed = srt[:-1] if len(srt) > 3 else srt   # without the single best: how much is one ticket
            print(f"  {k:<12} {label:<6} n={len(v):>2} win {sum(x>0 for x in v)/len(v)*100:>3.0f}%  avg {statistics.mean(v)*100:>7.1f}%  med {statistics.median(v)*100:>6.1f}%  total ${sum(v)*100:>6.0f}  without best ${sum(trimmed)*100:>6.0f}")
