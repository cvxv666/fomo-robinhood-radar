"""Discovery: who is worth tracking.

Three independent sources, any of which can be unavailable:
  - fomo leaderboard (24h/7d/30d)             -> needs FOMO_SESSION
  - fomo top-PnL holders of a token           -> needs FOMO_SESSION
  - Codex buyers of a fresh token             -> needs CODEX_API_KEY, works without fomo

fomo rows land in `fomo_users` first. A leaderboard profile address is not what trades on-chain,
so a second call per user resolves the real execution wallet before a `traders` row is created.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings
from ..models import norm_addr
from ..sources.fomo import FomoClient, FomoError, FomoNotConfigured

log = logging.getLogger(__name__)


def _store_fomo_rows(conn: sqlite3.Connection, rows, source: str) -> int:
    n_new = 0
    with db.tx(conn):
        for r in rows:
            if not r.fomo_user_id:
                continue
            n_new += db.upsert_fomo_user(
                conn, r.fomo_user_id, handle=r.fomo_handle, profile_address=r.profile_address,
                evm_address=r.evm_address, pnl_24h=getattr(r, "pnl_24h", None),
                pnl_7d=getattr(r, "pnl_7d", None), pnl_30d=getattr(r, "pnl_30d", None),
                trades_cnt=getattr(r, "trades_cnt", None), volume_usd=getattr(r, "volume_usd", None),
                source=source,
            )
    return n_new


def _store_theses(conn: sqlite3.Connection, rows, mint: str) -> int:
    """Keep the notes traders attached to their positions, and stamp that we asked about this token.

    The stamp goes on regardless of whether anyone wrote anything, so a token nobody has an opinion
    about rotates out of the queue instead of being asked again forever.
    """
    n_new = 0
    with db.tx(conn):
        for r in rows:
            if not getattr(r, "thesis", None) or not r.fomo_user_id:
                continue
            n_new += db.upsert_thesis(
                conn,
                trade_id=r.trade_id or f"h:{r.fomo_user_id}:{norm_addr(mint)}",
                user_id=r.fomo_user_id, mint=norm_addr(mint), text=r.thesis,
                pnl_usd=r.pnl_usd, cost_usd=r.cost_basis_usd, is_dev=r.is_dev,
            )
        conn.execute("UPDATE tokens SET thesis_at=? WHERE mint=?", (db.now(), norm_addr(mint)))
    return n_new


def resolve_execution_wallets(conn: sqlite3.Connection, fomo: FomoClient, limit: int | None = None) -> dict:
    """Record the per-chain addresses fomo reports for a user. **Not trading wallets.**

    This was written believing fomo's `/swaps` exposed the wallet a trader executes from. It does
    not: those addresses are internal accounts with zero on-chain events, verified against Codex in
    session 6. It once wrote them straight into `traders`, which produced 49 rows that could never
    be tracked or scored.

    So it now only annotates `fomo_users`. The wallet that actually trades is inferred from token
    events by `pipeline/resolve.py`, which needs no fomo call at all.
    """
    rows = db.unresolved_fomo_users(conn, limit or settings.fomo_resolve_limit)
    stats = {"looked_up": 0, "wallets": 0, "new_traders": 0, "empty": 0, "errors": 0}
    for u in rows:
        try:
            addrs = fomo.execution_addresses(u["user_id"])
            stats["looked_up"] += 1
        except FomoError as e:
            stats["errors"] += 1
            with db.tx(conn):
                conn.execute("UPDATE fomo_users SET resolve_error=? WHERE user_id=?", (str(e)[:200], u["user_id"]))
            log.warning("resolve %s failed: %s", u["handle"] or u["user_id"][:8], e)
            continue
        stats["wallets"] += len(addrs)
        with db.tx(conn):
            if not addrs:
                stats["empty"] += 1
            conn.execute(
                "UPDATE fomo_users SET resolved_at=?, resolve_error=? WHERE user_id=?",
                (db.now(), f"fomo reports {addrs} — internal accounts, not trading wallets"
                 if addrs else None, u["user_id"]),
            )
    log.info("resolve execution wallets: %s", stats)
    return stats


def discover_leaderboard(conn: sqlite3.Connection, fomo: FomoClient | None = None,
                         periods: tuple[str, ...] | None = None, resolve: bool = True) -> dict:
    fomo = fomo or FomoClient()
    stats = {"periods": {}, "new_users": 0, "requests": 0}
    for period in periods or settings.leaderboard_periods:
        rows = fomo.leaderboard(period)
        stats["periods"][period] = len(rows)
        stats["new_users"] += _store_fomo_rows(conn, rows, f"leaderboard_{period}")
        log.info("leaderboard %s: %d rows", period, len(rows))
    if resolve:
        stats["resolve"] = resolve_execution_wallets(conn, fomo)
    stats["requests"] = fomo.requests
    return stats


def discover_holders(conn: sqlite3.Connection, mint: str, fomo: FomoClient | None = None,
                     network_id: int = 1399811149, top_n: int | None = None, resolve: bool = True) -> dict:
    fomo = fomo or FomoClient()
    rows = fomo.token_holders(mint, network_id=network_id, limit=top_n)
    stats = {"mint": mint, "rows": len(rows), "new_users": _store_fomo_rows(conn, rows, f"holders:{mint}")}
    with db.tx(conn):
        db.upsert_token(conn, norm_addr(mint), triggered_at=db.now())
    stats["theses"] = _store_theses(conn, rows, mint)
    if resolve:
        stats["resolve"] = resolve_execution_wallets(conn, fomo)
    log.info("holders %s: %s", mint[:10], stats)
    return stats


def discover_makers(conn: sqlite3.Connection, mint: str, chain: str = "solana",
                    codex=None, top_n: int | None = None, min_usd: float | None = None) -> dict:
    """Codex-based discovery: wallets buying a fresh token become candidates. No fomo needed.

    Costs one Codex request per token, so callers must cap how many tokens they run this on.
    """
    from ..sources.codex import Codex

    codex = codex or Codex()
    top_n = top_n or settings.holders_top_n
    min_usd = settings.discover_min_buy_usd if min_usd is None else min_usd
    makers = codex.token_makers(mint, chain)
    picked = [m for m in makers if m["usd_bought"] >= min_usd][:top_n]
    n_new = 0
    with db.tx(conn):
        for m in picked:
            n_new += db.upsert_trader(conn, m["address"], chain=chain, source=f"makers:{mint}", status="candidate")
        db.upsert_token(conn, mint, chain=chain, triggered_at=db.now())
    log.info("makers %s [%s]: %d wallets seen, %d kept, %d new", mint[:8], chain, len(makers), len(picked), n_new)
    return {"mint": mint, "chain": chain, "seen": len(makers), "kept": len(picked), "new": n_new}


def import_browser_export(conn: sqlite3.Connection, source) -> dict:
    """Ingest what the browser collected — a file path, or the payload itself from the extension.

    Cloudflare blocks server-side calls to fomo, so this is how fomo data actually gets in:
    the user's own browser fetches it, we parse the untouched payloads with the same parsers
    the live client would use.
    """
    import json
    from pathlib import Path

    from ..sources.fomo import (parse_holders, parse_leaderboard, parse_swap_rows,
                                parse_top_holdings, parse_trade_rows)

    raw = source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding="utf-8"))
    stats = {"exported_at": raw.get("exportedAt"), "periods": {}, "new_users": 0,
             "resolved": 0, "swaps": 0, "positions": 0, "new_positions": 0, "holders": {}}

    for period, payload in (raw.get("leaderboards") or {}).items():
        rows = parse_leaderboard(payload, period)
        stats["periods"][period] = len(rows)
        stats["new_users"] += _store_fomo_rows(conn, rows, f"leaderboard_{period}")
        # the same response also carries each trader's largest open positions
        with db.tx(conn):
            for h in parse_top_holdings(payload):
                stats["positions"] += 1
                stats["new_positions"] += db.upsert_fomo_position(conn, **h)

    # What the collector *tried* to ask about, and what each ask returned. Without these a
    # collection that asked for thirteen tokens and delivered one is indistinguishable from a
    # collection that only ever asked for one — which is exactly how a broken batch survived twenty
    # passes unnoticed.
    stats["asked"] = len(raw.get("asked") or [])
    stats["holder_errors"] = raw.get("holderErrors") or {}
    stats["theses"] = 0
    for mint, payload in (raw.get("holders") or {}).items():
        # Per token, and soft. A collection carries three leaderboards, hundreds of swaps and a
        # dozen holder lists; one token whose payload we cannot parse must cost us that token, not
        # the delivery. It cost the whole delivery once — an hour of collection lost to a field
        # that turned out not to be a string — and the browser has no way to retry.
        try:
            rows = parse_holders(payload, mint)
            stats["holders"][mint] = len(rows)
            stats["new_users"] += _store_fomo_rows(conn, rows, f"holders:{mint}")
            with db.tx(conn):
                db.upsert_token(conn, norm_addr(mint), triggered_at=db.now())
            stats["theses"] += _store_theses(conn, rows, mint)
        except Exception as e:  # noqa: BLE001
            log.warning("holders payload for %s could not be read: %s", mint[:10], e)
            # same map the collector fills in: what went wrong, keyed by the token it went wrong on
            stats["holder_errors"][mint] = f"unreadable here: {str(e)[:100]}"

    # The addresses fomo reports are internal accounts with no on-chain swaps (verified against
    # Codex), so the swaps are stored as evidence and pipeline/resolve.py infers the real wallet.
    for user_id, payload in (raw.get("swaps") or {}).items():
        swaps = ((payload or {}).get("responseObject") or {}).get("swaps") or []
        with db.tx(conn):
            for s in swaps:
                for row in parse_swap_rows(user_id, s):
                    stats["swaps"] += db.insert_fomo_swap(conn, **row)
            conn.execute("UPDATE fomo_users SET resolved_at=? WHERE user_id=?", (db.now(), user_id))
        stats["resolved"] += 1

    # per-token positions: realized AND unrealized PnL, entry price, cost basis
    trades_raw = raw.get("trades") or {}
    stats["trades_users"] = len(trades_raw)
    stats["trade_errors"] = list((raw.get("tradeErrors") or {}).values())[:3]
    for user_id, payload in trades_raw.items():
        rows = parse_trade_rows(user_id, payload)
        with db.tx(conn):
            for row in rows:
                stats["positions"] += 1
                stats["new_positions"] += db.upsert_fomo_position(conn, **row)

    # if the endpoint answered but nothing parsed, keep one body so the shape can be fixed
    if trades_raw and not stats["positions"]:
        sample = Path("fomo_trades_sample.json")
        sample.write_text(json.dumps(next(iter(trades_raw.values())), indent=1)[:200000], encoding="utf-8")
        stats["unparsed_sample"] = str(sample)

    log.info("fomo import: %s", stats)
    return stats


def discover_trenches(conn: sqlite3.Connection, client=None, window: str | None = None) -> dict:
    """Import fomo traders from robinhoodtrenches.com — handles already mapped to execution wallets.

    This is the cheap path for Robinhood Chain: no fomo session, no Codex budget, one request.
    Their per-trader stats (realized PnL, win rate, best/worst trade) are stored verbatim and end
    up in the scoring context.
    """
    import json as _json

    from ..sources.trenches import Trenches, parse_trader

    client = client or Trenches()
    rows = client.traders(window or settings.trenches_window, stocks=settings.trenches_include_stocks)
    stats = {"window": window or settings.trenches_window, "seen": len(rows), "new": 0, "updated": 0}
    with db.tx(conn):
        for raw in rows:
            t = parse_trader(raw)
            if not t:
                continue
            created = db.upsert_trader(
                conn, t["address"], chain=t["chain"], fomo_handle=t["fomo_handle"],
                trades_cnt=t["trades_cnt"], volume_usd=t["volume_usd"], win_rate=t["win_rate"],
                stats_json=_json.dumps(t["stats"]), stats_at=db.now(), stats_source="trenches",
                source="trenches", status="candidate",
            )
            stats["new"] += created
            stats["updated"] += 0 if created else 1
    log.info("trenches discovery: %s", stats)
    return stats


def add_manual(conn: sqlite3.Connection, address: str, handle: str | None = None, chain: str | None = None) -> bool:
    """Add a wallet by hand (works without fomo at all). Chain is guessed from the address format."""
    address = norm_addr(address)
    chain = chain or guess_chain(address)
    with db.tx(conn):
        return db.upsert_trader(conn, address, chain=chain, fomo_handle=handle, source="manual", status="candidate")


def guess_chain(address: str) -> str:
    return "evm" if address.startswith("0x") and len(address) == 42 else "solana"


def safe_fomo() -> FomoClient | None:
    try:
        return FomoClient()
    except FomoNotConfigured as e:
        log.debug("fomo disabled: %s", e)   # the fomo units collect on their own; here it is expected
        return None
