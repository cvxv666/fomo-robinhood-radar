"""Claude scoring: compact per-trader context (<= 2 KB) -> strict JSON -> traders.score/status/tags."""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from pathlib import Path
from typing import Any

from .. import db
from ..sources.rpc import QUOTE_TOKENS
from ..config import settings
from ..models import ScoreResult

log = logging.getLogger(__name__)

SYSTEM = """You are a crypto trading analyst evaluating memecoin traders for a research watchlist.
You receive, for ONE trader: fomo.family's own PnL figures, on-chain trade aggregates, and sometimes a
third-party indexer's stats. Respond with ONLY a JSON object matching exactly this schema, no prose:
{"score": <int 0-100>, "status": "active"|"watch"|"dropped",
 "style": [subset of "sniper","swing","scalper","holder","copy-follower"],
 "red_flags": [subset of "bot","bundler","insider-like","wash","one-hit"],
 "summary": "<2-3 sentences>", "confidence": <float 0-1>}

How to weigh the evidence:
- fomo PnL (pnl_30d / pnl_7d / pnl_24h) is the primary signal. It is profit in USD **including open
  positions**, which is where memecoin results actually sit: a 5k entry that runs 500x is millions on
  paper and nothing realized. Do not discount it for being unrealized.
- On-chain flow (usd_bought vs usd_sold) is the sanity check, not the verdict. A trader who is
  accumulating shows negative flow while being deeply profitable; that is normal, not a red flag.
- `open_positions` is the book behind that number: what they hold, what it cost, and the multiple.
  A large PnL resting on one position is thinner evidence than the same PnL across several, and a
  high multiple on a real cost basis is the clearest proof of an early entry there is.
- Trade counts, unique tokens, hold times and early entries describe the *style*.
- Genuine red flags: minute-scale holds with uniform sizes (bot), round-tripping with no result
  (wash), a single position explaining everything (one-hit).
- `flags.distributing`, when present, means the wallet sold many times what it bought this week
  at size: it is handing out what it holds, whatever the PnL figure says. Status is "watch" at
  most for such a wallet, and the summary should say what it is selling down.

Guidance: score>=70 -> active (large PnL with a repeatable pattern behind it);
40-69 -> watch (promising, thin evidence, or profit that rests on one unresolved position);
<40 -> dropped (no PnL and no edge, bot-like, or wash).
Few trades = low confidence."""

# rough public list prices, USD per 1M tokens (input, output); used only for cost logging
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


