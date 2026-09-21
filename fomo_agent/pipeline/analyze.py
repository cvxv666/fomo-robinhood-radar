"""Answer two questions against the watchlist: what do we know about this token, and this trader.

The database already holds who is good, what they hold and what they bought. These read it back the
way a person asks: point at a contract address and ask whose money is in it, or point at a handle
and ask what that person is actually doing. Nothing here calls an API.

The measure that matters for a token is not how many wallets hold it but *whose*. One trader
scoring 85 says more than ten scoring 40, so holder quality is summed as (score/100)^2 — squaring
keeps a crowd of mediocre wallets from outvoting a good one.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .. import db
from ..config import settings
from ..sources.rpc import QUOTE_TOKENS

TRUSTED = 60
# What an undated launch is worth. Equal to an entry one hour after the pool opened: enough to
# rank, never enough to lead. Chosen rather than derived, and named so it can be argued with.
UNDATED_EARLINESS = 0.5
NOT_QUOTE = " AND {col} NOT IN (%s)" % ",".join("'%s'" % t for t in QUOTE_TOKENS)


def conviction(scores: list[int]) -> float:
    """Holder quality as one number: the sum of (score/100)^2 over distinct holders."""
    return sum((s / 100) ** 2 for s in scores if s)


def _real(t: str) -> str:
    from .provenance import REAL, NOT_SEEDED, NOT_UNSELLABLE
    return REAL.format(t=t) + NOT_SEEDED.format(t=t) + NOT_UNSELLABLE.format(t=t)


def _seed_params() -> list:
    from .provenance import seeded_params
    return seeded_params()


def _own(t: str) -> str:
    from .provenance import REAL
    return REAL.format(t=t)


def signals(conn: sqlite3.Connection, chain: str | None = None, hours: int = 24,
            min_buyers: int = 2, limit: int = 40) -> list[dict]:
    """Tokens that several trusted wallets bought inside the window, best conviction first.

    The canonical definition of a signal — the page, the terminal and the bot all read it from
    here, so they can never drift into disagreeing about what a signal is.

    Each buyer's fills are collapsed before aggregating, so a wallet that bought five times counts
    once toward the headcount and once toward conviction.
    """
    params: list = [db.now() - hours * 3600, TRUSTED]
    if chain:
        params.append(chain)
    return [dict(r) for r in conn.execute(
        "SELECT mint, sym, liq, COUNT(*) buyers, SUM(usd) usd, MIN(first_ts) first_ts, "
        "  AVG(score) avg_score, SUM((score / 100.0) * (score / 100.0)) conviction, "
        "  GROUP_CONCAT(handle) who, GROUP_CONCAT(score) scores FROM ("
        "  SELECT tr.mint mint, COALESCE(tk.symbol, substr(tr.mint,1,8)) sym, "
        "    tk.liquidity_usd liq, t.score score, t.fomo_handle handle, "
        "    SUM(tr.usd_value) usd, MIN(tr.ts) first_ts "
        "  FROM trades tr JOIN traders t ON t.address = tr.address "
        "  LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"  WHERE tr.side='buy' AND tr.ts >= ? AND t.score >= ?{' AND tr.chain=?' if chain else ''}"
        + NOT_QUOTE.format(col="tr.mint") + _real("tr") +
        "  GROUP BY tr.mint, tr.address"
        ") GROUP BY mint HAVING buyers >= ? ORDER BY conviction DESC, usd DESC LIMIT ?",
        [*params, *_seed_params(), min_buyers, limit],
    )]


def exits(conn: sqlite3.Connection, chain: str | None = None, hours: int = 6,
          min_sellers: int = 2, min_exit: float = 0.5, limit: int = 40) -> list[dict]:
    """Tokens the trusted wallets are leaving, heaviest departure first.

    The mirror of `signals`, and arguably the more useful half. Everyone publishes entries; the
    moment that costs a follower money is the one where the wallets they copied quietly left, and
    an entry feed cannot show it — a token stays on it for as long as the buy is inside the window,
    whether or not the buyer is still there.

    A sale is not an exit. Trimming a tenth off a winner is portfolio management; leaving is
    getting out, so a wallet counts only once it has sold `min_exit` of what the tape watched it
    buy, measured in tokens rather than dollars because dollars move with the price. The on-chain
    balance settles it where we have one: nothing left is `closed`, whatever the tape thinks.

    Wallets that were in a name before our tape starts are excluded rather than guessed at — their
    entry size is unknown, so what fraction they have sold is unknowable too.
    """
    cutoff = db.now() - hours * 3600
    # in the order the placeholders appear: the window sum, the score floor, the optional chain,
    # then the window again inside the sub-select that narrows this to tokens with recent selling
    params: list = [cutoff, TRUSTED, *( [chain] if chain else [] ), cutoff]
    rows = [dict(r) for r in conn.execute(
        "SELECT tr.mint mint, COALESCE(tk.symbol, substr(tr.mint,1,8)) sym, "
        "  tk.liquidity_usd liq, tr.address addr, t.score score, t.fomo_handle handle, "
        "  SUM(CASE WHEN tr.side='sell' AND tr.ts >= ? THEN COALESCE(tr.usd_value,0) END) out_usd, "
        "  MAX(CASE WHEN tr.side='sell' THEN tr.ts END) last_sell, "
        "  SUM(CASE WHEN tr.side='buy'  THEN tr.token_amount ELSE 0 END) bought_amt, "
        "  SUM(CASE WHEN tr.side='sell' THEN tr.token_amount ELSE 0 END) sold_amt, "
        "  SUM(tr.token_amount IS NULL) sizeless, h.amount balance "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        "LEFT JOIN holdings h ON h.address = tr.address AND h.token = tr.mint "
        f"WHERE t.score >= ?{' AND tr.chain=?' if chain else ''}"
        "  AND tr.mint IN (SELECT DISTINCT mint FROM trades WHERE side='sell' AND ts >= ?)"
        + NOT_QUOTE.format(col="tr.mint") + _own("tr") +
        " GROUP BY tr.mint, tr.address HAVING out_usd > 0",
        params,
    )]

    by_mint: dict[str, dict] = {}
    for r in rows:
        sizeless = bool(r["sizeless"])
        bought = None if sizeless else r["bought_amt"]
        sold = None if sizeless else r["sold_amt"]
        state, exit_pct = position_state(bought, sold, r["balance"])
        if state not in ("closed", "trimmed") or not exit_pct or exit_pct < min_exit:
            continue
        t = by_mint.setdefault(r["mint"], {
            "mint": r["mint"], "sym": r["sym"], "liq": r["liq"],
            "sellers": 0, "usd": 0.0, "conviction": 0.0, "last_sell": 0,
            "who": [], "scores": [], "gone": 0,
        })
        t["sellers"] += 1
        t["usd"] += r["out_usd"] or 0
        t["conviction"] += (r["score"] / 100.0) ** 2
        t["last_sell"] = max(t["last_sell"], r["last_sell"] or 0)
        t["gone"] += 1 if state == "closed" else 0
        t["who"].append(r["handle"] or r["addr"][:10])
        t["scores"].append(r["score"])

    out = [t for t in by_mint.values() if t["sellers"] >= min_sellers]
    out.sort(key=lambda t: (t["conviction"], t["usd"]), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- fresh launches

def earliness(entered_ts: int, launched_ts: int | None) -> float:
    """How early a wallet was, from 1.0 at the launch to nothing a day later.

    On a token that is hours old, *when* somebody bought is most of what their buy says. A wallet
    scoring 88 that entered four minutes after the pool opened took a different risk from the same
    wallet entering the next morning on a chart everyone could already see, and a feed that scores
    them the same is not reading the thing it claims to read.

    Halving every hour is aggressive on purpose — 0.5 at an hour, 0.09 at ten, 0.04 at a day — and
    it matches how these markets actually move.

    A token we cannot date must not outrank one we can. Returning 1.0 for an unknown launch —
    which this did — scores the wallet as if it had bought in the same second the pool opened, so
    the feed's top was decided by which tokens we had failed to look up. Measured on a live day,
    eight of the top ten showed "?" and sat above tokens with real, measured entries of ten and one
    minute. An unknown launch is now worth what an hour-old entry is worth: present, not first.
    """
    if launched_ts is None:
        return UNDATED_EARLINESS
    return 1.0 / (1.0 + max(entered_ts - launched_ts, 0) / 3600)


def heat(buyers: list[dict], launched_ts: int | None) -> float:
    """Conviction, weighted by how early each wallet got in. The fresh feed's one measure.

    Conviction alone answers whose money is in a name. On something that launched this morning the
    question is sharper — whose money got there *first* — so each buyer's (score/100)² is scaled by
    how soon after the launch they bought.
    """
    return sum((b["score"] / 100) ** 2 * earliness(b["ts"], launched_ts)
               for b in buyers if b.get("score"))


# The tickers a launch on this chain borrows when it wants to look like something it is not: the
# top of the market by name. A token called ZEC here is not Zcash and a token called SOL is not
# Solana; a launch wearing one of these needs a higher bar and a plain word in the message.
LISTED_TICKERS = frozenset("""
BTC ETH USDT XRP BNB SOL USDC DOGE TRX ADA HYPE LINK XLM SUI BCH AVAX HBAR LTC SHIB TON LEO DOT
UNI XMR DAI PEPE AAVE CRO NEAR ETC ONDO APT MNT TAO ICP PI POL KAS ALGO ARB VET RENDER FIL ATOM
OKB FET ENA WLD SEI BONK JUP QNT INJ TIA STX IMX GRT OP FLOKI FLR WIF LDO SAND CRV XDC THETA
NEXO GALA PYTH IOTA JASMY RAY BSV KAIA ENS BTT CAKE ZEC XTZ EOS FLOW MANA AXS NEO EGLD CFX AR
DYDX ROSE CHZ RUNE PENDLE MKR SNX COMP APE TRUMP VIRTUAL PENGU SPX FARTCOIN PUMP WBTC WETH
STETH USDG USDE FDUSD TUSD PAXG XAUT PYUSD MATIC LUNA LUNC FTT BRETT MOG TURBO POPCAT MEW
""".split())
LISTED_EXTRA = frozenset(x.strip().upper() for x in os.environ.get("LISTED_TICKERS_EXTRA", "").split(",") if x.strip())


def listed(sym: str | None) -> bool:
    """Whether the name is a listed ticker's, whatever the case."""
    return bool(sym) and sym.strip().lstrip("$").upper() in (LISTED_TICKERS | LISTED_EXTRA)


