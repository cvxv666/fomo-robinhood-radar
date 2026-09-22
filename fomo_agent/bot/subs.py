"""Who hears what: the subscriber list, what is due, and the messages that go out on a timer.

The bot pushes one thing - the burst alert, free to every chat since 22 September. Launches are
measured here and sent to nobody (`sweep_launches`); the hour-later look-back and the PRO notices
are the other two things this module sends.
"""
from __future__ import annotations

import logging
import time

from .. import db
from ..config import settings
from ..pipeline import analyze, prefs, pro, pushes
from .telegram import Telegram
from .words import esc, fmt_followup, fmt_launch, fmt_notice, fmt_void

log = logging.getLogger(__name__)


def subscribe(conn, chat_id, username: str | None, min_conviction: float | None = None,
              backlog: bool = False) -> bool:
    """Returns True when this is a new subscriber rather than a repeat.

    A new chat starts from now: whatever is due at this moment is marked as already sent to it,
    so its first push is the next launch, not the one from an hour ago. Two hundred people joined
    in an afternoon once and every one of them was handed the same three-hour-old launch as if it
    had just happened; the burst on that token had gone out to the six who were there when it
    burst. `backlog=True` keeps the old behaviour for a test that wants the queue.
    """
    existed = conn.execute("SELECT 1 FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO bot_subscribers(chat_id, username, min_conviction, subscribed_at, active) "
            "VALUES(?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET "
            "  username=excluded.username, min_conviction=COALESCE(excluded.min_conviction, bot_subscribers.min_conviction), active=1",
            (str(chat_id), username, min_conviction, db.now()),
        )
    if existed is None and not backlog:
        try:
            for _, t, _ in due(conn):
                mark_sent(conn, chat_id, t["mint"])
        except Exception:  # noqa: BLE001 - a feed that cannot answer must not block a subscription
            log.warning("could not quiet the backlog for %s", chat_id, exc_info=True)
    if existed is None and pro.enabled() and settings.pro_trial_days > 0:
        pro.grant(conn, chat_id, settings.pro_trial_days)
    return existed is None


def unsubscribe(conn, chat_id) -> None:
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))


def subscribers(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM bot_subscribers WHERE active=1")]


def gone(e: Exception) -> bool:
    """A send that will never succeed again: blocked, deleted, kicked, or a chat that is not
    there. Every one of these is a 403 or a 400 with a permanent reason; a network hiccup or a
    429 is not, and those are retried next time."""
    s = str(e)
    return ("Forbidden" in s or "bot was blocked" in s or "chat not found" in s
            or "user is deactivated" in s or "bot was kicked" in s)


def already_sent(conn, chat_id, mint: str, within_s: int) -> bool:
    row = conn.execute(
        "SELECT ts FROM bot_sent WHERE chat_id=? AND mint=? ORDER BY ts DESC LIMIT 1",
        (str(chat_id), mint),
    ).fetchone()
    return bool(row) and (db.now() - row["ts"]) < within_s


def mark_sent(conn, chat_id, mint: str) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?,?,?)",
                     (str(chat_id), mint, db.now()))


def due(conn, now: int | None = None) -> list[tuple[str, dict, str]]:
    """(kind, token, message) for every launch that qualifies right now.

    Nothing here is sent any more. The signal feed left the push first (0 for 5 over a day: by
    the time four wallets scoring 80 are in, the move is done), and the launch feed followed it
    on 22 September, on thirty days of its own record - $100 into each was -$1,205 at the hour
    read against the bursts' +$523. `sweep_launches` writes these to the ledger so the question
    keeps an answer, and `/launches` shows them to whoever asks.

    A launch is a push only while it is one: `telegram_launch_max_age_min` after the first
    trusted wallet went in it drops out of the queue, however hot it reads, so a subscriber who
    joins at four o'clock is not told about noon. And only while the cohort is still buying:
    once the last trusted buy is `telegram_launch_max_gap_s` old, the heat is history - every
    launch pushed more than two minutes after the cohort's last buy went nowhere.
    """
    chain = settings.dex_chains[0] if settings.dex_chains else None
    hours = settings.telegram_alert_window_h
    now = now or db.now()
    cutoff = now - settings.telegram_launch_max_age_min * 60
    stale = now - settings.telegram_launch_max_gap_s
    # and not before the first trusted buy is a few minutes old: provenance has run over the
    # fills by then, and a seeding wave (DEED: 17 wallets, one fill each, five minutes) reads
    # as one instead of as heat
    ripe = now - settings.telegram_launch_min_age_s
    from ..pipeline.safety import check as sell_check, cohort_share, crowd_objection, only_the_cohort
    from ..pipeline.watch import held

    out = []
    for t in analyze.fresh(conn, chain, hours=hours, limit=10)["tokens"]:
        first = t.get("first_ts")
        if t["heat"] >= settings.telegram_min_heat and (first or now) >= cutoff and (first is None or first <= ripe) \
                and (t.get("last_ts") or now) >= stale:
            # a launch nobody can leave is not a launch: asked of the chain before the message goes
            verdict = sell_check(conn, t["mint"], now=now)
            if verdict["sellable"] == 0:
                held(t["mint"], "launch %s not pushed: %s", t["sym"], verdict["note"])
                continue
            why = only_the_cohort(conn, t["mint"], t["buyers"], now)
            if why:
                held(t["mint"], "launch %s not pushed: %s", t["sym"], why)
                continue
            t["cohort_share"] = cohort_share(conn, t["mint"], t.get("usd"), now)
            why = crowd_objection(t["cohort_share"])
            if why:
                held(t["mint"], "launch %s not pushed: %s", t["sym"], why)
                continue
            from ..pipeline.deployers import objection
            why = objection(conn, t["mint"], now=now)
            if why:
                held(t["mint"], "launch %s not pushed: %s", t["sym"], why)
                continue
            t["clones"] = analyze.namesakes(conn, t.get("sym"), t["mint"], now, hours=settings.telegram_clone_hours)
            # the second PAWSINU of the hour is a clone, not a launch: the message that named the
            # first one already carries the warning, and this one is not sent
            if any(c.get("pushed_at") for c in t["clones"]):
                held(t["mint"], "launch %s not pushed: a %s was pushed in the last %dh", t["sym"], t["sym"], settings.telegram_clone_hours)
                continue
            # a borrowed name - a listed ticker's, or one launched here already this fortnight -
            # is told only with half again the heat, and the message says why it is suspect
            t["borrowed"] = analyze.borrowed_name(conn, t.get("sym"), t["mint"], now)
            if t["borrowed"] and t["heat"] < settings.telegram_min_heat * settings.namesake_bar_mult:
                held(t["mint"], "launch %s not pushed: %s, and heat %.2f is under %.2f", t["sym"], t["borrowed"]["why"],
                     t["heat"], settings.telegram_min_heat * settings.namesake_bar_mult)
                continue
            out.append(("launch", t, fmt_launch(t)))
    return out