def build_context(conn: sqlite3.Connection, address: str) -> dict[str, Any]:
    t = db.get_trader(conn, address)
    now = db.now()
    ctx: dict[str, Any] = {
        "address": address,
        "chain": t["chain"] if t else "solana",
        "fomo": {k: t[k] for k in ("fomo_handle", "pnl_24h", "pnl_7d", "pnl_30d", "win_rate", "trades_cnt", "volume_usd")
                 if t and t[k] is not None},
        "source": t["source"] if t else None,
    }
    # precomputed stats from a third-party indexer (realized PnL, win rate, best/worst trade)
    if t and t["stats_json"]:
        try:
            ctx["indexer_stats"] = {"source": t["stats_source"], **json.loads(t["stats_json"])}
        except (TypeError, ValueError):
            pass
    for days in (7, 30):
        # only what the wallet did itself, at size: a swap delivered by an outside key or a
        # dust push is somebody else's action and must not colour the verdict either way
        rows = conn.execute(
            "SELECT side, sol_amount, usd_value, mint, ts FROM trades WHERE address=? AND ts>=? "
            "AND COALESCE(kind,'trade') = 'trade' ORDER BY ts",
            (address, now - days * 86400),
        ).fetchall()
        buys = [r for r in rows if r["side"] == "buy"]
        sells = [r for r in rows if r["side"] == "sell"]
        # SOL only exists on Solana; every other chain reports size in USD
        sizes = [r["sol_amount"] for r in buys if r["sol_amount"]]
        usd_buys = [r["usd_value"] for r in buys if r["usd_value"]]
        usd_sells = [r["usd_value"] for r in sells if r["usd_value"]]
        # hold time: first buy -> first sell per mint
        first_buy: dict[str, int] = {}
        holds: list[int] = []
        for r in rows:
            if r["side"] == "buy":
                first_buy.setdefault(r["mint"], r["ts"])
            elif r["mint"] in first_buy:
                holds.append(r["ts"] - first_buy.pop(r["mint"]))
        # early entries: bought within 10 min of token creation (when tokens.created_at known)
        early = conn.execute(
            "SELECT COUNT(*) FROM trades tr JOIN tokens tk ON tk.mint=tr.mint "
            "WHERE tr.address=? AND tr.side='buy' AND tr.ts>=? AND tk.created_at IS NOT NULL AND tr.ts-tk.created_at<=600 "
            "AND COALESCE(tr.kind,'trade') = 'trade'",
            (address, now - days * 86400),
        ).fetchone()[0]
        ctx[f"last_{days}d"] = {
            "trades": len(rows), "buys": len(buys), "sells": len(sells),
            "unique_tokens": len({r["mint"] for r in rows}),
            "median_buy_sol": round(statistics.median(sizes), 3) if sizes else None,
            "sol_spent": round(sum(sizes), 2) or None,
            "sol_received": round(sum(r["sol_amount"] or 0 for r in sells), 2) or None,
            "median_buy_usd": round(statistics.median(usd_buys)) if usd_buys else None,
            "usd_bought": round(sum(usd_buys)) or None,
            "usd_sold": round(sum(usd_sells)) or None,
            "median_hold_min": round(statistics.median(holds) / 60) if holds else None,
            "early_buys_10min": early,
        }
    d7 = ctx["last_7d"]
    from .analyze import distributing
    why = distributing(d7.get("usd_bought"), d7.get("usd_sold"))
    if why:
        ctx["flags"] = {"distributing": why}
    # The open book, straight from fomo. This is the most direct evidence there is: what a trader
    # is holding, what it cost, and how far in front they are. A cost of null means fomo counts
    # profit already withdrawn from the position, so the entry price cannot be recovered.
    if t and t["fomo_user_id"]:
        quotes = ",".join(f"'{q}'" for q in QUOTE_TOKENS)
        rows = conn.execute(
            "SELECT COALESCE(tk.symbol, substr(p.token, 1, 8)) sym, p.unrealized_pnl pnl, "
            "  p.cost_basis cost FROM fomo_positions p LEFT JOIN tokens tk ON tk.mint = p.token "
            f"WHERE p.user_id = ? AND p.token NOT IN ({quotes}) "
            "ORDER BY p.unrealized_pnl DESC LIMIT 5", (t["fomo_user_id"],),
        ).fetchall()
        ctx["open_positions"] = [{
            "symbol": r["sym"], "pnl_usd": round(r["pnl"]) if r["pnl"] is not None else None,
            "cost_usd": round(r["cost"]) if r["cost"] else None,
            "multiple": round((r["cost"] + r["pnl"]) / r["cost"], 1)
            if r["cost"] and r["cost"] > 0 and r["pnl"] is not None else None,
            # no cost basis and a profit: the tokens arrived by transfer or airdrop and fomo
            # counts their whole value as profit. Not trading, and the model should know.
            "unearned": bool((not r["cost"]) and r["pnl"] and r["pnl"] > 0),
        } for r in rows]
        unearned = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(unrealized_pnl), 0) FROM fomo_positions "
            f"WHERE user_id = ? AND closed_at IS NULL AND token NOT IN ({quotes}) "
            "AND (cost_basis IS NULL OR cost_basis = 0) AND unrealized_pnl > 0", (t["fomo_user_id"],)).fetchone()
        ctx["unearned_pnl"] = {"positions": unearned[0], "usd": round(unearned[1])}

    recent = conn.execute(
        "SELECT side, mint, sol_amount, ts FROM trades WHERE address=? "
        "AND COALESCE(kind,'trade') = 'trade' ORDER BY ts DESC LIMIT 10", (address,)
    ).fetchall()
    ctx["recent"] = [{"s": r["side"][0], "m": r["mint"][:6], "sol": r["sol_amount"], "ts": r["ts"]} for r in recent]
    return ctx