def namesakes(conn: sqlite3.Connection, sym: str | None, mint: str, now: int | None = None,
              hours: int = 24) -> list[dict]:
    """Other tokens with this exact name in the last day, oldest first, with what became of each:
    pushed when, or found unsellable. Three musebooks in twelve hours - flat, a rocket, a
    honeypot - and a subscriber could not tell which one the message meant."""
    if not sym:
        return []
    now = now or db.now()
    rows = conn.execute(
        "SELECT mint, COALESCE(created_at, first_seen_at) t0, sellable FROM tokens "
        "WHERE symbol = ? COLLATE NOCASE AND mint != ? AND COALESCE(created_at, first_seen_at) >= ? "
        "ORDER BY t0", (sym, mint, now - hours * 3600)).fetchall()
    out = []
    for r in rows:
        pushed = conn.execute("SELECT MIN(ts) FROM bot_sent WHERE mint IN (?, ?)", (r["mint"], "hot:" + r["mint"])).fetchone()[0]
        out.append({"mint": r["mint"], "t0": r["t0"], "pushed_at": pushed, "unsellable": r["sellable"] == 0})
    return out


def borrowed_name(conn: sqlite3.Connection, sym: str | None, mint: str, now: int | None = None) -> dict | None:
    """Why this name deserves a second look, or None: a listed ticker's, or one launched here
    before within `namesake_days`. {"listed": bool, "earlier": [namesakes], "why": str}.

    The 21 Sep ZEC was the fourth ZEC on the chain in two weeks and a honeypot; NCAT before it.
    The clone rule looks a day back and at pushed names only - a name recycled every few days
    slips under it, and a listed ticker's name needs no earlier twin to be a costume.
    """
    now = now or db.now()
    earlier = namesakes(conn, sym, mint, now, hours=settings.namesake_days * 24)
    is_listed = listed(sym)
    if not earlier and not is_listed:
        return None
    parts = []
    if is_listed:
        parts.append(f"${sym} is a listed ticker; this is a Robinhood Chain token wearing its name")
    if earlier:
        n = len(earlier) + 1
        parts.append(f"the {ORD(n)} ${sym} on this chain in {settings.namesake_days} days")
    return {"listed": is_listed, "earlier": earlier, "why": "; ".join(parts)}


