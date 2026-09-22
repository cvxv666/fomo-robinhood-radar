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
SILENCE = "not one sell"           # the wording of a silence verdict; the one kind of no that is asked again


def _u256(n: int) -> str:
    return format(max(0, int(n)), "064x")


def _is_address(s: str | None) -> bool:
    return bool(s) and s.startswith("0x") and len(s) == 42


def probe_transfer(rpc: RobinhoodRPC, mint: str, pool: str, holder: str) -> tuple[str, str]:
    """Simulate the first step of a sell: the holder moving 1% of what it has to the pool.

    Returns ("blocked" | "ok" | "unknown", why). Unknown is a holder with no balance to move, or
    a pool that is not a plain address (a v4-style pool id has no account to transfer to).
    """
    # A v4-style pool is an id, not an account, and the sell goes through the router instead. A
    # transfer to the router from a plain holder is the same first step; a revert there is the
    # same no (the sender-list honeypot says no to any transfer that is not the deployer's).
    # A transfer that goes through says less, because the trap may be keyed to the pool manager
    # rather than the router - so that answer stays "unknown", and the pool's own record decides.
    to, where, weak = (pool, "pool", False) if _is_address(pool) else (next(iter(settings.rpc_routers), ""), "router", True)
    if not _is_address(to):
        return "unknown", "pool is not a plain address; nothing to transfer to"
    try:
        raw = rpc.call("eth_call", [{"to": mint, "data": BALANCE_SELECTOR + topic_for(holder)[2:]}, "latest"])
        units = int(raw, 16) if raw and raw != "0x" else 0
    except (RpcError, ValueError, TypeError) as e:
        return "unknown", f"balance unreadable: {e}"
    if units <= 0:
        return "unknown", "the holder has nothing left to move"
    amount = max(1, units // 100)
    data = TRANSFER_SELECTOR + topic_for(to)[2:] + _u256(amount)
    try:
        raw = rpc.call("eth_call", [{"from": holder, "to": mint, "data": data}, "latest"])
    except RpcError as e:
        # a cap on the size of one transfer is a limit, not a block: six tokens read unsellable
        # on 21 Sep for "Exceeds max tx" on a one-percent move, VDT among them, which had done
        # 2x with sellers all the way. Asked again a hundred thousandth at a time
        if _is_limit(e):
            data = TRANSFER_SELECTOR + topic_for(to)[2:] + _u256(max(1, units // 100_000))
            try:
                raw = rpc.call("eth_call", [{"from": holder, "to": mint, "data": data}, "latest"])
            except RpcError as e2:
                return "blocked", f"a transfer to the {where} reverts even at a hundred thousandth: {str(e2)[:100]}"
            ok = "unknown" if weak else "ok"
            return ok, f"a transfer to the {where} goes through at a hundred thousandth (one percent exceeds its max tx)"
        return "blocked", f"a transfer to the {where} reverts: {str(e)[:120]}"
    ok = "unknown" if weak else "ok"
    if raw in (None, "0x"):
        return ok, f"a transfer to the {where} goes through (returns nothing, which old tokens do)"
    try:
        return (ok, f"a transfer to the {where} goes through") if int(raw, 16) else ("blocked", f"a transfer to the {where} returns false")
    except ValueError:
        return "unknown", f"a transfer to the {where} answered something unreadable"


def _is_limit(e: Exception) -> bool:
    """The revert of a size cap, as tokens word it, rather than of a sell that is not allowed."""
    m = str(e).lower()
    return any(w in m for w in ("max tx", "maxtx", "max transaction", "exceeds max", "max wallet", "maxwallet", "amount too large", "exceeds limit"))


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
    if buys >= settings.sell_silence_min_buys and age is not None and age >= silence_wait_s(buys):
        return "silent", f"{buys} buys and {SILENCE} in the pool's last day, {age // 60} minutes in"
    return None


def silence_wait_s(buys: int) -> int:
    """How long a pool with this many buys and no sell gets before the silence is a verdict.

    Eight buys wait the full half hour - a small pool may just be early. Ninety-one buys with
    nobody out is not early at any age, so the wait shrinks in proportion, down to the floor.
    """
    full, floor = settings.sell_silence_min_age_s, settings.sell_silence_floor_s
    if buys <= 0:
        return full
    return int(max(floor, min(full, full * settings.sell_silence_min_buys / buys)))


def check(conn: sqlite3.Connection, mint: str, rpc: RobinhoodRPC | None = None, gt=None,
          now: int | None = None, max_age_s: int = 600, force: bool = False) -> dict:
    """The verdict for one token, from storage if recent enough, else asked again and stored.

    Returns {"sellable": 1|0|None, "note": str, "checked_at": int, "asked": bool}.
    """
    now = now or db.now()
    row = conn.execute("SELECT sellable, sell_checked_at, sell_note, pool_address, chain, liquidity_usd FROM tokens WHERE mint=?", (mint,)).fetchone()
    if row is None:
        return {"sellable": None, "note": "unknown token", "checked_at": None, "asked": False}
    if not force and row["sell_checked_at"] and now - row["sell_checked_at"] < max_age_s:
        return {"sellable": row["sellable"], "note": row["sell_note"], "checked_at": row["sell_checked_at"], "asked": False}
    # a verdict of no is final: a token that blocked a sell once is not asked again to be sure.
    # The pool's silence is the one no that can be outgrown - a first sell lands and the count
    # says so - and that one is asked again after its while, like a yes
    if not force and row["sellable"] == 0 and SILENCE not in (row["sell_note"] or ""):
        return {"sellable": 0, "note": row["sell_note"], "checked_at": row["sell_checked_at"], "asked": False}

    chain = row["chain"] or (settings.dex_chains[0] if settings.dex_chains else "robinhood")
    # every pool the screener knows for it charges a trap fee (SYNTH: 82% and 89%, made minutes
    # after the push): the lookup stores no pool and zero depth, and there is nothing to sell into
    if row["pool_address"] is None and row["liquidity_usd"] == 0:
        note = "only trap pools (fee over 10%): no market to sell into"
        with db.tx(conn):
            conn.execute("UPDATE tokens SET sellable=0, sell_checked_at=?, sell_note=? WHERE mint=?", (now, note, mint))
        log.warning("unsellable: %s - %s", mint, note)
        return {"sellable": 0, "note": note, "checked_at": now, "asked": False}
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
        if row["sellable"] != 0:
            try:
                void_pushes(conn, mint, note, now)
            except Exception as e:  # noqa: BLE001 - the verdict stands whether or not the word got out
                log.warning("could not void the pushes on %s: %s", mint[:10], e)
    return {"sellable": verdict, "note": note, "checked_at": now, "asked": True}


def void_pushes(conn: sqlite3.Connection, mint: str, note: str, now: int | None = None, tg=None) -> int:
    """A token that was pushed and has since proved unsellable: the chats that got the push are
    told so, once, with the fact that settled it, and the hooks get the same event.

    ZEC went out as a launch to seven chats at 19:47 and was called unsellable at 20:03, and
    nobody who got the push heard about it until the hour's follow-up. Sixteen minutes is the
    difference between a reader who checks the pool and one who buys more. Facts only: the count
    and the verdict, no advice - what to do with a pool nobody has left is the reader's call.
    """
    from . import pushes as ledger

    now = now or db.now()
    rows = conn.execute("SELECT p.*, COALESCE(tk.symbol, substr(p.mint, 1, 8)) sym FROM pushes p "
                        "LEFT JOIN tokens tk ON tk.mint = p.mint WHERE p.mint = ? AND p.ts >= ? ORDER BY p.ts",
                        (mint, now - settings.telegram_realert_hours * 3600)).fetchall()
    if not rows:
        return 0
    from ..bot import Telegram, fmt_void, gone, unsubscribe
    from . import discord, webhooks

    first = rows[0]
    text = fmt_void(first, note, now)
    key = f"void:{mint}"
    first_time = conn.execute("SELECT 1 FROM bot_sent WHERE mint = ?", (key,)).fetchone() is None
    chats: list[str] = []
    for row in rows:
        chats += [c for c in ledger.recipients(conn, row) if c not in chats]
    sent = 0
    if tg is None and settings.telegram_bot_token:
        tg = Telegram()
    for chat_id in chats:
        if conn.execute("SELECT 1 FROM bot_sent WHERE chat_id = ? AND mint = ?", (chat_id, key)).fetchone():
            continue
        if tg is None:
            continue
        try:
            tg.send(chat_id, text)
            sent += 1
        except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
            log.warning("void of %s to %s failed: %s", mint[:10], chat_id, e)
            if gone(e):
                unsubscribe(conn, chat_id)
            continue
        with db.tx(conn):
            conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?,?,?)", (chat_id, key, now))
    if first_time:
        item = {"mint": mint, "sym": first["sym"], "kind": first["kind"],
                "pushed_at": first["ts"], "note": note, "minutes_since_push": (now - first["ts"]) // 60}
        webhooks.fire(conn, "void", item, now)
        discord.send(text)
        try:
            from . import xpost
            xpost.void(conn, mint, note, now)
        except Exception as e:  # noqa: BLE001 - X being down is not the void being down
            log.warning("x void for %s failed: %s", mint[:10], e)
    log.info("void %s: %d chats told, %s", mint[:10], sent, note)
    return sent


_crowd_cache: dict[str, tuple[int, dict | None]] = {}


def crowd(conn: sqlite3.Connection, mint: str, gt=None, now: int | None = None, max_age_s: int = 300) -> dict | None:
    """Who is in the pool besides us: buyers, sellers and sells in its last hour, as the screener
    counts them. None when it cannot say (a pool minutes old is often not indexed yet)."""
    now = now or db.now()
    hit = _crowd_cache.get(mint)
    if hit and now - hit[0] < max_age_s:
        return hit[1]
    row = conn.execute("SELECT pool_address, chain FROM tokens WHERE mint=?", (mint,)).fetchone()
    out = None
    if row and row["pool_address"]:
        try:
            if gt is None:
                from ..sources.geckoterminal import GeckoTerminal
                gt = GeckoTerminal(patient=False)
            a = gt.pool(row["chain"] or "robinhood", row["pool_address"])
            tx = ((a or {}).get("transactions") or {}).get("h1") or {}
            if a and tx:
                out = {"buys": int(tx.get("buys") or 0), "sells": int(tx.get("sells") or 0),
                       "buyers": int(tx.get("buyers") or 0), "sellers": int(tx.get("sellers") or 0),
                       "vol_h1": a.get("vol_h1")}
        except Exception as e:  # noqa: BLE001 - the screener being busy is not a verdict
            log.debug("crowd lookup for %s failed: %s", mint[:10], e)
    _crowd_cache[mint] = (now, out)
    return out


_share_cache: dict[str, tuple[int, float | None]] = {}
SHARE_WINDOW_S = 1800   # the same half hour scripts/crowd_share.py measured the ledger with


def cohort_share(conn: sqlite3.Connection, mint: str, cohort_usd: float | None, now: int | None = None, gt=None,
                 rpc: RobinhoodRPC | None = None, max_age_s: int = 300) -> float | None:
    """The cohort's dollars over the pool's last half hour, 0..1, or None when nobody can say.

    A share near one is the cohort making the market; a share near zero is the cohort arriving in
    a market the crowd already made. The pool's half hour comes from the chain - its own swaps,
    which exist from the pool's first block - and from the screener's minute candles only when
    the chain will not answer: the screener takes minutes to index a new pool, and a launch is
    pushed inside those minutes (four pushes on 21 Sep, every share NULL). The same window the
    study measured the ledger with, so the floor means the same thing here. The number goes on
    the ledger and into the message either way; whether it gates a push is `hot_min_cohort_share`."""
    if not cohort_usd:
        return None
    now = now or db.now()
    hit = _share_cache.get(mint)
    if hit and now - hit[0] < max_age_s:
        return hit[1]
    row = conn.execute("SELECT pool_address, chain, created_at FROM tokens WHERE mint=?", (mint,)).fetchone()
    out, why = None, "no pool on record"
    if row and row["pool_address"]:
        vol = None
        try:
            rpc = rpc or RobinhoodRPC()
            vol = rpc.pool_volume_usd(row["pool_address"], now - SHARE_WINDOW_S, row["created_at"])
            why = "the chain has no swaps for it" if vol is None else ""
        except Exception as e:  # noqa: BLE001 - the chain saying no is the screener's turn
            why = f"chain: {str(e)[:80]}"
        if vol is None:
            try:
                if gt is None:
                    from ..sources.geckoterminal import GeckoTerminal
                    gt = GeckoTerminal(patient=False)
                c = gt.ohlcv(row["chain"] or "robinhood", row["pool_address"], "minute", 1, SHARE_WINDOW_S // 60 + 5) or []
                vol = sum(x[5] for x in c if len(x) > 5 and x[0] >= now - SHARE_WINDOW_S) or None
                why = why if vol is None else ""
            except Exception as e:  # noqa: BLE001 - the screener being busy is not a verdict
                why = f"{why}; screener: {str(e)[:60]}"
        if vol is not None and vol > 0:
            out = round(min(1.0, cohort_usd / vol), 3)
    if out is None:
        log.warning("cohort share of %s unmeasured: %s", mint[:10], why)
    _share_cache[mint] = (now, out)
    return out


def crowd_objection(share: float | None) -> str | None:
    """Why the push should not go, or None: the gate on the share, when a floor is set."""
    floor = settings.hot_min_cohort_share
    if floor <= 0 or share is None or share >= floor:
        return None
    return f"the cohort is {share:.0%} of the pool's last hour, under {floor:.0%}: following the crowd, not leading it"


def only_the_cohort(conn: sqlite3.Connection, mint: str, wallets: int, now: int | None = None, gt=None) -> str | None:
    """Why a young token should wait, or None if it need not.

    A token the cohort found minutes ago is pushed only when the pool shows somebody else in it:
    one sell by anyone, or more buyers than the entrants we counted. Thirteen buys into thirteen
    wallets and not one sell is the seeder's market, not a market. An older token, or one the
    screener has not indexed yet, is not held on this.
    """
    now = now or db.now()
    first = conn.execute("SELECT MIN(ts) FROM trades WHERE mint=? AND side='buy'", (mint,)).fetchone()[0]
    if first is None or now - first >= settings.hot_young_s:
        return None
    c = crowd(conn, mint, gt=gt, now=now)
    if not c:
        return None
    if c["sells"] == 0 and c["buyers"] <= wallets:
        return f"{(now - first) // 60} min old and nobody in the pool but the {c['buyers']} buyers we counted, no sell yet"
    return None


def sweep(conn: sqlite3.Connection, limit: int = 12, now: int | None = None, rpc=None, gt=None) -> dict:
    """Ask again about the tokens the cohort touched today whose verdict is missing or stale.

    Bounded per pass, longest-unasked first, so the cost stays flat: a dozen eth_calls and at most
    a dozen screener requests every fifteen minutes.
    """
    now = now or db.now()
    # a no from the pool's silence is asked again here too: every feed has dropped the token by
    # then, so nothing else will, and a first sell is the only way back in
    rows = conn.execute(
        "SELECT tk.mint FROM tokens tk WHERE tk.mint IN ("
        "  SELECT tr.mint FROM trades tr JOIN traders t ON t.address = tr.address "
        "  WHERE tr.side = 'buy' AND tr.ts >= ? AND t.score >= 60) "
        "AND (COALESCE(tk.sellable, 1) != 0 OR tk.sell_note LIKE ?) AND COALESCE(tk.sell_checked_at, 0) < ? "
        "ORDER BY COALESCE(tk.sell_checked_at, 0) LIMIT ?",
        (now - 86400, f"%{SILENCE}%", now - 1800, limit)).fetchall()
    stats = {"asked": 0, "unsellable": 0, "sellable": 0, "unknown": 0}
    for r in rows:
        v = check(conn, r["mint"], rpc=rpc, gt=gt, now=now, force=True)
        stats["asked"] += 1
        stats["unsellable" if v["sellable"] == 0 else "sellable" if v["sellable"] == 1 else "unknown"] += 1
        time.sleep(0.2)
    return stats
