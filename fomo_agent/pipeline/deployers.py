"""Who made the token, and what became of the ones they made before.

The four seeded tokens of 17 Sep were plain contract creations sent by three outside keys, two of
them by the same key half an hour apart. A real launch mostly comes through fomo's factory, with
the creator's wallet in the calldata; a token minted to itself by a fresh key is a different
animal. Either way the chain says who: the first Transfer from the zero address is the mint, its
transaction is the creation, and the creation's sender - or, through a factory, the first wallet
the launch contract pays out to, which on fomo is the creator's own first buy - is the creator.

The creator is stored on the token and read like a reputation: a push on a young token whose
creator's previous tokens were seeded, unsellable or dead inside the week is not sent, and the
token page says why. The first token of a key is judged on its own; the second pays for the first.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings

log = logging.getLogger(__name__)

ZERO_TOPIC = "0x" + "0" * 64
CREATE_SELECTORS = ("0x60806040", "0x60a06040", "0x60c06040")   # solc constructor prologues
BACK_BLOCKS = 20_000      # ~35 minutes of Robinhood Chain before the cohort's first fill
SPAN = 9_000              # under the node's log cap for one busy contract


def find(rpc, conn: sqlite3.Connection, mint: str) -> dict | None:
    """The creator of a token from the chain, or None when the mint cannot be found nearby.
    Two or three requests: the logs around the cohort's first fill, the creation transaction."""
    from ..sources.rpc import TRANSFER_TOPIC

    first_ts = conn.execute("SELECT MIN(ts) FROM trades WHERE mint=?", (mint,)).fetchone()[0]
    if not first_ts:
        return None
    approx = rpc.block_at(first_ts)
    head = rpc.block_number()
    logs = []
    lo = max(approx - BACK_BLOCKS, 1)
    while lo <= min(approx + 2_000, head) and not logs:
        logs = rpc.logs(lo, min(lo + SPAN - 1, head), address=mint, topics=[TRANSFER_TOPIC, ZERO_TOPIC])
        lo += SPAN
    if not logs:
        return None
    lg = logs[0]
    tx = rpc.call("eth_getTransactionByHash", [lg["transactionHash"]]) or {}
    rc = rpc.call("eth_getTransactionReceipt", [lg["transactionHash"]]) or {}
    inp = (tx.get("input") or "")
    to = (rc.get("to") or tx.get("to") or "")
    sender = (tx.get("from") or "").lower()
    if not to or inp[:10] in CREATE_SELECTORS:
        # a plain deployment: the key that sent it made the token
        return {"creator": sender, "via": "deploy", "block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"]}
    # through a factory: the calldata names contracts and a relayer, not the person. On fomo the
    # creator's own first buy is the first transfer out of the launch contract to a wallet, so
    # the first recipient that is not infrastructure is taken as the creator.
    from ..sources.rpc import parse_transfer

    minted_to = "0x" + lg["topics"][2][-40:]
    infra = {minted_to.lower(), to.lower(), mint.lower(), "0x" + "0" * 40, *(r.lower() for r in settings.rpc_routers)}
    block = int(lg["blockNumber"], 16)
    later = rpc.logs(block, min(block + 2_000, head), address=mint, topics=[TRANSFER_TOPIC])
    for t in (parse_transfer(e) for e in later[:60]):
        if not t:
            continue
        infra.add(t["frm"])          # anything that pays out is a contract, not a person
        if t["to"] not in infra and t["to"] != t["frm"]:
            return {"creator": t["to"].lower(), "via": "factory", "factory": to.lower(), "block": block, "tx": lg["transactionHash"]}
    return {"creator": "", "via": "factory", "factory": to.lower(), "block": block, "tx": lg["transactionHash"]}