def ORD(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def fresh(conn: sqlite3.Connection, chain: str | None = None, hours: int = 24,
          max_age_h: int = 72, min_liquidity: float = 5_000, min_buyers: int = 2,
          limit: int = 40, now: int | None = None) -> dict:
    """Tokens the cohort has *just started* buying, hottest first.

    Different question from the signal feed, which ranks everything trusted wallets bought today
    however long they have held it. Here a token qualifies only if its first trusted buy landed
    inside the window: the cohort is entering, not sitting.

    Liquidity is the filter that matters. Measured on a live 24h window, the token with the most
    trusted buyers — seventeen of them — had twenty-nine dollars of liquidity left, because the
    pool had already been drained. Ranked on headcount it would have topped the page. Those are
    counted and reported rather than silently dropped, but they do not rank.
    """
    # `now` other than the clock is a replay: the feed as it read at that moment, from the
    # trades that existed then, for judging a push after the fact
    now = now or db.now()
    since = now - hours * 3600
    params: list = [since, now, TRUSTED]
    if chain:
        params.append(chain)
    rows = [dict(r) for r in conn.execute(
        "SELECT tr.mint mint, tr.address address, t.fomo_handle handle, t.score score, "
        "  MIN(tr.ts) ts, SUM(tr.usd_value) usd "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        f"WHERE tr.side='buy' AND tr.ts >= ? AND tr.ts <= ? AND t.score >= ?{' AND tr.chain=?' if chain else ''}"
        + NOT_QUOTE.format(col="tr.mint") + _real("tr") +
        " GROUP BY tr.mint, tr.address", [*params, *_seed_params()],
    )]

    by_mint: dict[str, list[dict]] = {}
    for r in rows:
        by_mint.setdefault(r["mint"], []).append(r)
    if not by_mint:
        return {"tokens": [], "drained": 0, "hours": hours, "max_age_h": max_age_h,
                "min_liquidity": min_liquidity, "min_buyers": min_buyers}

    meta = {r["mint"]: dict(r) for r in conn.execute(
        "SELECT mint, symbol, chain, created_at, liquidity_usd, mcap_usd, price_usd FROM tokens "
        f"WHERE mint IN ({','.join('?' * len(by_mint))})", list(by_mint),
    )}
    # A token whose first trusted buy predates the window is not a launch we are watching happen.
    earliest = {m: min(b["ts"] for b in bs) for m, bs in by_mint.items()}
    prior = {r["mint"] for r in conn.execute(
        "SELECT DISTINCT tr.mint mint FROM trades tr JOIN traders t ON t.address = tr.address "
        f"WHERE tr.side='buy' AND tr.ts < ? AND t.score >= ? AND tr.mint IN ({','.join('?' * len(by_mint))})",
        [since, TRUSTED, *by_mint],
    )}

    out, drained = [], 0
    for mint, buyers in by_mint.items():
        if mint in prior or len(buyers) < min_buyers:
            continue
        m = meta.get(mint, {})
        launched = m.get("created_at")
        age_h = (now - launched) / 3600 if launched else None
        if age_h is not None and age_h > max_age_h:
            continue
        liq = m.get("liquidity_usd")
        # a pool with nothing left in it is a rug that already happened, not a signal
        if liq is not None and liq < min_liquidity:
            drained += 1
            continue
        buyers.sort(key=lambda b: b["ts"])
        scores = [b["score"] for b in buyers if b["score"]]
        first_ts = earliest[mint]
        out.append({
            "mint": mint, "sym": m.get("symbol") or mint[:10], "chain": m.get("chain"),
            "created_at": launched, "age_h": age_h,
            "liq": liq, "mcap": m.get("mcap_usd"), "price": m.get("price_usd"),
            "buyers": len(buyers), "avg_score": sum(scores) / len(scores) if scores else None,
            "conviction": conviction(scores),
            "heat": heat(buyers, launched),
            "usd": sum(b["usd"] or 0 for b in buyers) or None,
            "first_ts": first_ts, "last_ts": max(b["ts"] for b in buyers),
            # how long after the pool opened the first trusted wallet arrived
            "lead_minutes": (first_ts - launched) / 60 if launched else None,
            "who": [b["handle"] or b["address"][:10] for b in buyers],
            "scores": [b["score"] for b in buyers],
            "entries": [{"handle": b["handle"] or b["address"][:10], "score": b["score"],
                         "ts": b["ts"], "usd": b["usd"]} for b in buyers],
        })
    out.sort(key=lambda r: (-r["heat"], -(r["usd"] or 0)))
    return {"tokens": out[:limit], "drained": drained, "hours": hours, "max_age_h": max_age_h,
            "min_liquidity": min_liquidity, "min_buyers": min_buyers}


def leaderboard(conn: sqlite3.Connection, limit: int = 25, status: str = "active") -> list[dict]:
    """The scored roster, best first — our own ranking by judgement rather than by headline PnL."""
    return [dict(r) for r in conn.execute(
        "SELECT fomo_handle handle, address, score, status, ai_summary summary, "
        "  COALESCE(pnl_30d, pnl_7d, pnl_24h) fomo_pnl, tags "
        "FROM traders WHERE score IS NOT NULL AND (? = 'all' OR status = ?) "
        "ORDER BY score DESC, fomo_pnl DESC LIMIT ?", (status, status, limit),
    )]



# ---------------------------------------------------------------- the book

# How much of an entry may still be outstanding before a position counts as closed. Rounding in a
# router and the dust a wallet never bothers to sell both leave a fraction behind.
CLOSED_AT = 0.98
# Selling more than the tape ever saw bought means the entry predates us by some unknown amount.
PRE_TAPE_AT = 1.02


def position_state(bought_amt: float | None, sold_amt: float | None,
                   balance: float | None = None) -> tuple[str, float | None]:
    """(state, share of the watched entry already sold) for one token's buys and sells.

    Given a balance read off the chain, that settles it: nothing left is a closed position, and
    more left than the tape ever saw bought means the wallet was in this name before we were —
    `held`, where the size is known and the cost is not.

    Without one, all we have is the tape. `None` for either amount then means the fills were
    recorded without sizes, so how much is left cannot be known — a different thing from zero.
    """
    exit_pct = (sold_amt / bought_amt) if bought_amt and sold_amt is not None else None

    if balance is not None:
        if balance <= 0:
            return ("closed" if (sold_amt or 0) > 0 else "unknown"), (
                min(exit_pct, 1.0) if exit_pct else None)
        watched = None if bought_amt is None or sold_amt is None else bought_amt - sold_amt
        if watched is None or balance > watched * PRE_TAPE_AT:
            return "held", exit_pct
        return ("trimmed" if exit_pct else "open"), exit_pct

    if bought_amt is None or sold_amt is None:
        return "unknown", None
    if bought_amt <= 0:
        # sells with no entry behind them: bought before we started watching
        return ("pre-tape", None) if sold_amt > 0 else ("unknown", None)
    if exit_pct > PRE_TAPE_AT:
        return "pre-tape", exit_pct
    if exit_pct >= CLOSED_AT:
        return "closed", min(exit_pct, 1.0)
    return ("trimmed" if exit_pct > 0 else "open"), exit_pct


def ledger(conn: sqlite3.Connection, address: str) -> list[dict]:
    """Every token a wallet has traded, folded into one position each.

    The tape is the only record of this trader we own outright, so the book is rebuilt from it
    rather than taken on trust: the buys and sells of one name collapse into money in, money out,
    and how much of the entry is still held. That last figure is what separates a position someone
    closed from one they are sitting in, which is why token sizes matter here as much as dollars.

    A wallet that sold more of a name than the tape ever saw it buy opened that position before we
    started watching. Its profit is unknowable — the entry is missing — and reporting `out - in`
    would invent a windfall out of half a record, so it is marked `pre-tape` and ranked nowhere.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT tr.mint token, COALESCE(tk.symbol, substr(tr.mint,1,10)) sym, tr.chain chain, "
        "  tk.price_usd price, tk.price_at price_at, tk.liquidity_usd liq, "
        "  SUM(CASE WHEN tr.side='buy'  THEN COALESCE(tr.usd_value,0) ELSE 0 END) bought_usd, "
        "  SUM(CASE WHEN tr.side='sell' THEN COALESCE(tr.usd_value,0) ELSE 0 END) sold_usd, "
        "  SUM(CASE WHEN tr.side='buy'  THEN tr.token_amount ELSE 0 END) bought_amt, "
        "  SUM(CASE WHEN tr.side='sell' THEN tr.token_amount ELSE 0 END) sold_amt, "
        "  SUM(tr.token_amount IS NULL) sizeless, COUNT(*) fills, "
        "  SUM(tr.side='buy') buys, SUM(tr.side='sell') sells, "
        "  MIN(tr.ts) first_ts, MAX(tr.ts) last_ts "
        "FROM trades tr LEFT JOIN tokens tk ON tk.mint = tr.mint "
        # a position is what the wallet bought at size: not a swap somebody else delivered to
        # it, not fifty cents pushed through fomo - see pipeline/provenance.py
        "WHERE tr.address = ?" + NOT_QUOTE.format(col="tr.mint") + _own("tr") +
        " GROUP BY tr.mint", (address,),
    )]

    balances = db.holdings_for(conn, address)

    out = []
    for r in rows:
        sizeless = bool(r.pop("sizeless"))
        bought_amt = None if sizeless else r["bought_amt"]
        sold_amt = None if sizeless else r["sold_amt"]
        balance, read_at = balances.get(r["token"], (None, None))
        state, exit_pct = position_state(bought_amt, sold_amt, balance)

        # Average-cost accounting: what came out, less what the part that left had cost. For a
        # position sold down to nothing that is simply out minus in. A wallet that was in the name
        # before we were has an entry price we do not know, so its profit is not stated.
        realized = None
        if state in ("closed", "trimmed") and exit_pct:
            realized = r["sold_usd"] - r["bought_usd"] * min(exit_pct, 1.0)

        held = cost_open = value = unrealized = None
        if state in ("open", "trimmed", "held"):
            held = balance if balance is not None else max((bought_amt or 0) - (sold_amt or 0), 0.0)
            cost_open = r["bought_usd"] * (1 - min(exit_pct or 0, 1.0)) or None
            if r["price"] is not None:
                value = held * r["price"]
                # only where the tape covers the whole entry does value minus cost mean anything
                if state != "held" and cost_open is not None:
                    unrealized = value - cost_open
        elif state == "unknown":
            # Sizes are missing, so how much is left is unknown — but the money is not. Net cash
            # in is what this name has cost the wallet, and a six-figure one must not sort below
            # a fifty-dollar position just because nobody recorded the token counts.
            cost_open = max(r["bought_usd"] - r["sold_usd"], 0.0) or None

        out.append({**r, "bought_amt": bought_amt, "sold_amt": sold_amt,
                    "state": state, "exit_pct": exit_pct, "realized": realized,
                    "held": held, "cost_open": cost_open, "value": value,
                    "unrealized": unrealized, "src": "chain", "read_at": read_at})
    return out


def book(conn: sqlite3.Connection, address: str, user_id: str | None) -> dict:
    """A trader's positions as one answer, from the tape and from fomo's own marks.

    Two records describe the same wallet and neither is complete. The tape holds every fill since
    we started watching, which is most of the book and all of its recent shape. fomo holds three
    positions per trader — the largest — but knows what they are worth today and knows the ones
    opened long before we arrived, which is exactly where the outsized numbers live. So the tape
    supplies the book, and fomo overrides the mark wherever it has one.
    """
    rows = ledger(conn, address)
    bags: dict[str, dict] = {}
    if user_id:
        bags = {r["token"]: dict(r) for r in conn.execute(
            "SELECT p.token token, COALESCE(tk.symbol, p.symbol, substr(p.token,1,10)) sym, "
            "  p.chain chain, p.unrealized_pnl pnl, p.cost_basis cost, p.seen_at seen_at "
            "FROM fomo_positions p LEFT JOIN tokens tk ON tk.mint = p.token "
            "WHERE p.user_id = ?" + NOT_QUOTE.format(col="p.token"), (user_id,),
        )}

    open_rows, closed_rows = [], []
    for r in rows:
        bag = bags.pop(r["token"], None)
        if bag is not None:
            # fomo prices the whole position, including whatever was bought before our first
            # block. A name it still lists is open whatever our own tape reads of it.
            trimmed = bool(r["exit_pct"] and 0 < r["exit_pct"] < 1)
            r = {**r, "pnl": bag["pnl"], "cost": bag["cost"], "marked_at": bag["seen_at"],
                 "src": "fomo", "state": "trimmed" if trimmed else "held"}
        else:
            r = {**r, "pnl": r["unrealized"], "cost": r["cost_open"], "marked_at": r["price_at"]}
        # What the position is worth now: the chain's balance at the token's price where we have
        # both, and otherwise fomo's mark, which is a cost and a profit that add up to the same
        # thing. Sorting and display read the one field, so they cannot disagree.
        r["worth"] = r["value"] if r["value"] is not None else (
            r["cost"] + r["pnl"] if r["cost"] is not None and r["pnl"] is not None else None)
        # A position sold in part sits in both answers at once: still held, and already paid for
        # in part. Hiding the second half is how a page ends up claiming a trader never takes
        # profit, when trimming into strength is the most common thing good ones do.
        if r["state"] not in ("closed", "pre-tape"):
            open_rows.append(r)
        if r["realized"] is not None:
            closed_rows.append(r)

    # bags in no tape of ours: another chain, or a name entered before we started watching
    for token, bag in bags.items():
        open_rows.append({
            "token": token, "sym": bag["sym"], "chain": bag["chain"], "state": "held",
            "pnl": bag["pnl"], "cost": bag["cost"], "marked_at": bag["seen_at"], "src": "fomo",
            "bought_usd": 0.0, "sold_usd": 0.0, "fills": 0, "buys": 0, "sells": 0,
            "first_ts": None, "last_ts": None, "realized": None, "held": None, "price": None,
            "value": None, "unrealized": None, "exit_pct": None, "liq": None, "cost_open": None,
            "read_at": None,
            "worth": (bag["cost"] + bag["pnl"]) if bag["cost"] is not None and bag["pnl"] is not None else None,
        })

    # A book reads by size: biggest position first, whatever it has done. Everything nobody can
    # price follows, ordered by what went into it — a row of dashes is the least informative thing
    # on the page and belongs at the bottom, not scattered between the positions a reader came for.
    open_rows.sort(key=lambda r: (r["worth"] is None, -(r["worth"] or 0),
                                  -(r["cost"] or 0), -(r["bought_usd"] or 0)))
    closed_rows.sort(key=lambda r: -(r["realized"] or 0))

    # A win rate only means something over decided trades, so it counts the positions that were
    # sold out entirely — a trim is a position still running. How many trades had to be left out
    # for want of their entry is reported beside it rather than quietly dropped.
    done = [r for r in closed_rows if r["state"] == "closed"]
    wins = [r for r in done if r["realized"] > 0]
    return {
        "positions": open_rows,
        "closed": closed_rows,
        "realized_usd": sum(r["realized"] for r in closed_rows) or None,
        "round_trips": len(done),
        "wins": len(wins),
        "win_rate": (len(wins) / len(done)) if done else None,
        "pre_tape": sum(1 for r in rows if r["state"] == "pre-tape"),
        "open_pnl": sum(r["pnl"] for r in open_rows if r["pnl"]) or None,
        # What the whole book is worth right now. Unlike the profit it needs no entry price, so it
        # is the one portfolio figure that survives a position opened before we started watching.
        "book_value": sum(r["worth"] for r in open_rows if r["worth"]) or None,
        # when this wallet's tape starts, so a page can say what its own figures do not cover
        "tape_from": min((r["first_ts"] for r in rows if r["first_ts"]), default=None),
    }


def _creator(conn: sqlite3.Connection, token, mint: str) -> dict | None:
    """The token's creator and what their other tokens came to, when the chain has said."""
    if not token or "creator" not in token.keys() or not token["creator"]:
        return None
    from .deployers import history
    h = history(conn, token["creator"], before_mint=mint)
    return {"address": token["creator"], "via": token["created_via"], **h}


def _crews(conn: sqlite3.Connection, addresses: list[str]) -> dict[str, int]:
    from .crews import of
    return of(conn, list({a for a in addresses if a}))


def analyze_token(conn: sqlite3.Connection, mint: str, hours: int = 48) -> dict:
    """Whose money is in this token, what it cost them, and who moved on it recently."""
    mint = mint.lower() if mint.startswith("0x") else mint
    since = db.now() - hours * 3600

    # Who holds it, read off the chain. fomo publishes each trader's three largest bags, which is
    # why this question used to have no answer for anything but a handful of names; a balance has
    # no such limit, so every tracked wallet that owns any of this token appears here. Where fomo
    # does carry the position, its mark is kept: it prices the entry back to whenever it was made.
    holders = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, t.status, h.amount amount, "
        "  h.amount * tk.price_usd value, p.unrealized_pnl pnl, p.cost_basis cost "
        "FROM holdings h JOIN traders t ON t.address = h.address "
        "LEFT JOIN tokens tk ON tk.mint = h.token "
        "LEFT JOIN fomo_positions p ON p.token = h.token AND p.user_id = t.fomo_user_id "
        "WHERE h.token = ? AND h.amount > 0 "
        "ORDER BY COALESCE(t.score,0) DESC, COALESCE(value, 0) DESC", (mint,),
    )]
    # fomo may also carry the position for a wallet whose balance we have not read yet
    seen = {h["address"] for h in holders}
    holders += [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, t.status, p.amount amount, "
        "  NULL value, p.unrealized_pnl pnl, p.cost_basis cost "
        "FROM fomo_positions p JOIN traders t ON t.fomo_user_id = p.user_id "
        "WHERE p.token = ? ORDER BY COALESCE(t.score,0) DESC, p.unrealized_pnl DESC", (mint,),
    ) if r["address"] not in seen]
    flow = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, tr.side, tr.usd_value usd, tr.ts, "
        "  COALESCE(tr.kind, 'trade') kind "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint = ? AND tr.ts >= ? ORDER BY tr.ts DESC", (mint, since),
    )]
    from .provenance import seeded as _seeded
    seeded = _seeded(conn, mint)
    first = conn.execute(
        "SELECT MIN(tr.ts) ts, COUNT(DISTINCT tr.address) buyers FROM trades tr "
        "JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint = ? AND tr.side = 'buy' AND t.score >= ?", (mint, TRUSTED),
    ).fetchone()
    token = conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()

    # Who is *buying* it, grouped by wallet. This is a different question from who holds it, and
    # for a fresh token it is the only one with an answer: positions come from fomo's snapshot of a
    # trader's three biggest bags, so a token nobody has ridden yet appears in nobody's top three
    # while a dozen trusted wallets are already accumulating it.
    by_wallet: dict[str, dict] = {}
    for f in flow:
        w = by_wallet.setdefault(f["address"], {
            "handle": f["handle"], "address": f["address"], "score": f["score"],
            "bought": 0.0, "sold": 0.0, "fills": 0, "first_ts": f["ts"],
        })
        w["fills"] += 1
        w["first_ts"] = min(w["first_ts"], f["ts"])
        w["bought" if f["side"] == "buy" else "sold"] += f["usd"] or 0
    buyers = sorted(by_wallet.values(), key=lambda w: (-(w["score"] or 0), -w["bought"]))

    scores = [h["score"] for h in holders if h["score"]]
    costs = [h["cost"] for h in holders if h["cost"]]
    return {
        # The same measure the feed ranks by, so the two pages can never disagree about a token.
        "buyers": buyers,
        "buyer_conviction": conviction([b["score"] for b in buyers if (b["score"] or 0) >= TRUSTED]),
        "mint": mint,
        "symbol": token["symbol"] if token and token["symbol"] else None,
        "is_quote": mint in QUOTE_TOKENS,
        "liquidity_usd": token["liquidity_usd"] if token else None,
        "mcap_usd": token["mcap_usd"] if token else None,
        "holders": holders,
        "trusted_holders": sum(1 for s in scores if s >= TRUSTED),
        "avg_score": sum(scores) / len(scores) if scores else None,
        "conviction": conviction(scores),
        "cohort_pnl": sum(h["pnl"] for h in holders if h["pnl"]) or None,
        "cohort_cost": sum(costs) or None,
        # what the tracked wallets' holdings are worth at the token's current price
        "cohort_value": sum(h["value"] for h in holders if h["value"]) or None,
        "seeded": seeded, "flow": flow,
        "creator": _creator(conn, token, mint),
        "crews": _crews(conn, [h["address"] for h in holders] + [f["address"] for f in flow]),
        "sellable": token["sellable"] if token else None,
        "sell_note": token["sell_note"] if token else None,
        "bought_usd": sum(f["usd"] or 0 for f in flow if f["side"] == "buy"),
        "sold_usd": sum(f["usd"] or 0 for f in flow if f["side"] == "sell"),
        "first_trusted_buy": first["ts"] if first else None,
        "trusted_buyers": first["buyers"] if first else 0,
        # What the cohort *said* about it. Every other number on this page is inferred from the
        # tape; these are the traders' own words, and only from wallets carrying a verdict.
        "theses": db.theses_for_token(conn, mint),
        "hours": hours,
    }


