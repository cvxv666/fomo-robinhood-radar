"""Work out which on-chain wallet belongs to a fomo trader.

fomo's API never exposes it. Its `address`, `evmAddress`, swap `address`/`recipient` and trade
`userAddress` are all internal accounts — verified against Codex, they have zero swap events, while
the wallet the same handle trades from has hundreds.

What fomo does tell us is *which token* a user traded and *when*. That is enough: everyone who
traded that token in that minute is a suspect, and the only address that keeps showing up across
several of the user's trades is the user. Rare windows count for more than crowded ones, because
being one of twenty makers is evidence and being one of two hundred is not.

One request per window, so `RESOLVE_WINDOWS` caps how hard we try per trader. On Robinhood Chain
those requests are free: filtering `eth_getLogs` by the token's own address returns every transfer
of it in the window, and the legs facing the trade router are the makers. Other chains still spend
Codex budget for the same answer.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

from .. import db
from ..config import settings
from ..models import norm_addr
from ..sources.codex import NETWORK_IDS, TOKEN_EVENTS_QUERY, Codex

log = logging.getLogger(__name__)


def rank_candidates(windows: list[set[str]]) -> list[tuple[str, float, int]]:
    """Pure: maker sets -> [(address, score, hits)] best first.

    A window contributes 1/len(window) to each of its makers, so a quiet window is strong evidence.
    """
    score: dict[str, float] = defaultdict(float)
    hits: dict[str, int] = defaultdict(int)
    for makers in windows:
        if not makers:
            continue
        weight = 1.0 / len(makers)
        for m in makers:
            score[m] += weight
            hits[m] += 1
    return sorted(((a, s, hits[a]) for a, s in score.items()), key=lambda r: (-r[1], -r[2]))


def decide(ranked: list[tuple[str, float, int]], used: int,
           min_hits: int | None = None, min_ratio: float | None = None) -> tuple[str | None, dict]:
    """Accept the leader only when it is clearly ahead of the runner-up."""
    min_hits = settings.resolve_min_hits if min_hits is None else min_hits
    min_ratio = settings.resolve_min_ratio if min_ratio is None else min_ratio
    if not ranked:
        return None, {"reason": "no candidates", "windows": used}
    top = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    second_hits = ranked[1][2] if len(ranked) > 1 else 0
    ratio = top[1] / second_score if second_score else float("inf")
    info = {"windows": used, "score": round(top[1], 4), "hits": top[2], "second_hits": second_hits,
            "ratio": None if ratio == float("inf") else round(ratio, 2),
            "candidates": len(ranked)}
    if top[2] < min_hits:
        return None, {**info, "reason": f"only {top[2]} hits, need {min_hits}"}
    # Score alone can be close when a bystander shares one very quiet window. How *often* each
    # address turns up separates them much more sharply: the trader is in most windows, a
    # coincidence is in one.
    clear_on_hits = top[2] >= 2 * second_hits and top[2] - second_hits >= 2
    if ratio < min_ratio and not clear_on_hits:
        return None, {**info, "reason": f"ratio {ratio:.2f} below {min_ratio} and hits {top[2]} vs {second_hits} not decisive"}
    return top[0], info


def user_windows(conn: sqlite3.Connection, user_id: str, chain: str, limit: int) -> list[tuple[str, int, str]]:
    """Distinct (token, ts, side) the user traded on this chain, newest first.

    One window per swap, not per token: a wallet that trades the same two names all month is
    in every one of those windows, and each is its own piece of evidence - the makers around
    it at ten past three are not the makers around it the next morning. Grouped by token this
    user had two windows out of fifty-seven swaps and could never reach three hits.
    """
    rows = conn.execute(
        "SELECT token, ts, side FROM fomo_swaps WHERE user_id=? AND chain=? "
        "GROUP BY token, ts ORDER BY ts DESC LIMIT ?",
        (user_id, chain, limit),
    ).fetchall()
    return [(r["token"], r["ts"], r["side"]) for r in rows]


def makers_in_window(codex: Codex, token: str, chain: str, ts: int, side: str,
                     window_s: int | None = None) -> set[str]:
    """Everyone who traded `token` the same way within a few seconds of `ts`."""
    window_s = settings.resolve_window_s if window_s is None else window_s
    net = NETWORK_IDS.get(chain)
    if net is None:
        return set()
    body = codex._post(TOKEN_EVENTS_QUERY, {
        "query": {"address": token, "networkId": net, "eventType": "Swap",
                  "timestamp": {"from": ts - window_s, "to": ts + window_s}},
        "limit": 200, "cursor": None,
    })
    items = ((body.get("data") or {}).get("getTokenEvents") or {}).get("items") or []
    return {norm_addr(e["maker"]) for e in items
            if e.get("maker") and (e.get("eventDisplayType") or "").lower() == side}


def maker_source(chain: str, codex: Codex | None = None, rpc=None):
    """The cheapest way to ask who traded a token in a window, plus the client to count requests on.

    Robinhood Chain answers from its own RPC for nothing. Anything else goes through Codex, where
    each window is one request out of the monthly cap.
    """
    if chain == "robinhood" and "rpc" in settings.resolve_sources:
        from ..sources.rpc import RobinhoodRPC

        client = rpc or RobinhoodRPC()
        return (lambda token, ts, side: client.token_makers(token, ts, side)), client
    client = codex or Codex()
    return (lambda token, ts, side: makers_in_window(client, token, chain, ts, side)), client


def resolve_user(conn: sqlite3.Connection, fetch, user_id: str, chain: str,
                 max_windows: int | None = None) -> tuple[str | None, dict]:
    max_windows = max_windows or settings.resolve_windows
    windows = user_windows(conn, user_id, chain, max_windows)
    if not windows:
        return None, {"reason": "no stored swaps on this chain", "windows": 0}
    sets, used = [], 0
    for token, ts, side in windows:
        try:
            makers = fetch(token, ts, side)
        except Exception as e:  # noqa: BLE001 - one dead window must not abandon the trader
            log.warning("resolve window failed (%s): %s", token[:10], e)
            continue
        if makers:
            sets.append(makers)
            used += 1
    return decide(rank_candidates(sets), used)


def resolve_pending(conn: sqlite3.Connection, codex: Codex | None = None, chain: str = "robinhood",
                    limit: int | None = None) -> dict:
    """Turn fomo users with stored swaps into trackable `traders` rows."""
    fetch, client = maker_source(chain, codex)
    # Never tried first, then the ones tried longest ago, and a failed try is stamped so the
    # queue moves: without the stamp the twenty richest unresolved were asked every fifteen
    # minutes for a day, 132 RPC calls a pass, and the other 187 never got a turn. A user gets
    # another go once new swaps have arrived since the last try, or after three days.
    now = db.now()
    rows = conn.execute(
        "SELECT u.* FROM fomo_users u WHERE u.onchain_address IS NULL "
        "AND EXISTS (SELECT 1 FROM fomo_swaps s WHERE s.user_id=u.user_id AND s.chain=?) "
        "AND (u.onchain_at IS NULL OR u.onchain_at < ? "
        "     OR EXISTS (SELECT 1 FROM fomo_swaps s WHERE s.user_id=u.user_id AND s.chain=? AND s.ts > u.onchain_at)) "
        "ORDER BY u.onchain_at IS NOT NULL, u.onchain_at, COALESCE(u.pnl_30d, u.pnl_7d, u.pnl_24h) DESC NULLS LAST LIMIT ?",
        (chain, now - settings.resolve_retry_days * 86400, chain, limit or settings.resolve_users_per_pass),
    ).fetchall()
    stats = {"attempted": 0, "resolved": 0, "new_traders": 0, "confirmed_existing": 0,
             "conflicts": 0, "unresolved": 0, "requests": 0}
    for u in rows:
        stats["attempted"] += 1
        address, info = resolve_user(conn, fetch, u["user_id"], chain)
        with db.tx(conn):
            existing = db.get_trader(conn, address) if address else None
            # An address belongs to one trader. If someone else already vouched for this wallet
            # under a different handle, the inference lost to a better source — say so rather than
            # relabelling their row. It usually means two traders trade the same tokens together.
            if existing and existing["fomo_handle"] and u["handle"] \
                    and existing["fomo_handle"].lower() != u["handle"].lower():
                stats["conflicts"] += 1
                conn.execute("UPDATE fomo_users SET onchain_note=? WHERE user_id=?",
                             (f"conflict: {address} already attributed to {existing['fomo_handle']}", u["user_id"]))
                log.warning("resolve %s -> %s already belongs to %s, skipped",
                            u["handle"], address[:10], existing["fomo_handle"])
                continue
            if address:
                stats["resolved"] += 1
                known = existing is not None
                if known:
                    stats["confirmed_existing"] += 1
                stats["new_traders"] += db.upsert_trader(
                    conn, address, chain=chain, fomo_user_id=u["user_id"], fomo_handle=u["handle"],
                    profile_address=u["profile_address"], evm_address=u["evm_address"],
                    pnl_24h=u["pnl_24h"], pnl_7d=u["pnl_7d"], pnl_30d=u["pnl_30d"],
                    trades_cnt=u["trades_cnt"], volume_usd=u["volume_usd"],
                    # never relabel a wallet another source already vouched for
                    source=None if known else (u["source"] or "fomo"),
                    status="candidate",
                )
                conn.execute("UPDATE fomo_users SET onchain_at=?, onchain_address=?, onchain_note=? "
                             "WHERE user_id=?", (db.now(), address, str(info), u["user_id"]))
            else:
                stats["unresolved"] += 1
                conn.execute("UPDATE fomo_users SET onchain_at=?, onchain_note=? WHERE user_id=?",
                             (now, str(info), u["user_id"]))
        log.info("resolve %s -> %s %s", u["handle"] or u["user_id"][:8], address or "unresolved", info)
    stats["requests"] = client.requests
    stats["source"] = type(client).__name__
    return stats
