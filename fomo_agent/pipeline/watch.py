"""Read the chain as it happens, and say so the moment a burst forms.

The fifteen-minute pass reads the last 200k blocks every time it runs, which is the right shape
for a tape that must not have holes in it and the wrong shape for an alert. FLYBRAIN's eight
wallets went in between 20:10 and 20:18; the pass that noticed finished at 20:35. Blocks land every
tenth of a second on this chain, and everything the roster did in the last twenty seconds is two
`eth_getLogs` over two hundred blocks — cheaper than one page of the site.

So this loop asks for only the blocks it has not seen, writes whatever fills they hold through
the same `insert_trade` as everything else (the fill key makes the overlap with the fifteen-minute
pass harmless), and runs the burst rule on every tick. It keeps no state across restarts on
purpose: a gap of a minute or an hour is exactly what the fifteen-minute pass exists to fill.

The RPC is the one shared resource. Two processes each allowed 45 requests a minute is 90 against
an endpoint that already answers 429 to 45 now and then, so this one runs on a smaller allowance
of its own.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, RobinhoodRPC, RpcError
from . import noise, pro
from .hot import hot_now, record
from .provenance import classify
from .track import TRACKED

log = logging.getLogger(__name__)


@dataclass
class Watch:
    rpc: RobinhoodRPC
    last_block: int = 0
    wallets: list[str] = field(default_factory=list)
    roster_at: float = 0.0
    ticks: int = 0
    fills: int = 0
    alerts: int = 0
    burns: pro.Cursor = field(default_factory=pro.Cursor)


def roster(conn: sqlite3.Connection) -> list[str]:
    return sorted({r["address"].lower() for r in db.traders_by_status(conn, *TRACKED)
                   if (r["chain"] or CHAIN) == CHAIN and r["address"].startswith("0x")})


def tick(conn: sqlite3.Connection, w: Watch, now: int | None = None) -> dict:
    """One pass: the blocks since last time, their fills, and whatever is bursting now.

    The first tick of a process starts a minute back rather than at zero — anything older is the
    scheduled pass's job — and a tick that finds itself far behind (a pause, a slow RPC) caps the
    range for the same reason: an alert about half an hour ago is not an alert.
    """
    now = now or db.now()
    if time.monotonic() - w.roster_at > settings.watch_roster_refresh_s or not w.wallets:
        w.wallets = roster(conn)
        w.roster_at = time.monotonic()
        try:
            w.rpc.load_decimals(db.token_decimals(conn))
        except Exception as e:  # noqa: BLE001 - the chain can always be asked
            log.warning("decimals cache unavailable: %s", e)

    head, _ = w.rpc.head()   # number and timestamp in one call: the scan dates its logs from it
    first = w.last_block + 1 if w.last_block else head - settings.watch_start_back_blocks
    first = max(first, head - settings.watch_max_range_blocks, 0)
    stats = {"head": head, "from": first, "blocks": 0, "fills": 0, "hot": 0, "sent": 0, "spared": getattr(w.rpc, "spared", 0)}
    if head < first:
        return stats

    fills = w.rpc.scan(w.wallets, first, head)
    new_sigs: list[str] = []
    with db.tx(conn):
        for trades in fills.values():
            for t in trades:
                if db.insert_trade(conn, **t.model_dump()):
                    stats["fills"] += 1
                    new_sigs.append(t.sig)
        try:
            db.save_token_decimals(conn, w.rpc.known_decimals())
        except Exception as e:  # noqa: BLE001
            log.warning("could not store decimals: %s", e)
    if stats["fills"]:
        classify(conn, now - 3600)
        # an address that filled more in the hour than a person can is not tracked from here
        junk = noise.sweep(conn, now)
        if junk:
            stats["quarantined"] = [j["address"][:10] for j in junk]
            w.roster_at = 0.0   # the roster is read again next tick, without it
        # judged and swept, the fills of followed wallets go to the chats that asked
        stats["followed"] = tell_followers(conn, new_sigs, now)
    w.last_block = head
    stats["blocks"] = head - first + 1
    w.ticks += 1
    w.fills += stats["fills"]
    # the same chain holds the subscription burns: one more request, only while a quote waits
    paid = w.burns.advance(conn, w.rpc, head, now)
    if paid:
        stats["paid"] = len(paid)
        confirm(conn, paid)

    burning = hot_now(conn, CHAIN, delta=settings.hot_delta, window_s=settings.hot_window_min * 60,
                      min_wallets=settings.hot_min_wallets,
                      max_age_s=settings.hot_max_age_h * 3600 or None, now=now)
    stats["hot"] = len(burning)
    if burning:
        name(conn, burning)
        # written down whether or not anybody is subscribed: the record is what the digest and the
        # site read back, and what a month from now says whether the feed was worth having
        stats["recorded"] = sum(record(conn, h, settings.telegram_realert_hours * 3600, CHAIN)
                                for h in burning)
        stats["sent"] = push(conn, burning)
        w.alerts += stats["sent"]
    return stats


def name(conn: sqlite3.Connection, burning: list[dict]) -> int:
    """Put a name on a bursting token we only know by address, now rather than next pass.

    A burst is minutes old when it fires and the naming pass runs every fifteen, so the first
    alert for a launch would otherwise read `$0x3adde1`, which is a contract, not a name. One
    request per unnamed token, through the same lookup the site uses.
    """
    from .new_tokens import lookup_tokens

    unnamed = [h for h in burning if h["sym"] == h["mint"][:8]]
    if not unnamed:
        return 0
    try:
        found, _ = lookup_tokens(CHAIN, [h["mint"] for h in unnamed])
    except Exception as e:  # noqa: BLE001 - an alert without a name still carries the contract
        log.warning("naming %d bursting tokens failed: %s", len(unnamed), e)
        return 0
    by_mint = {t.mint.lower(): t for t in found}
    named = 0
    with db.tx(conn):
        for h in unnamed:
            t = by_mint.get(h["mint"].lower())
            if t and t.symbol:
                db.upsert_token(conn, t.mint, chain=t.chain, symbol=t.symbol, mcap_usd=t.mcap_usd,
                                liquidity_usd=t.liquidity_usd, created_at=t.created_at,
                                price_usd=t.price_usd, price_at=db.now(), checked_at=db.now(),
                                decimals=t.decimals, pool_address=t.pool_address)
                h["sym"], h["liq"] = t.symbol, t.liquidity_usd
                named += 1
    return named


def confirm(conn: sqlite3.Connection, paid: list[dict]) -> int:
    """Tell each chat its burn landed. The credit is already on the books; a message that fails
    is a message that fails, the next /status says the same thing."""
    from ..bot import Telegram, gone, unsubscribe

    if not settings.telegram_bot_token:
        return 0
    tg, sent = Telegram(), 0
    for p in paid:
        try:
            tg.send(p["chat_id"], f"✓ {p['tokens']:,.0f} ${settings.pro_token_symbol} burned. "
                                  f"PRO until {pro._date(p['paid_until'])}. The alerts and the live feeds are on.")
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("pro confirmation to %s failed: %s", p["chat_id"], e)
            if gone(e):
                unsubscribe(conn, p["chat_id"])
    return sent


def tell_followers(conn: sqlite3.Connection, sigs: list[str], now: int | None = None) -> int:
    """Each new fill of a followed wallet, to each chat following it. Lazy on the bot for the
    same reason `push` is."""
    from ..bot import Telegram, fmt_fill, gone, unsubscribe
    from . import follows

    if not sigs or not settings.telegram_bot_token:
        return 0
    due = follows.alerts(conn, sigs, now)
    if not due:
        return 0
    from . import prefs

    tg = Telegram()
    sent = 0
    for chat_id, f in due:
        sub = conn.execute("SELECT * FROM bot_subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        if sub is not None and prefs.quiet_now(sub, now or db.now()):
            continue
        try:
            tg.send(chat_id, fmt_fill(f, now))
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("follow alert to %s failed: %s", chat_id, e)
            if gone(e):
                unsubscribe(conn, chat_id)
    return sent


def push(conn: sqlite3.Connection, burning: list[dict]) -> int:
    """Tell every subscriber about each burst once. Lazy import: the bot needs a token, this does
    not, and a watcher with no bot configured is still a faster tape."""
    from ..bot import Telegram, fmt_hot, subscribers, already_sent, mark_sent, gone, unsubscribe
    from . import discord, prefs, webhooks
    from . import pushes as pro_pushes

    now = db.now()
    subs = [s for s in subscribers(conn) if pro.entitled_row(s, now)]
    if not subs or not settings.telegram_bot_token:
        return 0
    tg = Telegram()
    quiet = settings.telegram_realert_hours * 3600
    sent = 0
    from .safety import check as sell_check, only_the_cohort

    for h in burning:
        # the first time a token bursts is the first time anyone tries to sell it in our name
        verdict = sell_check(conn, h["mint"], rpc=None, now=None)
        if verdict["sellable"] == 0:
            log.warning("burst on %s not pushed: %s", h["sym"], verdict["note"])
            continue
        why = only_the_cohort(conn, h["mint"], h["wallets"], now)
        if why:
            log.warning("burst on %s not pushed: %s", h["sym"], why)
            continue
        from .deployers import objection
        why = objection(conn, h["mint"], now=now)
        if why:
            log.warning("burst on %s not pushed: %s", h["sym"], why)
            continue
        key = f"hot:{h['mint']}"
        from .analyze import namesakes
        h["clones"] = namesakes(conn, h.get("sym"), h["mint"], now, hours=settings.telegram_clone_hours)
        # a burst on a name pushed today already needs half again the conviction to be told
        if any(c.get("pushed_at") for c in h["clones"]) and h["conviction"] < 1.5 * settings.hot_delta:
            log.warning("burst on %s not pushed: a %s was pushed in the last %dh and conviction %.1f is under %.1f",
                        h["sym"], h["sym"], settings.telegram_clone_hours, h["conviction"], 1.5 * settings.hot_delta)
            continue
        text = fmt_hot(h)
        told = 0
        for sub in subs:
            # told of this burst already, or of the launch on the same token minutes ago
            if already_sent(conn, sub["chat_id"], key, quiet) \
                    or already_sent(conn, sub["chat_id"], h["mint"], settings.telegram_dedupe_s):
                continue
            if not prefs.wants(sub, "burst", h.get("conviction"), now):
                continue
            try:
                tg.send(sub["chat_id"], text)
                mark_sent(conn, sub["chat_id"], key)
                sent += 1
                told += 1
            except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                log.warning("hot push to %s failed: %s", sub["chat_id"], e)
                if gone(e):
                    unsubscribe(conn, sub["chat_id"])
        if told:
            pro_pushes.record(conn, "burst", h, told, now)
            webhooks.fire(conn, "burst", h, now)
            discord.send(text)
    return sent


def run(conn: sqlite3.Connection, once: bool = False, rpc: RobinhoodRPC | None = None) -> dict:
    rpc = rpc or RobinhoodRPC()
    rpc.limiter.max = settings.watch_rpc_max_per_min
    w = Watch(rpc=rpc)
    # Four thousand "rpc scan" lines a day would bury the ten that matter. A tick says something
    # only when it found a fill or a burst; the source stays quiet unless it has a complaint.
    for name in ("fomo_agent.sources.rpc", "fomo_agent.ratelimit"):
        logging.getLogger(name).setLevel(logging.WARNING)
    log.info("watch: every %ss, burst at +%.1f from %d wallets in %d min",
             settings.watch_poll_s, settings.hot_delta, settings.hot_min_wallets,
             settings.hot_window_min)
    while True:
        started = time.monotonic()
        try:
            s = tick(conn, w)
            if s["fills"] or s["hot"]:
                log.info("watch: %s", s)
        except RpcError as e:
            # the node throttling us is waited out; the node failing on its own side is not -
            # the next attempt usually lands on a backend that answers
            wait = settings.watch_node_error_wait_s if RobinhoodRPC.node_failed(e) else settings.watch_rate_limit_wait_s
            log.warning("watch: rpc says %s — waiting %ss", e, wait)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001 - a bad tick is a missed tick, not a dead watcher
            log.exception("watch tick failed: %s", e)
        if once:
            return {"ticks": w.ticks, "fills": w.fills, "alerts": w.alerts}
        time.sleep(max(0.0, settings.watch_poll_s - (time.monotonic() - started)))