def find_trader(conn: sqlite3.Connection, who: str) -> sqlite3.Row | None:
    """Accept a handle or an address, case-insensitively."""
    return conn.execute(
        "SELECT * FROM traders WHERE lower(address) = lower(?) OR lower(fomo_handle) = lower(?)",
        (who, who),
    ).fetchone()


def distributing(bought_usd: float | None, sold_usd: float | None) -> str | None:
    """Why the week's flow says the wallet is handing out, or None. The rule wants both size
    and proportion: a $60k sale against $50k of buys is trading, $125k against $11k is not."""
    sold, bought = sold_usd or 0.0, bought_usd or 0.0
    if sold >= settings.distrib_min_sold_usd and sold >= settings.distrib_ratio * max(bought, 1.0):
        return f"distributing: sold {usd(sold)} against {usd(bought)} bought in 7d"
    return None


def flow_7d(conn: sqlite3.Connection, address: str, now: int | None = None) -> dict:
    """The wallet's own buys and sells of the last seven days, in dollars, and the verdict on them."""
    now = now or db.now()
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN usd_value END), 0) bought, "
        "  COALESCE(SUM(CASE WHEN side='sell' THEN usd_value END), 0) sold FROM trades "
        "WHERE address = ? AND ts >= ? AND COALESCE(kind, 'trade') = 'trade'" + NOT_QUOTE.format(col="mint"),
        (address, now - 7 * 86400)).fetchone()
    return {"bought": r["bought"], "sold": r["sold"], "distributing": distributing(r["bought"], r["sold"])}


