"""A token that several trusted wallets entered *quickly*.

The signals feed answers "who is the cohort in" over a day. This answers a narrower and more
urgent question: did conviction arrive in a burst? FLYBRAIN on 2026-09-10 gained 4.4 of it in the
hour before its pool opened, from eight wallets, two and a half hours before the run began. A day
window would have shown it too — but so would it show a name three wallets drifted into over
twenty hours, and those are not the same thing.

The rule is a sliding window over first buys: at any moment, the conviction contributed by wallets
whose first buy on the token lands inside the last `window_s` seconds. It fires when that sum
reaches `delta` from at least `min_wallets` wallets. Conviction is the same number the rest of the
project uses — Σ(score/100)² — so a burst of three 80s (1.92) and a burst of three 40s (0.48) are
not confused.

`backtest` replays the rule over the whole tape so the thresholds are chosen against what
actually followed a burst here, not against what sounds right.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .. import db
from ..config import settings
from .analyze import TRUSTED, NOT_QUOTE
from .provenance import NOT_SEEDED, NOT_UNSELLABLE, REAL, seeded_params


@dataclass
class Burst:
    mint: str
    ts: int                     # when the rule fired: the buy that tipped it
    conviction: float           # gained inside the window at that moment
    wallets: int
    usd: float                  # bought inside the window
    px: float | None            # implied price of the tipping fill
    age_s: int | None           # since the earliest fill anybody we track made on it
    who: list[str] = field(default_factory=list)
    scores: list[int] = field(default_factory=list)


# ---------------------------------------------------------------- the rule


def _bursts(buys: list[tuple], delta: float, window_s: int, min_wallets: int,
            first_ts: int | None = None, once: bool = True) -> list[Burst]:
    """Run the window over one token's first buys, oldest first.

    `buys` rows are (ts, address, score, usd, px). Fires at the first moment the condition holds;
    with `once` false it keeps going and reports every distinct crossing, which the backtest wants
    and the live feed does not.
    """
    out: list[Burst] = []
    start = 0
    armed = True
    for i, (ts, _addr, _score, _usd, _px) in enumerate(buys):
        while buys[start][0] < ts - window_s:
            start += 1
        inside = buys[start:i + 1]
        conv = sum((s / 100.0) ** 2 for _, _, s, _, _ in inside)
        if conv >= delta and len(inside) >= min_wallets:
            if armed:
                out.append(Burst(
                    mint="", ts=ts, conviction=round(conv, 2), wallets=len(inside),
                    usd=sum(u or 0 for _, _, _, u, _ in inside), px=buys[i][4],
                    age_s=(ts - first_ts) if first_ts is not None else None,
                    who=[a for _, a, _, _, _ in inside], scores=[s for _, _, s, _, _ in inside]))
                armed = False
                if once:
                    break
        else:
            armed = True   # re-arm once the window has emptied below the bar
    return out


# ---------------------------------------------------------------- live


def hot_now(conn: sqlite3.Connection, chain: str | None = None, delta: float = 3.0,
            window_s: int = 1800, min_wallets: int = 3, max_age_s: int | None = None,
            now: int | None = None) -> list[dict]:
    """Tokens whose trusted first-buys inside the last window add up to a burst, hottest first.

    Uses the scores in force now, which is what a live alert can know. First buys are per wallet
    per token over the whole tape, so a wallet that bought yesterday and again today is not a new
    entrant today.
    """
    now = now or db.now()
    since = now - window_s
    not_quote = NOT_QUOTE.format(col="tr.mint")
    rows = conn.execute(
        "SELECT tr.mint mint, tr.address addr, t.score score, t.fomo_handle handle, "
        "  MIN(tr.ts) first_ts, SUM(tr.usd_value) usd, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,8)) sym, tk.liquidity_usd liq "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"WHERE tr.side='buy' AND t.score >= ?{not_quote}{REAL.format(t='tr')}"
        + (" AND tr.chain=?" if chain else "") + NOT_SEEDED.format(t="tr") + NOT_UNSELLABLE.format(t="tr") +
        " GROUP BY tr.mint, tr.address HAVING first_ts >= ?",
        [TRUSTED, *([chain] if chain else []), *seeded_params(now), since],
    ).fetchall()
    by_mint: dict[str, list] = defaultdict(list)
    meta: dict[str, tuple] = {}
    for r in rows:
        by_mint[r["mint"]].append((r["first_ts"], r["addr"], r["score"], r["usd"] or 0.0, None,
                                   r["handle"]))
        meta[r["mint"]] = (r["sym"], r["liq"])
    out = []
    for mint, entries in by_mint.items():
        entries.sort()
        conv = sum((s / 100.0) ** 2 for _, _, s, _, _, _ in entries)
        if conv < delta or len(entries) < min_wallets:
            continue
        # a cohort takes time to arrive; five wallets inside two minutes is a script (17 Sep)
        if entries[-1][0] - entries[0][0] < settings.hot_min_span_s:
            continue
        # six keys following one call are one opinion: the entrants have to be more than one crew
        from .crews import distinct
        if distinct(conn, [a for _, a, _, _, _, _ in entries]) < settings.hot_min_crews:
            continue
        # and it is an entry only if the tape is not busy leaving: Hound was pushed at 0.33x
        # while twenty tracked wallets sold it
        bought = sum(u for _, _, _, u, _, _ in entries) or 0.0
        sold = conn.execute(
            "SELECT COALESCE(SUM(usd_value), 0) FROM trades WHERE mint=? AND side='sell' AND ts >= ? "
            "AND COALESCE(kind, 'trade') = 'trade'", (mint, since)).fetchone()[0]
        if bought and sold >= settings.hot_max_sell_ratio * bought:
            continue
        first_any = conn.execute("SELECT MIN(ts) FROM trades WHERE mint=?", (mint,)).fetchone()[0]
        age = (now - first_any) if first_any else None
        if max_age_s and age is not None and age > max_age_s:
            continue
        sym, liq = meta[mint]
        # the price the last entrant paid: what a reader of the alert is being told about
        marks = ",".join("?" * len(entries))
        px = conn.execute(
            f"SELECT usd_value / token_amount FROM trades WHERE mint=? AND side='buy' AND ts>=? "
            f"AND address IN ({marks}) AND usd_value > 0 AND token_amount > 0 "
            "ORDER BY ts DESC LIMIT 1",
            (mint, since, *[a for _, a, _, _, _, _ in entries])).fetchone()
        out.append({
            "mint": mint, "sym": sym, "liq": liq,
            "conviction": round(conv, 2), "wallets": len(entries),
            "usd": sum(u for _, _, _, u, _, _ in entries),
            "first_ts": entries[0][0], "last_ts": entries[-1][0],
            "age_s": age, "window_s": window_s, "px": px[0] if px else None,
            "who": [h or a[:8] for _, a, _, _, _, h in entries],
            "scores": [s for _, _, s, _, _, _ in entries],
            "avg_score": sum(s for _, _, s, _, _, _ in entries) / len(entries),
        })
    out.sort(key=lambda h: (-h["conviction"], -h["usd"]))
    return out


# ---------------------------------------------------------------- the record


def record(conn: sqlite3.Connection, h: dict, quiet_s: int, chain: str | None = None) -> bool:
    """Write a burst down once. A token still bursting on the next tick is the same burst."""
    if conn.execute("SELECT 1 FROM bursts WHERE mint=? AND ts>=?",
                    (h["mint"], h["last_ts"] - quiet_s)).fetchone():
        return False
    with db.tx(conn):
        conn.execute(
            "INSERT INTO bursts(mint, chain, ts, conviction, wallets, usd, px, window_s, age_s, who) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (h["mint"], chain, h["last_ts"], h["conviction"], h["wallets"], h["usd"], h.get("px"),
             h["window_s"], h.get("age_s"), json.dumps(list(zip(h["who"], h["scores"])))))
    return True


def outcome(conn: sqlite3.Connection, mint: str, ts: int, px: float | None,
            horizon_s: int = 86400, now: int | None = None,
            candles: list[list[float]] | None = None) -> dict:
    """What the price did after a burst, in the price the cohort itself paid.

    Two witnesses. The tape: every tracked fill inside the horizon, which is exact for the fills
    it has and biased low — a cohort that holds through a run leaves no fill at the top, and the
    first live burst read 1.9x on the tape while the token did 22x. And the pool's own candles,
    when the caller has them and they agree with the fill price: then the high since the burst is
    `best`, and the tape's peak is not consulted, because a single mispriced row can beat it.
    `last` is the tape's most recent fill, `now` the candle close if there is one and the stored
    quote otherwise. None means nothing to measure with, which is not the same as 1.0.
    """
    now = now or db.now()
    if not px:
        return {"best": None, "last": None, "now": None, "fills": 0}
    # trades only, and only ones with a dollar leg worth reading: a dust row or a direct transfer
    # carries a price nobody paid, and one such row is enough to put a hundred x on the card
    rows = conn.execute(
        "SELECT usd_value / token_amount p FROM trades WHERE mint=? AND ts>? AND ts<=? "
        "AND usd_value >= 1 AND token_amount > 0 AND COALESCE(kind, 'trade') = 'trade' ORDER BY ts",
        (mint, ts, min(now, ts + horizon_s))).fetchall()
    later = [r["p"] for r in rows]
    best = max(later) / px if later else None
    cur = conn.execute("SELECT price_usd FROM tokens WHERE mint=?", (mint,)).fetchone()
    quote = cur["price_usd"] / px if cur and cur["price_usd"] else None
    # The candle that contains the burst counts, and nothing earlier. An hour of slack here let
    # a launch pushed after its opening spike claim the spike: ROBBIN read 6.6x on a push that
    # went to 0.38x, STANDARD 13x for a real 2.6x. Five-minute candles, so the most a push can
    # borrow is the four minutes before it.
    since = [c for c in (candles or []) if c[0] >= ts - 300 and c[0] <= ts + horizon_s]
    # a pool that saw less volume than the cohort itself put in is not where the cohort traded:
    # SYNTH's chart came from an 89%-fee pool with $253 of lifetime volume against a $1,532
    # burst, and one trade in it printed 384x. Its candles are not evidence of anything.
    burst = conn.execute("SELECT usd FROM bursts WHERE mint=? AND ts=?", (mint, ts)).fetchone()
    floor = max(settings.outcome_min_candle_volume_usd, (burst["usd"] or 0) * 0.5 if burst else 0)
    volume = [c[5] for c in since if len(c) > 5 and c[5]]
    if volume and sum(volume) < floor:   # candles without a volume column are judged on price alone
        since = []
    # candles in some other unit than the fill: the pools with a 32-byte id come back from
    # GeckoTerminal off by exactly a thousand, every one of them, and would put a 700x on the
    # scorecard. A clean power of ten between the candle nearest the burst and the cohort's own
    # price is a unit, and the candles are brought into the fill's; anything else is not
    # evidence, and the tape is the only witness
    if since:
        ratio = since[0][1] / px
        unit = next((f for f in (1.0, 1e3, 1e-3, 1e6, 1e-6) if 0.2 <= ratio / f <= 5), None)
        since = [] if unit is None else [[c[0], *[v / unit for v in c[1:5]], *c[5:]] for c in since]
    trail = None
    if since:
        # with candles that agree with the fill, the pool's high is the answer and the tape is
        # not consulted for the peak: a tape row can be mispriced - one FRONTIER sell carried a
        # dollar leg four times the pool's price that minute and read as 9.3x against a real 4x
        best = max(c[2] for c in since) / px
        quote = since[-1][4] / px
        # the trail: out at the first candle whose low is `trail_drop` under the running high,
        # at that level; else at the last close. The hour exit sells the peak short on most
        # alerts (the record: 88% trade above the call, 35% close the hour above it); this is
        # the same candles read the way a reader who rides the peak would
        high, drop = px, settings.trail_drop
        for i, c in enumerate(since):
            # the alert's own candle: its high and its low are in an unknown order, so only a
            # dip under the entry's stop counts, and the high it leaves behind is its close
            if i == 0:
                if c[3] <= px * (1 - drop):
                    trail = 1 - drop
                    break
                high = max(high, c[4])
                continue
            high = max(high, c[2])
            if c[3] <= high * (1 - drop):
                trail = high * (1 - drop) / px
                break
        else:
            trail = since[-1][4] / px
    return {
        "best": round(best, 2) if best is not None else None,
        "last": round(later[-1] / px, 2) if later else None,
        "now": round(quote, 2) if quote is not None else None,
        "trail": round(trail, 2) if trail is not None else None,
        "fills": len(later),
    }


def recent(conn: sqlite3.Connection, chain: str | None = None, hours: int = 24,
           now: int | None = None, horizon_s: int = 86400, candles_for=None) -> list[dict]:
    """Every burst recorded in the window, newest first, with what followed it so far.

    `candles_for(mint)` is optional and answers with the pool's OHLCV rows or None; the API hands
    in its cached one, the digest a fresh client, and a test nothing at all.
    """
    now = now or db.now()
    rows = conn.execute(
        "SELECT b.*, COALESCE(tk.symbol, substr(b.mint,1,8)) sym, tk.liquidity_usd liq, tk.sellable sellable "
        "FROM bursts b LEFT JOIN tokens tk ON tk.mint = b.mint "
        "WHERE b.ts >= ?" + (" AND b.chain = ?" if chain else "") + " ORDER BY b.ts DESC",
        [now - hours * 3600, *([chain] if chain else [])]).fetchall()
    from .provenance import seeded as _seeded

    out = []
    for r in rows:
        who = json.loads(r["who"] or "[]")
        # a burst that later turned out to be seeded stays on the record - the feed did fire on
        # it - but it is not evidence about the feed, and the scorecard leaves it out
        out.append({
            "seeded": _seeded(conn, r["mint"], now)["seeded"],
            "unsellable": r["sellable"] == 0,
            "mint": r["mint"], "sym": r["sym"], "liq": r["liq"], "ts": r["ts"],
            "conviction": r["conviction"], "wallets": r["wallets"], "usd": r["usd"],
            "px": r["px"], "window_s": r["window_s"], "age_s": r["age_s"],
            "who": [w for w, _ in who], "scores": [sc for _, sc in who],
            "age_at_read_h": round((now - r["ts"]) / 3600, 1),
            **outcome(conn, r["mint"], r["ts"], r["px"], horizon_s, now,
                      candles_for(r["mint"]) if candles_for else None),
        })
    return out


# ---------------------------------------------------------------- backtest


def _score_lookup(conn: sqlite3.Connection, mode: str):
    """address, moment -> the score the backtest judges that moment's buy with.

    `strict`: the verdict actually in force then, from score_history — honest, and blind to
    everything before the wallet was first judged. `current`: today's score for the whole tape,
    which reads the answer first but is the only mode with a month of data behind it; the score
    itself was checked out of sample in calibrate.py, so this asks "given who we trust now, what
    followed their bursts". `first`: each wallet's earliest verdict — kept because calibrate.py
    uses it, and it is the weakest of the three here: the first pass on 2026-09-06 averaged 45 and
    was rescored three days later to 66, which is why it was rescored.
    """
    if mode == "current":
        cur = {r["address"]: r["score"] for r in conn.execute(
            "SELECT address, score FROM traders WHERE score IS NOT NULL")}
        return lambda address, ts: cur.get(address)
    hist: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in conn.execute("SELECT address, ts, score FROM score_history WHERE score IS NOT NULL "
                          "ORDER BY ts"):
        hist[r["address"]].append((r["ts"], r["score"]))
    keys = {a: [t for t, _ in v] for a, v in hist.items()}

    def at(address: str, ts: int) -> int | None:
        v = hist.get(address)
        if not v:
            return None
        if mode == "first":
            return v[0][1]
        i = bisect.bisect_right(keys[address], ts) - 1
        return v[i][1] if i >= 0 else None
    return at


BASELINE = (1.0, 24 * 60, 2)   # the signals feed as it stands: two trusted buyers inside a day


def backtest(conn: sqlite3.Connection, chain: str | None = None, deltas=(2.0, 3.0, 4.0, 5.0),
             windows_min=(15, 30, 60, 120), min_wallets=(3,), horizon_s: int = 86400,
             mode: str = "current") -> dict:
    """What followed every burst the rule would have fired on, per setting.

    Price is the one each fill implies — usd_value over token_amount — because that is the only
    price history this project has for every token, and it is exactly the price the cohort paid.
    Outcome after a burst: the best implied price any tracked fill showed inside the horizon,
    and the last one. A token nobody we track touched again inside the horizon is counted
    separately rather than as a win or a loss, since its tape simply ends.

    A wallet's first buy on a token is its first buy, whatever the wallet was scored at the time;
    the score decides whether that entry counts, never which entry is first. Otherwise a holder
    from before scoring began looks like a fresh entrant the day scoring reached them.
    """
    at = _score_lookup(conn, mode)
    not_quote = NOT_QUOTE.format(col="mint")
    fills = conn.execute(
        "SELECT mint, address, side, ts, usd_value, token_amount FROM trades "
        f"WHERE 1=1{not_quote}" + (" AND chain=?" if chain else "") + " ORDER BY ts",
        [chain] if chain else []).fetchall()

    first_any: dict[str, int] = {}
    prices: dict[str, list[tuple[int, float]]] = defaultdict(list)
    buys_by_mint: dict[str, list[tuple]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    span_lo = None
    for f in fills:
        m = f["mint"]
        first_any.setdefault(m, f["ts"])
        px = (f["usd_value"] / f["token_amount"]) if (f["usd_value"] and f["token_amount"]) else None
        if px:
            prices[m].append((f["ts"], px))
        if f["side"] != "buy" or (m, f["address"]) in seen:
            continue
        seen.add((m, f["address"]))
        score = at(f["address"], f["ts"])
        if score is None:
            continue
        span_lo = span_lo or f["ts"]
        if score >= TRUSTED:
            buys_by_mint[m].append((f["ts"], f["address"], score, f["usd_value"], px))
    for v in buys_by_mint.values():
        v.sort()
    price_ts = {m: [t for t, _ in v] for m, v in prices.items()}

    def outcome(mint: str, ts: int, px: float | None):
        if not px or mint not in prices:
            return None
        ks, ps = price_ts[mint], prices[mint]
        lo, hi = bisect.bisect_right(ks, ts), bisect.bisect_right(ks, ts + horizon_s)
        later = [p for _, p in ps[lo:hi]]
        if not later:
            return None
        return max(later) / px, later[-1] / px

    # the days the rule could have fired on: from the first buy a score reaches to the end
    days = ((fills[-1]["ts"] - span_lo) / 86400) if (fills and span_lo) else 0

    def run(d: float, w: int, n: int, label: str = "") -> dict:
        best, last, silent, count = [], [], 0, 0
        for mint, buys in buys_by_mint.items():
            for b in _bursts(buys, d, w * 60, n, first_any.get(mint), once=True):
                count += 1
                o = outcome(mint, b.ts, b.px)
                if o is None:
                    silent += 1
                else:
                    best.append(o[0]); last.append(o[1])
        return {
            "label": label, "delta": d, "window_min": w, "min_wallets": n, "bursts": count,
            "per_day": round(count / days, 2) if days else None,
            "measured": len(best), "silent": silent,
            "median_best": _median(best), "p_best_2x": _share(best, 2.0),
            "p_best_3x": _share(best, 3.0),
            "median_last": _median(last), "p_last_half": _share_below(last, 0.5),
        }

    results = [run(*BASELINE, label="signals feed today")]
    for d in deltas:
        for w in windows_min:
            for n in min_wallets:
                results.append(run(d, w, n))
    return {"mode": mode, "days": round(days, 1), "horizon_h": horizon_s // 3600,
            "tokens_with_trusted_buys": len(buys_by_mint), "rows": results}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[len(s) // 2], 2)


def _share(xs: list[float], bar: float) -> float | None:
    return round(sum(x >= bar for x in xs) / len(xs), 2) if xs else None


def _share_below(xs: list[float], bar: float) -> float | None:
    return round(sum(x < bar for x in xs) / len(xs), 2) if xs else None