def call_claude(ctx: dict, model: str) -> tuple[ScoreResult, float]:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    user = json.dumps(ctx, separators=(",", ":"))[:2048]
    last_err: Exception | None = None
    cost = 0.0
    for attempt in range(2):
        msg = client.messages.create(
            model=model, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        pin, pout = PRICES.get(model, (0, 0))
        cost += msg.usage.input_tokens * pin / 1e6 + msg.usage.output_tokens * pout / 1e6
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        try:
            return parse_score(text), cost
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("score parse failed (attempt %d): %s | %s", attempt + 1, e, text[:200])
    raise ValueError(f"unparseable score after retry: {last_err}")


def parse_score(text: str) -> ScoreResult:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return ScoreResult.model_validate_json(text[start : end + 1])


# ---------- manual / in-chat scoring (no ANTHROPIC_API_KEY needed) ----------
#
# Flow:  score --export pending.json  ->  a Claude session reads it and writes results.json
#        ->  score --import results.json  ->  same DB writes as the API path.
# The exported file carries the schema and the same rules the API prompt uses, so the
# in-chat model produces identical output shape.

EXPORT_INSTRUCTIONS = (
    "Score each wallet in `wallets`. Reply with a JSON array only, one object per wallet: "
    '{"address": "<address>", "score": 0-100, "status": "active|watch|dropped", '
    '"style": ["sniper"|"swing"|"scalper"|"holder"|"copy-follower"], '
    '"red_flags": ["bot"|"bundler"|"insider-like"|"wash"|"one-hit"], '
    '"summary": "2-3 sentences", "confidence": 0-1}. '
    "Weigh fomo PnL first: it is USD profit INCLUDING open positions, which is where memecoin results sit. "
    "On-chain buy/sell flow is only a sanity check — an accumulating trader shows negative flow while being "
    "profitable. `open_positions` shows the book behind the number: several winners beat one, and a high "
    "multiple on a real cost basis is the clearest evidence of an early entry. A position marked `unearned` "
    "(profit with no cost basis) was received, not bought - an airdrop, a transfer, a dev allocation: count none of "
    "its PnL as skill, and flag `insider-like` when it is most of the number; `unearned_pnl` sums them. "
    "Rules: score>=70 -> active (large PnL with a repeatable pattern); 40-69 -> watch (promising, "
    "thin, or resting on one unresolved position); <40 -> dropped (no PnL and no edge, bot-like, wash). "
    "Few trades = low confidence."
)


DIGEST_COLUMNS = ("handle", "fills", "closed", "win%", "realized", "unreal", "volume",
                  "best", "worst", "bags", "state", "7d", "7dUSD", "hold")


def digest_lines(payload: dict) -> list[str]:
    """One line per wallet, ranked by banked profit — small enough to score in a chat.

    The full contexts stay in the file; this is the view a human (or a model) actually reads.
    """
    def num(v, nd=0):
        if v is None:
            return "-"
        try:
            return f"{float(v):,.{nd}f}"
        except (TypeError, ValueError):
            return str(v)

    def net30(w):
        d = w.get("last_30d") or {}
        return (d.get("usd_sold") or 0) - (d.get("usd_bought") or 0)

    def fomo_pnl(w):
        f = w.get("fomo") or {}
        return f.get("pnl_30d") or f.get("pnl_7d") or f.get("pnl_24h") or 0

    # Traders we imported from an indexer carry its realized PnL; ones we resolved ourselves have
    # only our own on-chain history, so rank on what every row actually has.
    # fomo's own PnL leads: it includes open positions, and in memecoins that is where the result is.
    # On-chain flow stays as the sanity check underneath it.
    def best_multiple(w):
        """The strongest open position, which is what a large fomo figure usually rests on."""
        ms = [p.get("multiple") for p in (w.get("open_positions") or []) if p.get("multiple")]
        return max(ms) if ms else None

    def top_bag(w):
        ps = w.get("open_positions") or []
        return ps[0]["symbol"] if ps and ps[0].get("symbol") else "-"

    wallets = sorted(payload["wallets"], key=lambda w: -(fomo_pnl(w) or net30(w)))
    out = [f"{'#':>4} {'handle':<18}{'fomoPnL':>12}{'realized':>11}{'30d':>5}{'buy':>5}{'sel':>5}{'tok':>5}"
           f"{'usdBuy':>11}{'usdSell':>11}{'net':>11}{'hold':>7}{'e10m':>5}{'7d':>5}"
           f"{'win%':>6}{'bags':>5}{'bestX':>7} {'topBag':<10} address"]
    for i, w in enumerate(wallets, 1):
        s = w.get("indexer_stats") or {}
        d30, d7 = w.get("last_30d") or {}, w.get("last_7d") or {}
        wr = s.get("win_rate")
        out.append(
            f"{i:>4} {str(w.get('fomo', {}).get('fomo_handle'))[:17]:<18}"
            f"{num(fomo_pnl(w)):>12}{num(s.get('realized_pnl')):>11}"
            f"{num(d30.get('trades')):>5}{num(d30.get('buys')):>5}{num(d30.get('sells')):>5}"
            f"{num(d30.get('unique_tokens')):>5}"
            f"{num(d30.get('usd_bought')):>11}{num(d30.get('usd_sold')):>11}{num(net30(w)):>11}"
            f"{num(d30.get('median_hold_min')):>7}{num(d30.get('early_buys_10min')):>5}"
            f"{num(d7.get('trades')):>5}"
            f"{(f'{wr*100:.0f}' if isinstance(wr, (int, float)) else '-'):>6}"
            f"{num(s.get('open_bags')):>5}"
            f"{(f'{best_multiple(w):.1f}' if best_multiple(w) else '-'):>7} {top_bag(w)[:9]:<10} {w['address']}")
    return out


def looks_automated(ctx: dict[str, Any]) -> str | None:
    """Reject market-making bots before they cost a scoring call. Returns the reason, or None.

    Frequency alone does not identify a bot — the best early-entry traders here fire hundreds of
    trades a week at sub-minute holds. What separates them is *breadth*: a person spreading 259
    trades over 100 tokens is hunting, while 600 trades confined to six tokens at a two-minute hold
    is a wallet cycling inventory. All three conditions have to hold.
    """
    d = ctx.get("last_7d") or {}
    trades, tokens, hold = d.get("trades") or 0, d.get("unique_tokens") or 0, d.get("median_hold_min")
    if trades >= settings.bot_min_trades_7d and 0 < tokens <= settings.bot_max_tokens             and hold is not None and hold <= settings.bot_max_hold_min:
        return (f"{trades} trades in {tokens} tokens at a {hold:.0f}-minute median hold: "
                "automated inventory cycling, not a trader to follow")
    return None


def drop_automated(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], int]:
    """Mark the obvious bots dropped and take them out of the scoring queue."""
    keep, dropped = [], 0
    for r in rows:
        reason = looks_automated(build_context(conn, r["address"]))
        if reason is None:
            keep.append(r)
            continue
        dropped += 1
        with db.tx(conn):
            db.set_status(conn, r["address"], "dropped")
            conn.execute(
                "UPDATE traders SET score=?, tags=?, ai_summary=?, ai_model=?, ai_scored_at=? WHERE address=?",
                (settings.bot_score, json.dumps({"style": ["scalper"], "red_flags": ["bot"]}),
                 reason, "heuristic:bot", db.now(), r["address"]),
            )
        log.info("bot heuristic dropped %s: %s", r["address"][:10], reason)
    return keep, dropped