def pnl_gap(fomo_pnl: float | None, stats: dict) -> dict | None:
    """fomo's figure against the indexer's realized one, when both exist and disagree by more
    than `pnl_gap_usd`: {"fomo", "indexer", "gap"}. Different questions - fomo counts open
    positions, the indexer what was closed - and a $270k gap is worth seeing before trusting either."""
    realized = stats.get("realized_pnl") if stats else None
    if fomo_pnl is None or realized is None:
        return None
    gap = float(fomo_pnl) - float(realized)
    if abs(gap) < settings.pnl_gap_usd:
        return None
    return {"fomo": fomo_pnl, "indexer": realized, "gap": gap}


def analyze_trader(conn: sqlite3.Connection, who: str, hours: int = 168) -> dict | None:
    """One trader's verdict, open bags, recent fills and who else is in the same names."""
    row = find_trader(conn, who)
    if row is None:
        return None
    address, since = row["address"], db.now() - hours * 3600

    # Quote assets are excluded everywhere below: a wallet holding USDG is holding cash, and a
    # WETH leg is how a swap is paid for, not a position anyone took.
    positions = book(conn, address, row["fomo_user_id"])
    fills = [dict(r) for r in conn.execute(
        "SELECT tr.ts, tr.side, tr.usd_value usd, tr.mint, tr.source, "
        "  COALESCE(tr.kind, 'trade') kind, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,10)) sym FROM trades tr "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        "WHERE tr.address = ? AND tr.ts >= ?" + NOT_QUOTE.format(col="tr.mint") +
        " ORDER BY tr.ts DESC", (address, since),
    )]
    # Who else this trader keeps showing up next to. Read from the tape rather than from fomo's
    # three bags: overlap only means something across a whole book, and against three names almost
    # everyone looks like a stranger.
    held = [p["token"] for p in positions["positions"][:60]]
    company = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.score, COUNT(DISTINCT tr.mint) shared FROM trades tr "
        "JOIN traders t ON t.address = tr.address "
        f"WHERE tr.side='buy' AND tr.mint IN ({','.join('?' * len(held))}) "
        "  AND tr.address != ? AND t.score IS NOT NULL "
        "GROUP BY tr.address ORDER BY shared DESC, t.score DESC LIMIT 8",
        (*held, address),
    )] if held else []
    tags = json.loads(row["tags"]) if row["tags"] else {}
    stats = json.loads(row["stats_json"]) if row["stats_json"] else {}
    fomo_pnl = row["pnl_30d"] or row["pnl_7d"] or row["pnl_24h"]
    flow = flow_7d(conn, address)
    return {
        "address": address, "handle": row["fomo_handle"], "chain": row["chain"],
        "score": row["score"], "status": row["status"], "summary": row["ai_summary"],
        "model": row["ai_model"], "style": tags.get("style") or [],
        "red_flags": tags.get("red_flags") or [], "stats": stats,
        "fomo_pnl": fomo_pnl,
        # the week's flow and what it says; the two PnL sources side by side when they disagree
        "flow_7d": flow, "distributing": flow["distributing"], "pnl_gap": pnl_gap(fomo_pnl, stats),
        # the whole book: what is still held, what was closed, and what the closed part earned
        **positions,
        "fills": fills,
        "bought_usd": sum(f["usd"] or 0 for f in fills if f["side"] == "buy"),
        "sold_usd": sum(f["usd"] or 0 for f in fills if f["side"] == "sell"),
        "company": company, "hours": hours,
    }


