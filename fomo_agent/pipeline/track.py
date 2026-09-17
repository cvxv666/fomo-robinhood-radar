"""On-chain tracking: pull swaps for tracked wallets.

Sources are tried in the order given by TRACK_SOURCES; the first one that supports the
wallet's chain wins:

  rpc     - Robinhood Chain only, free and keyless: two eth_getLogs calls against the chain's
            own public endpoint cover the whole roster, whatever its size.
  trenches- Robinhood Chain only, free and keyless: one tape request covers every wallet in a
            pass, but only for the ~108 wallets that site curates.
  codex   - every chain we watch (solana/base/robinhood), returns USD per trade.
            Costs >=1 request per wallet per pass out of 10k/month, so keep
            TRACK_MAX_WALLETS_PER_PASS and the loop interval sane.
  helius  - Solana only, needs HELIUS_API_KEY. Free tier is 1M credits/month and an
            Enhanced Transactions call costs 100 credits, i.e. ~10k calls/month.

A wallet whose chain no source supports is counted in `unsupported` and left alone.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Protocol

from .. import db
from ..config import settings
from ..models import Trade

log = logging.getLogger(__name__)
TRACKED = ("candidate", "tracking", "active", "watch")


class Tracker(Protocol):
    def supports(self, chain: str) -> bool: ...
    def get_trades(self, address: str, chain: str = "solana", since_ts: int | None = None) -> list[Trade]: ...


class HeliusTracker:
    """Adapter so the Solana-only Helius client fits the Tracker protocol."""

    def __init__(self, client=None):
        from ..sources.helius import HeliusClient

        self.client = client or HeliusClient()

    def supports(self, chain: str) -> bool:
        return chain == "solana"

    def get_trades(self, address: str, chain: str = "solana", since_ts: int | None = None) -> list[Trade]:
        return self.client.get_swaps(address, since_ts=since_ts)


def build_trackers(names: tuple[str, ...] | None = None) -> list[Tracker]:
    """Instantiate the configured sources, skipping any that cannot start (missing key)."""
    out: list[Tracker] = []
    for name in names or settings.track_sources:
        try:
            if name == "rpc":
                from ..sources.rpc import RobinhoodRPC

                out.append(RobinhoodRPC())
            elif name == "trenches":
                from ..sources.trenches import Trenches

                out.append(Trenches())
            elif name == "codex":
                from ..sources.codex import Codex

                out.append(Codex())
            elif name == "helius":
                out.append(HeliusTracker())
            else:
                log.warning("unknown track source %r", name)
        except Exception as e:  # noqa: BLE001 - a missing key must not break the others
            log.info("track source %s unavailable: %s", name, e)
    if not out:
        raise RuntimeError(
            "no wallet-trade source available. Set CODEX_API_KEY (dashboard.codex.io, $1) "
            "or HELIUS_API_KEY (free tier at helius.dev), then check TRACK_SOURCES."
        )
    return out


def pick_tracker(trackers: list[Tracker], chain: str, address: str | None = None) -> Tracker | None:
    """First source that handles this chain — and, when it only indexes a fixed roster, this wallet."""
    for t in trackers:
        if not t.supports(chain):
            continue
        covers = getattr(t, "covers", None)
        if address and covers and not covers(address):
            continue
        return t
    return None


def track_wallet(conn: sqlite3.Connection, tracker: Tracker, address: str, chain: str = "solana",
                 *, backfill_days: int | None = None) -> int:
    """Collect new trades since the last stored one (or the lookback window). Returns inserted count."""
    since = db.last_trade_ts(conn, address)
    if since is None:
        since = db.now() - (backfill_days or settings.track_lookback_days) * 86400
    trades = tracker.get_trades(address, chain, since_ts=since)
    inserted = 0
    with db.tx(conn):
        for t in trades:
            inserted += db.insert_trade(conn, **t.model_dump())
        conn.execute("UPDATE traders SET last_tracked_ts=? WHERE address=?", (db.now(), address))
        row = db.get_trader(conn, address)
        if row and row["status"] == "candidate":
            db.set_status(conn, address, "tracking")
    return inserted


def track_all(conn: sqlite3.Connection, trackers: list[Tracker] | None = None, limit: int | None = None) -> dict:
    trackers = trackers or build_trackers()
    rows = db.traders_by_status(conn, *TRACKED)
    # least-recently-tracked first, so a large candidate pool rotates fairly under the request budget
    rows.sort(key=lambda r: r["last_tracked_ts"] or 0)
    rows = rows[: (limit or settings.track_max_wallets_per_pass)]
    # batch sources index a whole roster in one shot; tell them which wallets this pass needs
    for t in trackers:
        prime = getattr(t, "prime", None)
        if prime:
            try:
                prime([r["address"] for r in rows if t.supports(r["chain"] or "solana")])
            except Exception as e:  # noqa: BLE001 - a source that cannot prime is simply skipped
                log.warning("prime %s failed: %s", type(t).__name__, e)
        # a source that fetches receipts is told which fills the tape already holds (the watcher
        # wrote them within a tick), so it pays only for what is new
        skip = getattr(t, "skip_known", None)
        if skip:
            try:
                since = db.now() - int(settings.rpc_window_blocks * 0.1 * 2) - 3600
                skip({r[0].split(":")[0] for r in conn.execute("SELECT sig FROM trades WHERE ts >= ? AND source='rpc'", (since,))})
            except Exception as e:  # noqa: BLE001 - without it the pass merely pays for every receipt
                log.warning("known fills for %s unavailable: %s", type(t).__name__, e)
        # a source that reads token sizes needs each token's base unit; it is a constant, so hand
        # over what earlier passes already learned rather than let it re-ask the chain
        load = getattr(t, "load_decimals", None)
        if load:
            try:
                load(db.token_decimals(conn))
            except Exception as e:  # noqa: BLE001 - the source can always ask the chain instead
                log.warning("decimals cache for %s unavailable: %s", type(t).__name__, e)
    stats = {"wallets": 0, "unsupported": 0, "trades": 0, "errors": 0, "by_source": {}}
    for r in rows:
        chain = r["chain"] or "solana"
        tracker = pick_tracker(trackers, chain, r["address"])
        if tracker is None:
            stats["unsupported"] += 1
            continue
        name = type(tracker).__name__
        try:
            n = track_wallet(conn, tracker, r["address"], chain)
            stats["wallets"] += 1
            stats["trades"] += n
            stats["by_source"][name] = stats["by_source"].get(name, 0) + 1
        except Exception as e:  # noqa: BLE001 - one bad wallet must not stop the loop
            stats["errors"] += 1
            if "scan failed earlier this pass" in str(e):
                # the roster scan failed once; every wallet after it gets the same answer, and
                # that is one fact, not four hundred warnings (1,596 lines for three lost scans)
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            log.warning("track %s (%s) failed: %s", r["address"][:8], chain, e)
    if stats.get("skipped"):
        log.warning("track: %d wallets skipped after the roster scan failed this pass", stats["skipped"])
    for t in trackers:
        learned = getattr(t, "known_decimals", None)
        if learned:
            try:
                with db.tx(conn):
                    db.save_token_decimals(conn, learned())
            except Exception as e:  # noqa: BLE001 - a cache that will not persist is still a cache
                log.warning("could not store decimals from %s: %s", type(t).__name__, e)
        used = getattr(t, "requests", None) or getattr(getattr(t, "limiter", None), "total", None)
        if used:
            stats.setdefault("requests", {})[type(t).__name__] = used
    # size every new flow fill against its wallet's own median, then move the medians on
    from .provenance import classify, refresh_medians

    stats["kinds"] = classify(conn, since=0)   # every flow fill still unsized, however old
    refresh_medians(conn)
    log.info("track: %s", stats)
    return stats