class Burns:
    """The bot's own look at the burn address, on the alert timer: the watcher sees a burn
    within a tick when the RPC lets it, and this is the second pair of eyes for when it does
    not. Lazy: no RPC client until there is a gate, none of the allowance until a quote waits."""

    def __init__(self) -> None:
        self.rpc = None
        self.cursor = pro.Cursor()

    def look(self, conn) -> int:
        if not pro.enabled() or not pro.waiting(conn):
            return 0
        if self.rpc is None:
            from ..sources.rpc import RobinhoodRPC
            self.rpc = RobinhoodRPC()
            self.rpc.limiter.max = max(2, settings.watch_rpc_max_per_min // 3)
        head, _ = self.rpc.head()
        paid = self.cursor.advance(conn, self.rpc, head)
        if paid:
            from ..pipeline.watch import confirm
            confirm(conn, paid)
        return len(paid)








def followups(conn, tg: Telegram, now: int | None = None) -> int:
    """Every push whose hour is up: measure, tell the chats that got it, close the row."""
    now = now or db.now()
    sent = 0
    from ..pipeline import cards, discord, webhooks

    for row in pushes.due(conn, now):
        m = pushes.measure(conn, row, now=now)
        if m is None:
            continue   # the screener was busy; next minute
        if not row["chats"]:
            # a launch nobody was sent: measured for the ledger, and that is all it is for
            pushes.close(conn, row["id"], m, now)
            continue
        text = fmt_followup(row, m)
        # a x2 gets a card: the same read as a picture, with the text as its caption
        card = cards.make(conn, row, m, now)
        for chat_id in pushes.recipients(conn, row):
            try:
                if card is not None:
                    tg.photo(chat_id, card, text)
                else:
                    tg.send(chat_id, text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("followup to %s failed: %s", chat_id, e)
                if gone(e):
                    unsubscribe(conn, chat_id)
        pushes.close(conn, row["id"], m, now)
        log.info("followup %s %s: %s", row["kind"], row["sym"], m)
        webhooks.fire(conn, "followup", {"mint": row["mint"], "sym": row["sym"], "kind": row["kind"], "pushed_at": row["ts"], **m}, now)
        discord.send(text, image=card)
        try:
            from ..pipeline import xpost
            xpost.reply_followup(conn, row["id"], m, now=now)
            if card is not None:
                cards.post(conn, card, cards.data(row, m, now), now=now)
        except Exception as e:  # noqa: BLE001
            log.warning("x reply failed: %s", e)
    return sent


def notify(conn, tg: Telegram) -> int:
    """PRO ending soon, or just ended: one line each, once per paid period."""
    sent = 0
    for chat_id, kind in pro.reminders(conn):
        try:
            tg.send(chat_id, fmt_notice(kind, pro.paid_until(conn, chat_id)))
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("pro notice to %s failed: %s", chat_id, e)
            if gone(e):
                unsubscribe(conn, chat_id)
    return sent


def sweep_launches(conn, now: int | None = None) -> dict:
    """Write down every launch that would have been pushed, and push none of them.

    Measured over thirty days, a hundred dollars into each launch was -$1,205 at the hour read
    and 34% of them traded above the call; the same money into each burst was +$523 at 51%. Two
    thirds of what this bot sent was the half that lost. So the launch feed is a pull now -
    `/launches` answers with it and says what it has done - and the ledger keeps taking the rows
    so the question stays measurable: these carry `chats` = 0 and go nowhere.
    """
    now = now or db.now()
    stats = {"qualified": 0, "recorded": 0}
    for kind, t, _text in due(conn, now):
        stats["qualified"] += 1
        if pushes.record(conn, kind, t, 0, now, shadow=True) is not None:
            stats["recorded"] += 1
    if stats["recorded"]:
        log.info("launches: %s (recorded, not sent)", stats)
    return stats
