"""What is quietly broken.

Every failure mode this project has hit looked the same from outside: the site kept serving, the
pages kept rendering, and the numbers stopped moving. Nothing 500s when fomo's session expires or
when the router contract changes — the tape simply goes flat, and a flat tape reads exactly like a
quiet market until somebody notices the dates.

So the checks here are about *staleness and silence* rather than errors. Each one knows what it
would look like if the thing it watches had stopped, and says so in a sentence a person can act on.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import httpx

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, RobinhoodRPC
from .collect_api import spent_this_month

log = logging.getLogger(__name__)


def _age_h(ts: int | None) -> float | None:
    return None if not ts else (db.now() - ts) / 3600


def router_alive(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> dict:
    """Fills stopped, but did the chain?

    `RPC_ROUTERS` is one hardcoded contract. If fomo moves to another one, every fill vanishes and
    nothing anywhere errors: the feed empties, the pages keep working, and the whole product
    quietly becomes a museum. The test that separates "they changed the router" from "nobody
    traded today" is whether the wallets have any ERC-20 traffic at all while producing no fills.
    """
    fills = conn.execute("SELECT COUNT(*) FROM trades WHERE chain = ? AND ts >= ?",
                         (CHAIN, db.now() - 86400)).fetchone()[0]
    if fills:
        return {"name": "router", "ok": True, "detail": f"{fills} fills in the last day"}

    wallets = [r["address"] for r in conn.execute(
        "SELECT address FROM traders WHERE chain = ? AND status IN ('active','watch') LIMIT 60",
        (CHAIN,))]
    if not wallets:
        return {"name": "router", "ok": True, "detail": "nothing tracked on this chain yet"}
    try:
        rpc = rpc or RobinhoodRPC()
        head = rpc.block_number()
        moved = rpc.transfers(wallets, max(head - settings.rpc_window_blocks, 0), head,
                              outgoing=False)
    except Exception as e:  # noqa: BLE001 - a check that cannot run is not a failing check
        return {"name": "router", "ok": True, "detail": f"could not ask the chain: {e}"}

    if not moved:
        return {"name": "router", "ok": True,
                "detail": "no fills, but no transfers either - the cohort is simply still"}
    return {"name": "router", "ok": False,
            "detail": (f"no fills in a day while {len(moved)} transfers moved through these "
                       f"wallets. RPC_ROUTERS ({', '.join(settings.rpc_routers)}) is probably "
                       "stale - find the new one in a recent fomo trade.")}


def checks(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> list[dict]:
    """Every check, worst first. `ok` False is something a person has to do."""
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    # A delivery that brought nothing is not a collection. Either route writes a run row whatever
    # happened — a signed-out browser, a rejected key, an exhausted month — and without the second
    # clause this check would go on reporting a healthy collection while nothing at all came in.
    # Requiring a leaderboard in the payload is what makes the check mean what it says.
    fomo_age = _age_h(one(
        "SELECT MAX(finished_at) FROM runs WHERE kind IN ('fomoapi', 'fomo_ingest') "
        "AND error IS NULL AND stats_json LIKE '%\"24h\":%'"))
    month = spent_this_month(conn)
    cap = settings.fomoapi_monthly_credits
    fills_age = _age_h(one("SELECT MAX(ts) FROM trades"))
    holdings_age = _age_h(one("SELECT MAX(ts) FROM holdings"))
    unscored = one("SELECT COUNT(*) FROM traders WHERE score IS NULL AND status IN "
                   "('tracking','active','watch')")
    unresolved = one("SELECT COUNT(*) FROM fomo_users WHERE onchain_address IS NULL")
    resolvable = one("SELECT COUNT(*) FROM fomo_users u WHERE u.onchain_address IS NULL AND EXISTS "
                     "(SELECT 1 FROM fomo_swaps s WHERE s.user_id = u.user_id)")
    priced = one("SELECT COUNT(*) FROM tokens WHERE price_usd IS NOT NULL")
    tokens = one("SELECT COUNT(*) FROM tokens")
    # Six thousand collect errors in a day raised no flag: a roster scan the node refused shows
    # up as a pass with four hundred wallet errors and no wallets read, and every pass writes its
    # stats into the run ledger. The share of such passes in six hours is the check.
    now = db.now()
    track = conn.execute("SELECT stats_json FROM runs WHERE kind='track' AND started_at >= ? AND finished_at IS NOT NULL",
                         (now - 6 * 3600,)).fetchall()
    lost = 0
    for r in track:
        try:
            st = json.loads(r[0] or "{}")
        except ValueError:
            st = {}
        if (st.get("errors") or 0) > (st.get("wallets") or 0):
            lost += 1
    # The watcher keeps no ledger, but the tape it writes does: the roster fills every minute of
    # the day, so a quarter of an hour with no fill at all is a quarter of an hour it was blind.
    stamps = [r[0] for r in conn.execute("SELECT ts FROM trades WHERE ts >= ? ORDER BY ts", (now - 6 * 3600,))]
    gap = max((b - a for a, b in zip(stamps, stamps[1:])), default=0) if len(stamps) > 20 else 0

    out = [
        router_alive(conn, rpc),
        # The hint belongs on the failure only: a passing check that ends with a warning is how a
        # report trains people to skim past it.
        {"name": "fomo collection", "ok": fomo_age is not None and fomo_age < 3,
         "detail": (f"last collection {fomo_age:.1f}h ago" if fomo_age is not None
                    else "never collected") + ("" if fomo_age is not None and fomo_age < 3
                    else " - check `journalctl -u radar-fomo`; a rejected key and an exhausted "
                         "month both look like this")},
        # The one failure that arrives on a schedule rather than by surprise, so it is worth
        # saying while there is still time to do something about it.
        {"name": "fomoapi budget", "ok": not cap or month < cap * 0.9,
         "detail": f"{month:.0f} of {cap} credits used this month"},
        {"name": "on-chain tape", "ok": fills_age is not None and fills_age < 6,
         "detail": f"newest fill {fills_age:.1f}h ago" if fills_age is not None else "no fills"},
        {"name": "holdings", "ok": holdings_age is not None and holdings_age < 6,
         "detail": (f"balances read {holdings_age:.1f}h ago" if holdings_age is not None
                    else "never read")},
        {"name": "scoring queue", "ok": unscored < 25,
         "detail": f"{unscored} tracked wallets waiting for a verdict"},
        {"name": "prices", "ok": tokens == 0 or priced / tokens > 0.6,
         "detail": f"{priced} of {tokens} tokens priced"},
        {"name": "collect scans", "ok": not track or lost / len(track) <= 0.3,
         "detail": f"{lost} of {len(track)} track passes in 6h lost their roster scan"
                   + ("" if not track or lost / len(track) <= 0.3 else
                      " - the node is refusing eth_getLogs; see `journalctl -u radar-collect | grep 'scan failed'`")},
        {"name": "watcher gaps", "ok": gap < 900,
         "detail": (f"longest silence on the tape in 6h: {gap // 60} min" if len(stamps) > 20 else "too few fills to judge")
                   + ("" if gap < 900 else " - the watcher was blind that long; `journalctl -u radar-watch | grep 'rpc says'`")},
        {"name": "wallet resolution", "ok": True,
         "detail": f"{unresolved} fomo users still without an on-chain address, "
                   f"{resolvable} of them with swaps to try, {unresolved - resolvable} with none"},
    ]
    return sorted(out, key=lambda c: c["ok"])


def report(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> dict:
    rows = checks(conn, rpc)
    bad = [c for c in rows if not c["ok"]]
    log.info("health: %d checks, %d failing", len(rows), len(bad))
    return {"checks": rows, "failing": len(bad), "ok": not bad}


def heartbeat(ok: bool, url: str | None = None, timeout: float = 8.0) -> str:
    """Tell the outside world the pipeline is alive, or deliberately stop telling it.

    Healthchecks.io and its kind read silence as failure, so this pings only while everything
    passes, and posts to `<url>/fail` when it does not. A broken pipeline therefore raises the same
    alarm as a dead host — which is right, because from a reader's side they are the same event.

    Never raises: a monitoring call that can take the process down with it is worse than no
    monitoring, and this runs on the same timer as the checks themselves.
    """
    url = settings.heartbeat_url if url is None else url
    if not url:
        return "no HEARTBEAT_URL set - nothing is watching from outside"
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(target)
        return f"pinged {'ok' if ok else 'fail'} ({r.status_code})"
    except Exception as e:  # noqa: BLE001 - monitoring must never break the thing it monitors
        log.warning("heartbeat failed: %s", e)
        return f"heartbeat unreachable: {str(e)[:80]}"

