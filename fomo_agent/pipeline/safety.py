"""Can it be sold. The question every other number on a token page assumes the answer to.

A honeypot is a token that lets you buy and not sell, and on a tape it looks better than anything
real: three trusted wallets in, a clean 7x, and it just sits there, because nobody can get out.
STONKINU did exactly that on the first day with subscribers - pushed as a launch, 7.4x, flagged
Unsafe on every screener, and the card said "still there" as if that were a virtue.

Two witnesses, and either is enough to say no:

  the chain    a transfer of the token from a wallet that holds it to the pool, simulated with
               eth_call. That is the first thing a sell does, and the usual honeypot blocks it
               right there - a revert when `to` is the pair and the sender is not on a list.
               Costs nothing, answers in a second, and works from the first minute of the pool.
  the pool     what GeckoTerminal counts: buys and sells in the last day. A pool half an hour
               old with eight buys and not one sell has nobody who managed to leave.

And one witness for yes: a tracked wallet that actually sold, on our own tape.

The verdict lives on the token (`sellable`: 1, 0 or NULL for not known), is asked for again after
a while, and is read by every feed the way `seeded` is: an unsellable token is not a signal, not
a launch and not a burst, whatever its numbers say.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import db
from ..config import settings
from ..sources.rpc import BALANCE_SELECTOR, RobinhoodRPC, RpcError, topic_for

log = logging.getLogger(__name__)

TRANSFER_SELECTOR = "0xa9059cbb"   # transfer(address,uint256)
MIN_BUYS_FOR_SILENCE = 8           # buys with no sell at all before the silence counts
MIN_AGE_FOR_SILENCE_S = 30 * 60    # and the pool has to have had time for one


def _u256(n: int) -> str:
    return format(max(0, int(n)), "064x")


def _is_address(s: str | None) -> bool:
    return bool(s) and s.startswith("0x") and len(s) == 42


def probe_transfer(rpc: RobinhoodRPC, mint: str, pool: str, holder: str) -> tuple[str, str]:
    """Simulate the first step of a sell: the holder moving 1% of what it has to the pool.

    Returns ("blocked" | "ok" | "unknown", why). Unknown is a holder with no balance to move, or
    a pool that is not a plain address (a v4-style pool id has no account to transfer to).
    """
    if not _is_address(pool):
        return "unknown", "pool is not a plain address; nothing to transfer to"
    try:
        raw = rpc.call("eth_call", [{"to": mint, "data": BALANCE_SELECTOR + topic_for(holder)[2:]}, "latest"])
        units = int(raw, 16) if raw and raw != "0x" else 0
    except (RpcError, ValueError, TypeError) as e:
        return "unknown", f"balance unreadable: {e}"
    if units <= 0:
        return "unknown", "the holder has nothing left to move"
    amount = max(1, units // 100)
    data = TRANSFER_SELECTOR + topic_for(pool)[2:] + _u256(amount)
    try:
        raw = rpc.call("eth_call", [{"from": holder, "to": mint, "data": data}, "latest"])
    except RpcError as e:
        return "blocked", f"a transfer to the pool reverts: {str(e)[:120]}"
    if raw in (None, "0x"):
        return "ok", "a transfer to the pool goes through (returns nothing, which old tokens do)"
    try:
        return ("ok", "a transfer to the pool goes through") if int(raw, 16) else ("blocked", "a transfer to the pool returns false")
    except ValueError:
        return "unknown", "a transfer to the pool answered something unreadable"


def holders_to_try(conn: sqlite3.Connection, mint: str, limit: int = 3) -> list[str]:
    """Tracked wallets that bought it for real, biggest buy first: the ones with something to sell."""
    return [r["address"] for r in conn.execute(
        "SELECT tr.address, MAX(tr.usd_value) usd FROM trades tr WHERE tr.mint = ? AND tr.side = 'buy' "
        "AND COALESCE(tr.kind, 'trade') = 'trade' GROUP BY tr.address ORDER BY usd DESC LIMIT ?", (mint, limit))]


def cohort_sold(conn: sqlite3.Connection, mint: str) -> bool:
    """Somebody we track got out. The one thing a honeypot cannot show."""
    return conn.execute(
        "SELECT 1 FROM trades tr JOIN traders t ON t.address = tr.address WHERE tr.mint = ? AND tr.side = 'sell' "
        "AND COALESCE(tr.kind, 'trade') = 'trade' AND tr.usd_value > 0 LIMIT 1", (mint,)).fetchone() is not None


def pool_silence(gt, chain: str, pool: str, now: int) -> tuple[str, str] | None:
    """The pool's own count of buys and sells. ("silent" | "sells", why) or None if it cannot say."""
    if not pool:
        return None
    try:
        a = gt.pool(chain, pool)
    except Exception as e:  # noqa: BLE001 - a screener that is busy is not a verdict
        log.debug("pool lookup for %s failed: %s", pool, e)
        return None
    if not a:
        return None
    tx = (a.get("transactions") or {}).get("h24") or {}
    buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
    created = a.get("created_at")
    age = (now - created) if created else None
    if sells > 0:
        return "sells", f"{sells} sells against {buys} buys in the pool's last day"
    if buys >= MIN_BUYS_FOR_SILENCE and age is not None and age >= MIN_AGE_FOR_SILENCE_S:
        return "silent", f"{buys} buys and not one sell in the pool's last day, {age // 60} minutes in"
    return None