def ensure(conn: sqlite3.Connection, mint: str, rpc=None, now: int | None = None) -> str | None:
    """The token's creator, from storage or from the chain, stored either way (an empty string
    when the chain could not say, so it is not asked again every tick)."""
    row = conn.execute("SELECT creator FROM tokens WHERE mint=?", (mint,)).fetchone()
    if row is None:
        return None
    if row["creator"] is not None:
        return row["creator"] or None
    try:
        if rpc is None:
            from ..sources.rpc import RobinhoodRPC
            rpc = RobinhoodRPC()
        found = find(rpc, conn, mint)
    except Exception as e:  # noqa: BLE001 - the chain being busy is not a verdict
        log.debug("creator of %s: %s", mint[:10], e)
        return None
    with db.tx(conn):
        conn.execute("UPDATE tokens SET creator=?, created_via=?, creator_at=? WHERE mint=?",
                     (found["creator"] if found else "", found["via"] if found else None, now or db.now(), mint))
    return (found["creator"] or None) if found else None


def history(conn: sqlite3.Connection, creator: str, before_mint: str | None = None, days: int | None = None) -> dict:
    """What the creator's other tokens came to: how many, and how many were seeded, unsellable
    or dead (no trade in the hour after an alert)."""
    if not creator:
        return {"tokens": 0, "seeded": 0, "unsellable": 0, "dead": 0, "bad": 0, "symbols": []}
    days = days or settings.deployer_days
    since = db.now() - days * 86400
    rows = conn.execute(
        "SELECT t.mint, t.symbol, t.sellable, "
        "  EXISTS(SELECT 1 FROM trades s WHERE s.mint = t.mint AND s.kind = 'seed') seeded, "
        "  EXISTS(SELECT 1 FROM pushes p WHERE p.mint = t.mint AND p.followup_at IS NOT NULL AND COALESCE(p.vol_usd, 99999) < 2000) dead "
        "FROM tokens t WHERE t.creator = ? AND t.mint != ? AND COALESCE(t.first_seen_at, 0) >= ?",
        (creator, before_mint or "", since)).fetchall()
    seeded = sum(1 for r in rows if r["seeded"])
    unsellable = sum(1 for r in rows if r["sellable"] == 0)
    dead = sum(1 for r in rows if r["dead"])
    bad = sum(1 for r in rows if r["seeded"] or r["sellable"] == 0 or r["dead"])
    return {"tokens": len(rows), "seeded": seeded, "unsellable": unsellable, "dead": dead, "bad": bad,
            "symbols": [r["symbol"] or r["mint"][:8] for r in rows if r["seeded"] or r["sellable"] == 0 or r["dead"]][:5]}


def objection(conn: sqlite3.Connection, mint: str, rpc=None, now: int | None = None) -> str | None:
    """Why this token should not be pushed on its creator's account, or None."""
    creator = ensure(conn, mint, rpc, now)
    if not creator:
        return None
    h = history(conn, creator, before_mint=mint)
    if h["bad"] >= settings.deployer_max_bad:
        what = ", ".join(h["symbols"])
        return f"its creator {creator[:10]} made {h['tokens']} other token{'s' if h['tokens'] != 1 else ''} this week and {h['bad']} went seeded, unsellable or dead ({what})"
    return None


def sweep(conn: sqlite3.Connection, limit: int = 20, now: int | None = None, rpc=None) -> dict:
    """Name the creators of the tokens the cohort bought today that have none yet, a few per
    pass, so the reputation exists before the next push needs it."""
    now = now or db.now()
    rows = conn.execute(
        "SELECT DISTINCT tk.mint FROM tokens tk JOIN trades tr ON tr.mint = tk.mint JOIN traders t ON t.address = tr.address "
        "WHERE tk.creator IS NULL AND tr.side = 'buy' AND tr.ts >= ? AND t.score >= 60 AND (tk.chain IS NULL OR tk.chain = 'robinhood') "
        "ORDER BY tr.ts DESC LIMIT ?", (now - 86400, limit)).fetchall()
    stats = {"asked": 0, "found": 0}
    for r in rows:
        stats["asked"] += 1
        if ensure(conn, r["mint"], rpc, now):
            stats["found"] += 1
    return stats
