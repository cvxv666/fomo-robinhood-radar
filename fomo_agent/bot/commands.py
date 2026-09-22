"""What the bot answers, and the loop that keeps asking.

One function decides every reply (`handle_text`), which makes the whole command surface readable
in one place and testable without a network: a test hands it a string and reads the answer.
"""
from __future__ import annotations

import logging
import pathlib
import time

from .. import db
from .. import links as links_
from ..config import settings
from ..pipeline import analyze, prefs, pro, pushes
from .telegram import Telegram, TelegramError
from .words import *  # noqa: F401,F403 - every formatter, by the name the replies use
from .words import (CAPTION_LIMIT, HELP, HELP_PRO, PRO_ONLY, START_BANNER, esc, help_text,
                    launch_record, short)
from .subs import (Burns, already_sent, due, followups, gone, mark_sent, notify, subscribe,
                   subscribers, sweep_launches, unsubscribe)

log = logging.getLogger(__name__)


def status_text(conn, chat_id=None) -> str:
    def q(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    out = "<b>DATABASE</b>\n" + rows([
        ("traders", f"{q('SELECT COUNT(*) FROM traders'):,}"),
        ("scored", f"{q('SELECT COUNT(*) FROM traders WHERE score IS NOT NULL'):,}"),
        ("follow", f"{q('SELECT COUNT(*) FROM traders WHERE status=?', 'active'):,}"),
        ("fills", f"{q('SELECT COUNT(*) FROM trades'):,}"),
        ("positions", f"{q('SELECT COUNT(*) FROM fomo_positions'):,}"),
        ("subscribers", f"{q('SELECT COUNT(*) FROM bot_subscribers WHERE active=1'):,}"),
    ], width=15)
    if chat_id is not None and pro.enabled():
        end = pro.paid_until(conn, chat_id)
        now = db.now()
        if end and end > now:
            line = f"PRO until {pro._date(end)} ({(end - now) // 86400} days left)"
        elif pro.entitled(conn, chat_id, now):
            line = f"free until {pro._date(settings.pro_grace_until)}"
        else:
            line = "free tier — /pro for the alerts and the live feeds"
        out += f"\n\n<b>THIS CHAT</b>\n{line}"
    return out


def handle_text(conn, text: str, chat_id, username: str | None) -> str:
    """Route one message to its answer. Pure enough to test without a network."""
    text = (text or "").strip()
    if not text:
        return help_text(conn, chat_id)
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]

    # /token_0xabc… — the deep link the signal message offers
    if cmd.startswith("/token_"):
        cmd, args = "/token", [cmd[len("/token_"):]]
    # /follow_unipcs - the one-tap form every push offers
    # the launch feed under the name it goes by now; /fresh is what it was called
    if cmd == "/launches":
        cmd = "/fresh"
    if cmd.startswith("/follow_"):
        cmd, args = "/follow", [cmd[len("/follow_"):]]

    if cmd == "/start":
        # Starting is joining. Three of four thousand visitors found /subscribe on the first
        # day; the alerts are the product, and asking people to opt in twice is how a bot with
        # a hundred readers has three subscribers.
        was_new = subscribe(conn, chat_id, username)
        if args and args[0].lower() == "pro" and pro.enabled():
            return handle_text(conn, "/pro", chat_id, username)   # the site's button: t.me/<bot>?start=pro
        if args and args[0].lower().startswith("r") and len(args[0]) == 9:
            from ..pipeline import referrals

            r = referrals.join(conn, chat_id, args[0][1:].lower(), was_new)
            if r and r["rewarded"]:
                return (f"Welcome - you came by a reader's invite, so this chat has <b>{r['days']} days of PRO</b> from now, "
                        f"and so do they.\n\n" + help_text(conn, chat_id))
        return help_text(conn, chat_id)
    if cmd == "/help":
        return help_text(conn, chat_id)
    if cmd == "/status":
        return status_text(conn, chat_id)
    if cmd == "/pro":
        if not pro.enabled():
            return "There is no PRO tier yet: everything here is open. /help"
        q = pro.quote(conn, chat_id)
        if q is None:
            return "No price for the token right now, so no quote. Try again in a minute."
        q["_conn"] = conn
        return fmt_quote(q)
    if cmd == "/apikey":
        if not pro.enabled() or settings.api_key_rate_per_min <= 0:
            return "There are no API keys yet: the API is open at 120 a minute. /help"
        if not pro.entitled(conn, chat_id):
            return ("An API key is <b>PRO</b>: " + PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                    symbol=esc(settings.pro_token_symbol)).replace("This feed is <b>PRO</b>. ", ""))
        from ..pipeline import keys
        had = keys.current(conn, chat_id)
        key = keys.issue(conn, chat_id)
        return (("Your previous key is revoked. " if had else "") +
                f"Your API key:\n<code>{key}</code>\n\n"
                f"Send it as the <code>X-API-Key</code> header for {settings.api_key_rate_per_min} requests a minute "
                f"(120 without). It works while this chat is PRO. Keep it to yourself; /apikey again replaces it. "
                f"Docs: {settings.public_site_url or ''}/docs")
    if cmd == "/claim":
        if not pro.enabled():
            return "There is nothing to claim: everything here is open. /help"
        if not args:
            return "Send it as <code>/claim 0x…</code> with the transaction hash of your burn."
        ok, why = pro.claim(conn, chat_id, args[0])
        return ("✓ " if ok else "") + esc(why)
    # bursts and the launch feed answer anybody: the burst alerts are free now, and a launch is
    # only ever pulled. /signals and /exits stay PRO - they are the cohort's whole book
    if cmd in ("/signals", "/exits") and not pro.entitled(conn, chat_id):
        return PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                               symbol=esc(settings.pro_token_symbol))
    if cmd == "/health":
        from ..pipeline.health import report

        return fmt_health(report(conn))
    if cmd == "/invite":
        from ..pipeline import referrals

        st = referrals.stats(conn, chat_id)
        days = settings.pro_referral_days
        return (f"Your invite link:\n{referrals.link(conn, chat_id)}\n\n"
                + (f"A chat that joins by it gets <b>{days} days of PRO</b>, and so do you, up to {settings.pro_referral_max_per_month} times a month. " if pro.enabled() and days else "")
                + f"Joined by your link so far: <b>{st['joined']}</b>.")
    if cmd == "/webhook":
        from ..pipeline import webhooks

        if not pro.entitled(conn, chat_id):
            return "A webhook is <b>PRO</b>: " + PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                                                                 symbol=esc(settings.pro_token_symbol)).replace("This feed is <b>PRO</b>. ", "")
        if not args:
            return webhooks.status(conn, chat_id)
        return webhooks.set_url(conn, chat_id, args[0])[1]
    if cmd == "/settings":
        return prefs.describe(conn, chat_id)
    if cmd == "/alerts":
        return esc(prefs.set_kinds(conn, chat_id, args[0] if args else "")[1]) if args else prefs.describe(conn, chat_id)
    if cmd == "/minconv":
        return esc(prefs.set_min_conviction(conn, chat_id, args[0] if args else "")[1])
    if cmd == "/quiet":
        return esc(prefs.set_quiet(conn, chat_id, args[0] if args else "")[1])
    if cmd in ("/follow", "/unfollow", "/following"):
        from ..pipeline import follows

        if cmd == "/following" or not args:
            return fmt_following(follows.following(conn, chat_id), follows.limit_for(conn, chat_id))
        fn = follows.follow if cmd == "/follow" else follows.unfollow
        return esc(fn(conn, chat_id, args[0])[1])
    if cmd in ("/record", "/paper"):
        from ..pipeline import record as record_

        days = int(args[0]) if args and args[0].isdigit() else 7
        rep = record_.report(conn, days=min(days, 120))
        return fmt_paper(rep) if cmd == "/paper" else fmt_record(rep)
    if cmd == "/signals":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_signals(analyze.signals(conn, chain, hours=hours, limit=10), hours)
    if cmd == "/hot":
        from ..pipeline.hot import hot_now

        mins = int(args[0]) if args and args[0].isdigit() else settings.hot_window_min
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_hot_list(hot_now(conn, chain, delta=settings.hot_delta, window_s=mins * 60,
                                    min_wallets=settings.hot_min_wallets,
                                    max_age_s=settings.hot_max_age_h * 3600 or None), mins)
    if cmd == "/fresh":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        text = fmt_fresh(analyze.fresh(conn, chain, hours=hours, limit=10))
        note = launch_record(conn)
        return f"{text}\n\n{note}" if note else text
    if cmd == "/digest":
        from ..pipeline.digest import daily
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_digest(daily(conn, hours=24, chain=chain))
    if cmd == "/exits":
        hours = int(args[0]) if args and args[0].isdigit() else 6
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_exits(analyze.exits(conn, chain, hours=hours, limit=10), hours)
    if cmd in ("/top", "/watch", "/dropped"):
        status = {"/top": "active", "/watch": "watch", "/dropped": "dropped"}[cmd]
        n = int(args[0]) if args and args[0].isdigit() else 15
        return fmt_leaderboard(analyze.leaderboard(conn, min(n, 40), status), status)
    if cmd == "/subscribe":
        # a number after it used to set a conviction floor for signal pushes; signals are not
        # pushed any more, so it is accepted and means nothing
        floor = float(args[0]) if args and args[0].replace(".", "", 1).isdigit() else None
        fresh = subscribe(conn, chat_id, username, floor)
        return (("Alerts are on." if fresh else "Alerts were already on.") +
                " Free, for everybody. You will hear about:\n"
                "· a <b>burst</b> — several trusted wallets entering one token inside minutes, "
                "sent within seconds of the fill\n"
                "· one digest a day\n\n"
                "That is the half of the feed that pays for itself: over 30 days $100 into every burst "
                "was +$523 at the hour read, and into every launch \u2212$1,205. So launches are not sent "
                "any more \u2014 /launches shows them when you want them, with their record attached.\n\n"
                "/stop for none. /follow, /signals and /exits are PRO: /pro.")
    if cmd in ("/stop", "/unsubscribe", "/mute"):
        unsubscribe(conn, chat_id)
        return "Alerts are off for this chat. /start turns them back on; the feeds still answer."
    if cmd == "/token":
        if not args:
            return "Send it as <code>/token 0x…</code>, or just paste the address."
        return fmt_token(analyze.analyze_token(conn, args[0]))
    if cmd == "/trader":
        if not args:
            return "Send it as <code>/trader unipcs</code>, or just send the handle."
        a = analyze.analyze_trader(conn, args[0])
        return fmt_trader(a) if a else f"No trader matches <b>{esc(args[0])}</b>."
    if cmd.startswith("/"):
        return f"Unknown command {esc(cmd)}.\n\n" + HELP

    # bare text: an address is a token, anything else is a handle
    if text.startswith("0x") and len(text) == ADDRESS_LEN:
        a = analyze.analyze_trader(conn, text)
        return fmt_trader(a) if a else fmt_token(analyze.analyze_token(conn, text))
    a = analyze.analyze_trader(conn, text)
    if a:
        return fmt_trader(a)
    return (f"No trader called <b>{esc(text)}</b>, and that is not a token address.\n\n"
            "Send a 0x… address for a token, a handle for a trader, or /signals.")