# ---------------------------------------------------------------- terminal output

def usd(v) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if a >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.0f}"


def format_token(a: dict) -> str:
    name = a["symbol"] or a["mint"][:10]
    out = [f"# {name}  {a['mint']}"]
    if a["is_quote"]:
        out.append("\n**This is a quote asset.** Every swap passes through it, so holdings and buys "
                   "here are plumbing, not conviction.")
    out.append(f"\nliquidity {usd(a['liquidity_usd'])} · mcap {usd(a['mcap_usd'])}")
    out.append(f"\n## Who holds it\n")
    if not a["holders"]:
        out.append("_nobody on the watchlist_")
    else:
        out.append(f"{len(a['holders'])} holders, {a['trusted_holders']} of them scoring {TRUSTED}+ · "
                   f"avg score {a['avg_score']:.0f} · conviction {a['conviction']:.2f}"
                   if a["avg_score"] else f"{len(a['holders'])} holders, none scored")
        out.append(f"cohort cost {usd(a['cohort_cost'])} → open PnL {usd(a['cohort_pnl'])}")
        out.append("")
        for h in a["holders"][:15]:
            out.append(f"  {str(h['score'] or '--'):>3}  {(h['handle'] or h['address'][:10]):<20} "
                       f"{usd(h['pnl']):>9} open   cost {usd(h['cost'])}")
    out.append(f"\n## Flow, last {a['hours']}h\n")
    if not a["flow"]:
        out.append("_no fills recorded_")
    else:
        out.append(f"bought {usd(a['bought_usd'])} · sold {usd(a['sold_usd'])} · "
                   f"{len(a['flow'])} fills by {len({f['address'] for f in a['flow']})} wallets")
        for f in a["flow"][:15]:
            out.append(f"  {f['side']:<4} {usd(f['usd']):>9}  {(f['handle'] or f['address'][:10]):<20} "
                       f"score {f['score'] or '--'}")
    return "\n".join(out)