def pending_for_scoring(conn: sqlite3.Connection, *, force: bool = False, limit: int | None = None,
                        unscored_only: bool = False) -> list[sqlite3.Row]:
    # `needs_review` is where a wallet lands when scoring failed or the model refused to commit.
    # Leaving it out of this list is what makes the status a dead end instead of a retry queue.
    rows = db.traders_by_status(conn, "tracking", "active", "watch", "dropped", "needs_review")
    # A wallet with no verdict at all is a hole in the product; one with a verdict two days old is
    # a refinement. When scoring is done by hand, those two deserve different sittings.
    if unscored_only:
        return [r for r in rows if r["score"] is None][:limit] if limit else [
            r for r in rows if r["score"] is None]
    rows = [r for r in rows if force or needs_rescore(r)]
    return rows[:limit] if limit else rows


def export_contexts(conn: sqlite3.Connection, path: Path, *, force: bool = False,
                    limit: int | None = None, unscored_only: bool = False) -> dict:
    rows = pending_for_scoring(conn, force=force, limit=limit, unscored_only=unscored_only)
    rows, bots = drop_automated(conn, rows)
    contexts = [build_context(conn, r["address"]) for r in rows]
    # a wallet with no fills on our tape and no open book is a name and a number from fomo -
    # there is nothing to judge, and a verdict on nothing is worse than a queue
    judged = [c for c in contexts if (c.get("last_30d") or {}).get("trades") or c.get("open_positions")]
    payload = {
        "instructions": EXPORT_INSTRUCTIONS,
        "schema": ScoreResult.model_json_schema(),
        "generated_at": db.now(),
        "wallets": judged,
    }
    Path(path).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"exported": len(judged), "nothing_to_judge": len(contexts) - len(judged), "bots_dropped": bots, "path": str(path)}


