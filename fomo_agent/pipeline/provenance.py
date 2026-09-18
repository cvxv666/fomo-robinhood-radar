"""Whose trade a fill really is.

On 2026-09-11 a burst fired on a token seventeen trusted wallets had "bought" inside minutes.
None of them had. Four outside keys had called the fomo router directly, paid with their own ETH,
and named a trusted wallet as the recipient of each swap — and to a tracker that reads "token
arrived from the router" as "wallet bought", that is indistinguishable from the cohort piling in.
The same day a second token sat at the top of the signal feed on twenty-seven "buys" of fifty
cents each, delivered to eighteen trusted wallets through fomo's own flow. Thirty-five dollars.

Every fill on the chain is signed by a relayer rather than by the wallet — that is how fomo
works, 142 signers in a day — so who signed is no help. What is:

  · `direct`  the transaction was sent to the router itself rather than to fomo's entrypoint.
    Structural, from the receipt: a fomo-app trade never looks like this. Such a fill is real
    market activity and is not that wallet's trade; it counts for nothing.
  · `dust`    a buy smaller than max(an absolute floor, a share of the wallet's own median buy).
    Traders do make small probes, so this is stored and shown, and left out of conviction.
  · `trade`   everything else, which is everything a fomo user actually did.

  · `seed`    a fill inside a wave: one buy apiece into a queue of trusted wallets, seconds
    apart (see `waves`). Judged by shape, because on 17 Sep the size was raised to $30 and the
    route was fomo's own, and on chain that is a real buy in every field.

And the rule that turns the attack on itself: a token where several trusted wallets received
direct, dust or seed buys inside a day is *seeded*, and leaves every feed. The more wallets a
seeder touches to look like a cohort, the more certainly the token disappears.

A NULL kind is a row from before any of this existed. It passes as a trade until `verify-fills`
has fetched its receipt, which is a bounded backlog and not a permanent state.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import db
from ..config import settings
from .analyze import TRUSTED

log = logging.getLogger(__name__)


def refresh_medians(conn: sqlite3.Connection, days: int = 30) -> int:
    """Each tracked wallet's median buy over the window, stored on the trader row.

    Direct fills are left out; dust is left in, because on the first pass nothing is dust yet and
    a wallet that mostly probes is a wallet whose probes are its size.
    """
    since = db.now() - days * 86400
    rows = conn.execute(
        "SELECT address, usd_value FROM trades WHERE side='buy' AND usd_value > 0 AND ts >= ? "
        "AND COALESCE(kind,'trade') != 'direct' ORDER BY address, usd_value", (since,)).fetchall()
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["address"], []).append(r["usd_value"])
    with db.tx(conn):
        for address, sizes in by.items():
            conn.execute("UPDATE traders SET median_buy_usd=? WHERE address=?",
                         (sizes[len(sizes) // 2], address))
    return len(by)


def classify(conn: sqlite3.Connection, since: int) -> dict:
    """Judge the size of every flow fill since `since` that has not been judged yet.

    `flow` is what the scanner writes for a fill that came through fomo; this settles it into
    `trade` or `dust`. A wallet with no median on record yet is judged against the floor alone.
    """
    floor, ratio = settings.dust_abs_usd, settings.dust_ratio
    with db.tx(conn):
        dust = conn.execute(
            "UPDATE trades SET kind='dust' WHERE kind='flow' AND side='buy' AND ts >= ? "
            "AND COALESCE(usd_value, 0) < MAX(?, ? * COALESCE("
            "  (SELECT median_buy_usd FROM traders WHERE traders.address = trades.address), 0))",
            (since, floor, ratio)).rowcount
        trade = conn.execute(
            "UPDATE trades SET kind='trade' WHERE kind='flow' AND ts >= ?", (since,)).rowcount
    return {"dust": dust, "trade": trade, "waves": len(waves(conn, since))}


def waves(conn: sqlite3.Connection, since: int, now: int | None = None, dry: bool = False) -> list[dict]:
    """The seeder's queue, caught by its shape rather than its size.

    On 2026-09-17, 21:09 to 22:05, four tokens each received one buy apiece into thirteen
    top-scored wallets, in the same order, six seconds apart, all inside two minutes of the
    pool's first fill: $30 each on the first two, $32-$94 on the third, $32-$299 on the fourth.
    Through fomo's own flow, from fomo's own treasury, the recipient in the calldata exactly where
    a real buy carries it - on chain the two are the same transaction. At $30 the fills beat the
    dust bar for eight of the thirteen, so a burst fired on each token, and then a launch.

    What a script cannot hide is that it is a script. A cohort arrives over minutes, in mixed
    sizes, and the ones who mean it buy twice; a seeder's targets each get one fill, in a queue,
    seconds apart. So: among trusted wallets whose only buy on a token landed inside the window,
    a run of `seed_wave_min_wallets` first buys with no gap wider than `seed_wave_max_gap_s`,
    none of them over `seed_wave_max_usd`, is a wave. Every trusted buy of that size on the
    token around the run becomes `seed` - the second $30 a target got as well - which counts
    for nothing anywhere, and under the seeded rule the token leaves every feed until real
    buyers outnumber the targets. `dry` reports without marking, for a replay.
    """
    now = now or db.now()
    n_min, max_gap, max_usd = settings.seed_wave_min_wallets, settings.seed_wave_max_gap_s, settings.seed_wave_max_usd
    rows = conn.execute(
        "SELECT tr.mint mint, tr.address address, MIN(tr.ts) ts, COUNT(*) n, MAX(COALESCE(tr.usd_value, 0)) usd "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.side = 'buy' AND t.score >= ? AND COALESCE(tr.kind, 'trade') NOT IN ('direct', 'seed') "
        "AND tr.mint IN (SELECT mint FROM trades WHERE side = 'buy' AND ts >= ? AND ts <= ?) "
        "GROUP BY tr.mint, tr.address HAVING ts >= ? AND ts <= ?",
        (TRUSTED, since, now, since, now)).fetchall()
    by_mint: dict[str, list[tuple[int, str, float]]] = {}
    for r in rows:
        if r["n"] == 1 and r["usd"] <= max_usd:
            by_mint.setdefault(r["mint"], []).append((r["ts"], r["address"], r["usd"]))
    out = []
    for mint, cands in by_mint.items():
        cands.sort()
        run: list[tuple[int, str, float]] = []
        for c in cands + [(None, "", 0.0)]:
            if run and (c[0] is None or c[0] - run[-1][0] > max_gap):
                if len(run) >= n_min:
                    sizes = sorted(u for _, _, u in run)
                    out.append({"mint": mint, "wallets": len(run), "first_ts": run[0][0], "last_ts": run[-1][0],
                                "span_s": run[-1][0] - run[0][0], "median_usd": sizes[len(sizes) // 2],
                                "who": [a for _, a, _ in run]})
                run = []
            if c[0] is not None:
                run.append(c)
    if not dry:
        with db.tx(conn):
            for w in out:
                # the queue is preceded by two or three larger fills into the best-known wallets
                # (unipcs $299, DumbCrayonEater $149, a minute or three before the $30s): the
                # window reaches five minutes back for them
                w["marked"] = conn.execute(
                    "UPDATE trades SET kind='seed' WHERE mint = ? AND side = 'buy' AND ts BETWEEN ? AND ? "
                    "AND COALESCE(usd_value, 0) <= ? AND COALESCE(kind, 'trade') != 'direct' "
                    "AND address IN (SELECT address FROM traders WHERE score >= ?)",
                    (w["mint"], w["first_ts"] - 300, w["last_ts"] + 60, max_usd, TRUSTED)).rowcount
                log.warning("wave on %s: %d trusted wallets, one fill each, %ds apart in all, median $%.0f - %d fills marked seed",
                            w["mint"][:10], w["wallets"], w["span_s"], w["median_usd"], w["marked"])
    return out


# Any query that counts buys toward a signal takes both of these.
REAL = " AND COALESCE({t}.kind, 'trade') = 'trade'"
# Seeded: enough trusted wallets received pushed fills, and they outnumber the ones that bought for
# real. Both halves matter. The first is the pattern; the second is what keeps the pattern from
# being turned around and used to hide a real token with a few dollars of dust.
# Unsellable: the token said no to a sell, or nobody has managed one. Settled by safety.check
# and kept on the token, so a feed only has to look it up.
NOT_UNSELLABLE = " AND {t}.mint NOT IN (SELECT mint FROM tokens WHERE sellable = 0)"
NOT_SEEDED = (
    " AND {t}.mint NOT IN ("
    "  SELECT s.mint FROM trades s JOIN traders st ON st.address = s.address"
    "  WHERE s.side = 'buy' AND st.score >= ? AND s.ts >= ?"
    "  GROUP BY s.mint"
    "  HAVING COUNT(DISTINCT CASE WHEN s.kind IN ('dust', 'direct', 'seed') THEN s.address END) >= ?"
    "     AND COUNT(DISTINCT CASE WHEN s.kind IN ('dust', 'direct', 'seed') THEN s.address END)"
    "       > COUNT(DISTINCT CASE WHEN COALESCE(s.kind, 'trade') = 'trade' THEN s.address END))"
)


def seeded_params(now: int | None = None) -> list:
    """The three placeholders NOT_SEEDED opens, in order."""
    return [TRUSTED, (now or db.now()) - settings.seed_window_h * 3600, settings.seed_min_wallets]


def seeded(conn: sqlite3.Connection, mint: str, now: int | None = None) -> dict:
    """How this token has been seeded, for the page that shows it."""
    now = now or db.now()
    row = conn.execute(
        "SELECT COUNT(DISTINCT CASE WHEN s.kind IN ('dust','direct','seed') THEN s.address END) wallets, "
        "  COUNT(DISTINCT CASE WHEN COALESCE(s.kind,'trade') = 'trade' THEN s.address END) real, "
        "  SUM(s.kind = 'dust') dust, SUM(s.kind = 'direct') direct, SUM(s.kind = 'seed') seed, "
        "  MIN(CASE WHEN s.kind IN ('dust','direct','seed') THEN s.ts END) first_ts "
        "FROM trades s JOIN traders st ON st.address = s.address "
        "WHERE s.mint = ? AND s.side = 'buy' AND st.score >= ? AND s.ts >= ?",
        (mint, TRUSTED, now - settings.seed_window_h * 3600)).fetchone()
    wallets, real = row["wallets"] or 0, row["real"] or 0
    return {"wallets": wallets, "real": real, "dust": row["dust"] or 0, "direct": row["direct"] or 0,
            "seed": row["seed"] or 0, "first_ts": row["first_ts"],
            "seeded": wallets >= settings.seed_min_wallets and wallets > real}


def resize(conn: sqlite3.Connection) -> dict:
    """Judge every sized fill again under the current floor and ratio. For when the bar moves."""
    with db.tx(conn):
        conn.execute("UPDATE trades SET kind='flow' WHERE kind IN ('dust', 'trade', 'seed')")
    return classify(conn, since=0)


def verify(conn: sqlite3.Connection, days: int = 7, rpc=None, max_per_min: int | None = None,
           limit: int | None = None) -> dict:
    """Fetch the receipt of every unjudged buy in the window and settle its kind.

    Forty receipts a request; a week of buys is a few hundred requests. Runs beside the watcher
    on a share of the allowance rather than all of it, and stops cleanly when the endpoint says
    no, leaving the rest for the next run — the rows it did not reach are still NULL, still
    passing as trades, still on the list.
    """
    from ..sources.rpc import RobinhoodRPC, RpcError

    rpc = rpc or RobinhoodRPC()
    if max_per_min:
        rpc.limiter.max = max_per_min
    routers = {r.lower() for r in settings.rpc_routers}
    since = db.now() - days * 86400
    rows = conn.execute(
        "SELECT sig FROM trades WHERE kind IS NULL AND side='buy' AND chain='robinhood' AND ts >= ? "
        "ORDER BY ts DESC" + (f" LIMIT {int(limit)}" if limit else ""), (since,)).fetchall()
    txs = sorted({r["sig"].split(":")[0] for r in rows})
    stats = {"pending": len(txs), "checked": 0, "direct": 0, "flow": 0, "stopped": None}
    t0 = time.time()
    for i in range(0, len(txs), settings.rpc_batch_size):
        chunk = txs[i:i + settings.rpc_batch_size]
        # The endpoint's limit is shared with the watcher and the collector, and it says no from
        # time to time whatever allowance this runs on. A no is a pause, not the end: wait a
        # minute and ask again, and only give up when it has said no six times in a row.
        for attempt in range(6):
            try:
                receipts = rpc.batch("eth_getTransactionReceipt", [[tx] for tx in chunk])
                break
            except RpcError as e:
                stats["stopped"] = str(e)
                time.sleep(60)
        else:
            break
        stats["stopped"] = None
        with db.tx(conn):
            for tx, rc in zip(chunk, receipts):
                if not rc:
                    continue
                kind = "direct" if (rc.get("to") or "").lower() in routers else "flow"
                # the chain tracker suffixes a log index onto the hash; the older Codex rows are
                # the bare hash, and both are the same transaction
                conn.execute("UPDATE trades SET kind=? WHERE (sig = ? OR sig LIKE ?) AND kind IS NULL",
                             (kind, tx, tx + ":%"))
                stats[kind] += 1
                stats["checked"] += 1
    stats.update(classify(conn, since))
    stats["seconds"] = round(time.time() - t0)
    log.info("verify-fills: %s", stats)
    return stats