def format_trader(a: dict) -> str:
    out = [f"# {a['handle'] or a['address']}  ({a['status']}, score {a['score']})", a["address"]]
    if a["summary"]:
        out.append(f"\n{a['summary']}")
    if a["style"] or a["red_flags"]:
        out.append("style: " + ", ".join(a["style"]) + ("  flags: " + ", ".join(a["red_flags"]) if a["red_flags"] else ""))
    out.append(f"\nfomo 30d {usd(a['fomo_pnl'])} · open bags {len(a['positions'])} "
               f"worth {usd(a['open_pnl'])} unrealised")
    s = a["stats"]
    if s:
        bits = [f"{k} {v}" for k, v in (("fills", s.get("fills")),
                                        ("volume", usd(s["volume"]) if s.get("volume") else None)) if v]
        if a["round_trips"] >= 5 and a["win_rate"] is not None:
            bits.append(f"win {a['win_rate']*100:.0f}% of {a['round_trips']} round trips")
        out.append(" · ".join(bits))

    out.append(f"\n## The book — {len(a['positions'])} names open\n")
    for p in a["positions"][:15] or [None]:
        out.append(f"  {p['sym'][:14]:<14} {usd(p['pnl']):>9} open   cost {usd(p['cost']):<9} "
                   f"{p['state']}" if p else "_none_")
    if a["pre_tape"]:
        out.append(f"\n  ({a['pre_tape']} more sold down from an entry older than our tape, so "
                   "neither size nor profit can be stated)")
    out.append("\n## What came back out\n")
    if not a["closed"]:
        out.append("_nothing sold yet inside our tape_")
    else:
        line = f"realised {usd(a['realized_usd'])} over {len(a['closed'])} positions"
        if a["round_trips"]:
            line += f", {a['wins']} of {a['round_trips']} sold out entirely for a profit"
        out.append(line)
        for p in a["closed"][:10]:
            exit_at = "all" if p["state"] == "closed" else f"{(p['exit_pct'] or 0) * 100:.0f}%"
            out.append(f"  {p['sym'][:14]:<14} {usd(p['realized']):>9} realised   "
                       f"in {usd(p['bought_usd']):<9} out {usd(p['sold_usd']):<9} sold {exit_at}")
    out.append(f"\n## Fills, last {a['hours']}h\n")
    out.append(f"bought {usd(a['bought_usd'])} · sold {usd(a['sold_usd'])} · {len(a['fills'])} fills")
    for f in a["fills"][:15]:
        out.append(f"  {f['side']:<4} {usd(f['usd']):>9}  {f['sym']:<14} via {f['source']}")
    if a["company"]:
        out.append("\n## Sits in the same names as\n")
        for c in a["company"]:
            out.append(f"  {str(c['score']):>3}  {c['handle']:<20} {c['shared']} shared positions")
    return "\n".join(out)