def is_start(text: str) -> bool:
    """`/start`, `/start@thebot`, and the deep-linked `/start ref=x` Telegram sends from a link."""
    return text.strip().split(" ")[0].split("@")[0] == "/start"


def handle_update(conn, tg: Telegram, update: dict) -> bool:
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat is None or not msg.get("text"):
        return False
    user = (msg.get("from") or {}).get("username")
    try:
        answer = handle_text(conn, msg["text"], chat, user)
    except Exception as e:  # noqa: BLE001 - a bad question must not kill the bot
        log.exception("handling %r failed", msg.get("text"))
        answer = f"That broke something: <code>{esc(type(e).__name__)}</code>. Try /help."

    # The first thing a new chat sees is the masthead, with the help as its caption — one message,
    # not two. Anything at all going wrong here falls back to the text: a missing file or a
    # rejected upload must not be the reason someone's first /start answers nothing.
    if is_start(msg["text"]) and START_BANNER.exists() and len(answer) <= CAPTION_LIMIT:
        try:
            tg.photo(chat, START_BANNER, answer)
            return True
        except Exception:  # noqa: BLE001
            log.warning("start banner failed, sending the text alone", exc_info=True)

    tg.send(chat, answer)
    return True


def run(conn, tg: Telegram | None = None, once: bool = False) -> dict:
    """Long-poll for questions and push signals on a timer, in one loop."""
    tg = tg or Telegram()
    who = tg.me()
    log.info("bot @%s online", who.get("username"))
    try:
        tg.set_commands(COMMANDS)
    except Exception as e:  # noqa: BLE001 - a menu that failed to register is not a bot that is down
        log.warning("could not register the command menu: %s", e)
    stats = {"handled": 0, "broadcasts": 0, "recorded": 0}
    # -inf rather than 0: monotonic() counts from boot, and on a machine up for less than the
    # alert interval - a fresh CI runner, a just-rebooted server - zero would mean waiting
    # out the interval before the first broadcast instead of sending it at once
    offset, last_alert = 0, float("-inf")
    burns = Burns()
    while True:
        try:
            for u in tg.updates(offset) or []:
                offset = max(offset, u["update_id"] + 1)
                stats["handled"] += handle_update(conn, tg, u)
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001 - a dropped poll is normal, keep going
            # the class and the first words: an httpx error carries the request URL, and the
            # URL carries the token (the formatter scrubs it too, but a line that never had it
            # needs no scrubbing)
            log.warning("poll failed: %s: %s", type(e).__name__, str(e).split(" for url")[0][:120])
            time.sleep(5)

        if time.monotonic() - last_alert >= settings.telegram_alert_interval_s:
            last_alert = time.monotonic()
            try:
                stats["recorded"] += sweep_launches(conn)["recorded"]
                stats["broadcasts"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("launch sweep failed: %s", e)
            try:
                notify(conn, tg)
            except Exception as e:  # noqa: BLE001
                log.warning("pro notices failed: %s", e)
            try:
                burns.look(conn)
            except Exception as e:  # noqa: BLE001 - the watcher is the first pair of eyes
                log.warning("pro burn check failed: %s", e)
            try:
                followups(conn, tg)
            except Exception as e:  # noqa: BLE001
                log.warning("followups failed: %s", e)
            try:
                from ..pipeline import xpost
                xpost.send_due(conn)
            except Exception as e:  # noqa: BLE001 - X being down is not the bot being down
                log.warning("x posting failed: %s", e)
        if once:
            return stats