def import_results(conn: sqlite3.Connection, path: Path, model: str = "manual") -> dict:
    """Ingest scores produced outside the API (in-chat). Same validation as the API path."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("expected a JSON array of results, or an object with a 'results' array")
    stats = {"imported": 0, "invalid": 0, "unknown_address": 0, "by_status": {}}
    for item in items:
        address = (item or {}).get("address")
        if not address:
            stats["invalid"] += 1
            continue
        if db.get_trader(conn, address) is None:
            stats["unknown_address"] += 1
            continue
        try:
            res = ScoreResult.model_validate({k: v for k, v in item.items() if k != "address"})
        except Exception as e:  # noqa: BLE001
            log.warning("invalid score for %s: %s", address[:8], e)
            stats["invalid"] += 1
            continue
        apply_score(conn, address, res, model)
        stats["imported"] += 1
        stats["by_status"][res.status] = stats["by_status"].get(res.status, 0) + 1
    return stats


def apply_score(conn: sqlite3.Connection, address: str, res: ScoreResult, model: str) -> None:
    """Write the verdict. A distributing wallet is watch at most whatever the model said: the
    rule is the flow's, not the model's, and the summary carries the reason."""
    status, summary, flags = res.status, res.summary, list(res.red_flags)
    from .analyze import flow_7d
    why = flow_7d(conn, address)["distributing"]
    if why:
        if status == "active":
            status = "watch"
        if "distributing" not in flags:
            flags.append("distributing")
        if "distributing" not in (summary or ""):
            summary = f"{summary.rstrip('.')}. Held to watch: {why}." if summary else f"Held to watch: {why}."
    with db.tx(conn):
        conn.execute(
            "UPDATE traders SET score=?, status=?, tags=?, ai_summary=?, ai_scored_at=?, ai_model=? WHERE address=?",
            (res.score, status, json.dumps({"style": res.style, "red_flags": flags}),
             summary, db.now(), model, address),
        )
        db.add_score_history(conn, address, res.score, status, model, summary)


def needs_rescore(row: sqlite3.Row) -> bool:
    if row["ai_scored_at"] is None:
        return True
    hours = settings.rescore_after_hours.get(row["status"], 0)
    return (db.now() - row["ai_scored_at"]) >= hours * 3600


def score_trader(conn: sqlite3.Connection, address: str, model: str | None = None) -> tuple[ScoreResult | None, float]:
    model = model or settings.score_model
    ctx = build_context(conn, address)
    try:
        res, cost = call_claude(ctx, model)
    except ValueError as e:
        with db.tx(conn):
            db.set_status(conn, address, "needs_review")
            db.add_score_history(conn, address, -1, "needs_review", model, str(e)[:300])
        return None, 0.0
    apply_score(conn, address, res, model)
    return res, cost


def score_all(conn: sqlite3.Connection, *, deep: bool = False, force: bool = False, limit: int | None = None) -> dict:
    if settings.scorer == "manual":
        out = Path(settings.manual_scores_path)
        stats = export_contexts(conn, out, force=force, limit=limit)
        stats["scorer"] = "manual"
        log.info("scorer=manual: %d contexts written to %s; score them in chat, then `score --import`",
                 stats["exported"], out)
        return stats
    if deep:
        rows = conn.execute("SELECT * FROM traders WHERE status='active' ORDER BY score DESC LIMIT ?", (settings.deep_top_n,)).fetchall()
        model = settings.deep_model
    else:
        rows = pending_for_scoring(conn, force=force)
        model = settings.score_model
    if limit:
        rows = rows[:limit]
    rows, bots = (rows, 0) if deep else drop_automated(conn, rows)
    stats = {"scored": 0, "needs_review": 0, "bots_dropped": bots, "cost_usd": 0.0,
             "model": model, "by_status": {}}
    for r in rows:
        res, cost = score_trader(conn, r["address"], model)
        stats["cost_usd"] += cost
        if res is None:
            stats["needs_review"] += 1
            continue
        stats["scored"] += 1
        stats["by_status"][res.status] = stats["by_status"].get(res.status, 0) + 1
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    log.info("score: %s", stats)
    return stats