def check(conn: sqlite3.Connection, mint: str, rpc: RobinhoodRPC | None = None, gt=None,
          now: int | None = None, max_age_s: int = 600, force: bool = False) -> dict:
    """The verdict for one token, from storage if recent enough, else asked again and stored.

    Returns {"sellable": 1|0|None, "note": str, "checked_at": int, "asked": bool}.
    """
    now = now or db.now()
    row = conn.execute("SELECT sellable, sell_checked_at, sell_note, pool_address, chain FROM tokens WHERE mint=?", (mint,)).fetchone()
    if row is None:
        return {"sellable": None, "note": "unknown token", "checked_at": None, "asked": False}
    if not force and row["sell_checked_at"] and now - row["sell_checked_at"] < max_age_s:
        return {"sellable": row["sellable"], "note": row["sell_note"], "checked_at": row["sell_checked_at"], "asked": False}
    # a verdict of no is final: a token that blocked a sell once is not asked again to be sure
    if not force and row["sellable"] == 0:
        return {"sellable": 0, "note": row["sell_note"], "checked_at": row["sell_checked_at"], "asked": False}

    chain = row["chain"] or (settings.dex_chains[0] if settings.dex_chains else "robinhood")
    verdict, note = None, "nothing to go on yet"
    # the chain first: cheap, immediate, and the usual pattern blocks exactly this
    try:
        rpc = rpc or RobinhoodRPC()
        for holder in holders_to_try(conn, mint):
            state, why = probe_transfer(rpc, mint, row["pool_address"] or "", holder)
            if state == "blocked":
                verdict, note = 0, why
                break
            if state == "ok":
                verdict, note = 1, why
                break
            note = why
    except Exception as e:  # noqa: BLE001 - no rpc is no verdict, not a crash in the watcher
        log.debug("transfer probe for %s failed: %s", mint, e)
    if verdict != 0 and cohort_sold(conn, mint):
        verdict, note = 1, "a tracked wallet sold it"
    if verdict != 0:
        try:
            if gt is None:
                from ..sources.geckoterminal import GeckoTerminal
                gt = GeckoTerminal(patient=False)
            witness = pool_silence(gt, chain, row["pool_address"] or "", now)
        except Exception:  # noqa: BLE001
            witness = None
        if witness:
            state, why = witness
            if state == "silent" and verdict is None:
                verdict, note = 0, why
            elif state == "sells" and verdict is None:
                verdict, note = 1, why
            elif state == "silent" and verdict == 1:
                # the transfer goes through but nobody has ever sold: say so, but a working transfer is
                # the stronger witness for the first hours
                note = f"{note}; yet {why}"
    with db.tx(conn):
        conn.execute("UPDATE tokens SET sellable=?, sell_checked_at=?, sell_note=? WHERE mint=?", (verdict, now, note, mint))
    if verdict == 0:
        log.warning("unsellable: %s - %s", mint, note)
    return {"sellable": verdict, "note": note, "checked_at": now, "asked": True}


def sweep(conn: sqlite3.Connection, limit: int = 12, now: int | None = None, rpc=None, gt=None) -> dict:
    """Ask again about the tokens the cohort touched today whose verdict is missing or stale.

    Bounded per pass, longest-unasked first, so the cost stays flat: a dozen eth_calls and at most
    a dozen screener requests every fifteen minutes.
    """
    now = now or db.now()
    rows = conn.execute(
        "SELECT tk.mint FROM tokens tk WHERE tk.mint IN ("
        "  SELECT tr.mint FROM trades tr JOIN traders t ON t.address = tr.address "
        "  WHERE tr.side = 'buy' AND tr.ts >= ? AND t.score >= 60) "
        "AND COALESCE(tk.sellable, 1) != 0 AND COALESCE(tk.sell_checked_at, 0) < ? "
        "ORDER BY COALESCE(tk.sell_checked_at, 0) LIMIT ?",
        (now - 86400, now - 1800, limit)).fetchall()
    stats = {"asked": 0, "unsellable": 0, "sellable": 0, "unknown": 0}
    for r in rows:
        v = check(conn, r["mint"], rpc=rpc, gt=gt, now=now, force=True)
        stats["asked"] += 1
        stats["unsellable" if v["sellable"] == 0 else "sellable" if v["sellable"] == 1 else "unknown"] += 1
        time.sleep(0.2)
    return stats
