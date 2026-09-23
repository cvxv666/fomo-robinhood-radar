"""Telegram bot: routing, formatting and the alert loop, all without a network.

The transport is faked, so these cover the parts that decide what a subscriber actually receives —
which is where a bug is expensive: a wrong threshold or a broken dedupe means either silence or
a stream of duplicates, and both lose the subscriber.
"""
import time
import json

import pytest

from fomo_agent import bot, db
from fomo_agent.config import settings

ACE = "0x" + "a" * 40      # scores 88
MID = "0x" + "b" * 40      # scores 74
DUD = "0x" + "c" * 40      # scores 30
TOKEN = "0x" + "d" * 40
QUIET = "0x" + "e" * 40


class FakeTelegram:
    """Records what would have been sent instead of sending it."""

    def __init__(self, fail_for=()):
        self.sent = []
        self.photos = []
        self.fail_for = set(fail_for)

    def send(self, chat_id, text, preview=False):
        if str(chat_id) in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((str(chat_id), text))
        return {"message_id": len(self.sent)}

    def set_commands(self, commands):
        self.commands = commands
        return True

    def photo(self, chat_id, path, caption):
        if "photo" in self.fail_for:
            raise RuntimeError("Bad Request: PHOTO_INVALID_DIMENSIONS")
        self.photos.append((str(chat_id), path, caption))
        return {"message_id": len(self.photos), "photo": [{"file_id": "abc"}]}

    def me(self):
        return {"username": "fomoradar_bot"}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "bot.db")
    now = db.now()
    with db.tx(c):
        for addr, handle, uid, score, status in ((ACE, "ace", "u1", 88, "active"),
                                                 (MID, "mid", "u2", 74, "active"),
                                                 (DUD, "dud", "u3", 30, "dropped")):
            db.upsert_trader(c, addr, chain="robinhood", fomo_handle=handle, fomo_user_id=uid,
                             score=score, status=status, pnl_30d=2_000_000.0,
                             ai_summary=f"{handle} verdict", ai_model="claude-opus-5",
                             tags=json.dumps({"style": ["swing"], "red_flags": []}),
                             stats_json=json.dumps({"win_rate": 0.6, "realized_pnl": 12_000.0}))
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS", liquidity_usd=500_000)
        c.execute("INSERT INTO fomo_positions(trade_id, user_id, token, chain, unrealized_pnl, "
                  "cost_basis, seen_at) VALUES(?,?,?,?,?,?,?)",
                  ("p1", "u1", TOKEN, "robinhood", 900_000.0, 30_000.0, now))
        for i, (addr, mint) in enumerate(((ACE, TOKEN), (MID, TOKEN), (DUD, TOKEN), (DUD, QUIET))):
            db.insert_trade(c, sig=f"0xsig{i}", address=addr, chain="robinhood", mint=mint,
                            side="buy", usd_value=5_000.0, ts=now - 900, source="rpc")
    return c


# ---------------------------------------------------------------- formatting

def test_rows_line_up():
    out = bot.rows([("buyers", "11"), ("average score", "80")])
    assert "<pre>" in out and "</pre>" in out
    body = out.removeprefix("<pre>").removesuffix("</pre>").split("\n")
    assert len({len(line) for line in body}) == 1, "every line is the same width"


def test_who_line_names_the_wallets_and_counts_the_rest():
    line = bot.who_line("a,b,c,d,e,f,g,h", "90,88,86,84,82,80,78,76", limit=3)
    assert line == "a 90 · b 88 · c 86 +5"
    assert bot.who_line(None, None) == ""


def test_everything_user_supplied_is_escaped(conn):
    with db.tx(conn):
        db.upsert_trader(conn, ACE, ai_summary="<script>alert(1)</script>")
    out = bot.handle_text(conn, "ace", 1, None)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_signal_message_carries_the_names_not_just_a_count(conn):
    from fomo_agent.pipeline import analyze

    sig = analyze.signals(conn, "robinhood", hours=24)[0]
    msg = bot.fmt_signal(sig)
    assert "$PONS" in msg and "conviction" in msg
    assert "ace 88" in msg and "mid 74" in msg
    assert "dud" not in msg, "a wallet scoring 30 is not part of the signal"


# ---------------------------------------------------------------- routing

def test_help_is_the_answer_to_start_and_to_nothing(conn):
    for text in ("/start", "/help", "", "   "):
        assert "FOMO ROBINHOOD RADAR" in bot.handle_text(conn, text, 1, None)


def test_starting_is_joining(conn):
    """Three of four thousand visitors found /subscribe on the first day. /start is the opt-in;
    /stop is the opt-out, and it says the feeds still answer."""
    assert bot.subscribers(conn) == []
    bot.handle_text(conn, "/start", 42, "newcomer")
    assert [s["chat_id"] for s in bot.subscribers(conn)] == ["42"]
    bot.handle_text(conn, "/start", 42, "newcomer")
    assert len(bot.subscribers(conn)) == 1, "a second /start is not a second row"
    answer = bot.handle_text(conn, "/stop", 42, "newcomer")
    assert bot.subscribers(conn) == [] and "/start" in answer
    assert "FOMO ROBINHOOD RADAR" in bot.handle_text(conn, "/help", 43, None)
    assert bot.subscribers(conn) == [], "/help alone does not subscribe"


def test_the_menu_is_registered_on_start(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)

    class Poller(FakeTelegram):
        def updates(self, offset, timeout=None):
            return []

    tg = Poller()
    bot.run(conn, tg, once=True)
    assert [c for c, _ in tg.commands][:2] == ["hot", "launches"]


def test_a_bare_handle_returns_the_verdict(conn):
    out = bot.handle_text(conn, "ace", 1, None)
    assert "<b>ace</b>" in out and "88" in out and "ace verdict" in out
    assert "PONS" in out, "the open book comes with the verdict"


def test_a_bare_address_returns_the_token(conn):
    out = bot.handle_text(conn, TOKEN, 1, None)
    assert "$PONS" in out and "conviction" in out
    assert "holders" in out


def test_a_wallet_address_returns_its_trader_not_a_token(conn):
    """An address we know as a trader must not be read as a token — the trader is the better answer."""
    assert "<b>ace</b>" in bot.handle_text(conn, ACE, 1, None)


def test_the_deep_link_from_a_signal_message_works(conn):
    assert "$PONS" in bot.handle_text(conn, f"/token_{TOKEN}", 1, None)


def test_unknown_things_say_so_without_pretending(conn):
    out = bot.handle_text(conn, "definitelynobody", 1, None)
    assert "No trader called" in out
    out = bot.handle_text(conn, "0x" + "9" * 40, 1, None)
    assert "Nobody on the watchlist" in out


def test_a_quote_asset_is_refused_with_a_reason(conn):
    from fomo_agent.sources.rpc import USDG

    out = bot.handle_text(conn, USDG, 1, None)
    assert "quote asset" in out and "plumbing" in out


def test_leaderboard_commands(conn):
    assert "FOLLOW" in bot.handle_text(conn, "/top", 1, None)
    assert "DROPPED" in bot.handle_text(conn, "/dropped", 1, None)
    top = bot.handle_text(conn, "/top 1", 1, None)
    assert "ace" in top and "mid" not in top, "the limit is respected"


def test_status_counts_what_is_actually_there(conn):
    out = bot.handle_text(conn, "/status", 1, None)
    assert "traders" in out and "subscribers" in out


def test_a_broken_question_does_not_kill_the_bot(conn, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("nope")

    monkeypatch.setattr(bot.analyze, "analyze_trader", boom)
    tg = FakeTelegram()
    assert bot.handle_update(conn, tg, {"message": {"chat": {"id": 7}, "text": "ace"}})
    assert "ValueError" in tg.sent[0][1]


def test_start_leads_with_the_masthead(conn):
    """The first thing a new chat sees is the banner, with the help as its caption — one message."""
    tg = FakeTelegram()
    assert bot.handle_update(conn, tg, {"message": {"chat": {"id": 7}, "text": "/start"}})
    assert tg.sent == []
    chat, path, caption = tg.photos[0]
    assert (chat, path) == ("7", bot.START_BANNER)
    assert "FOMO ROBINHOOD RADAR" in caption and len(caption) <= bot.CAPTION_LIMIT


def test_a_failed_banner_still_answers(conn):
    """A missing file or a rejected upload must not be why someone's first /start says nothing."""
    tg = FakeTelegram(fail_for=["photo"])
    assert bot.handle_update(conn, tg, {"message": {"chat": {"id": 7}, "text": "/start"}})
    assert "FOMO ROBINHOOD RADAR" in tg.sent[0][1]


def test_help_is_text_only(conn):
    tg = FakeTelegram()
    assert bot.handle_update(conn, tg, {"message": {"chat": {"id": 7}, "text": "/help"}})
    assert tg.photos == [] and "FOMO ROBINHOOD RADAR" in tg.sent[0][1]


def test_updates_without_text_are_ignored(conn):
    tg = FakeTelegram()
    assert not bot.handle_update(conn, tg, {"message": {"chat": {"id": 7}}})
    assert not bot.handle_update(conn, tg, {"edited_message": {}})
    assert tg.sent == []


# ---------------------------------------------------------------- subscriptions

def test_subscribe_is_idempotent_and_carries_a_threshold(conn):
    assert bot.subscribe(conn, 7, "someone") is True
    assert bot.subscribe(conn, 7, "someone") is False, "second time is an update, not a new sub"
    assert len(bot.subscribers(conn)) == 1

    bot.handle_text(conn, "/subscribe 9.5", 7, "someone")
    assert bot.subscribers(conn)[0]["min_conviction"] == 9.5

    bot.unsubscribe(conn, 7)
    assert bot.subscribers(conn) == []


def a_launch(mint="0x" + "e" * 40, sym="HOT", age_s=600, heat=4.2):
    return {"mint": mint, "sym": sym, "heat": heat, "buyers": 5, "avg_score": 80.0, "lead_minutes": 6.0,
            "age_h": 0.3, "usd": 21_000.0, "liq": 90_000.0, "who": ["ace", "mid"], "scores": [88, 74],
            "first_ts": db.now() - age_s, "last_ts": db.now() - 60}


def test_the_bot_pushes_nothing_and_writes_the_launches_down(conn, monkeypatch):
    """Neither feed is pushed by the bot any more. The signals went first (0 for 5 over a day);
    the launches followed on their own record - $100 into each was -$1,205 at the hour read over
    thirty days. They are still written to the ledger, unsent, so the question stays measurable."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "low", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})

    stats = bot.sweep_launches(conn)
    assert stats == {"qualified": 1, "recorded": 1}
    row = conn.execute("SELECT kind, chats FROM pushes").fetchone()
    assert row["kind"] == "launch" and row["chats"] == 0, "written down, sent to nobody"
    assert conn.execute("SELECT COUNT(*) FROM bot_sent").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM x_posts").fetchone()[0] == 0, "and not posted either"
    assert bot.sweep_launches(conn)["recorded"] == 0, "the same launch is one row"

    # the record of this product is what it sent: the shadow rows are not in it
    from fomo_agent.pipeline import record
    assert record.report(conn, days=30)["totals"]["pushes"] == 0
    assert record.report(conn, days=30, sent_only=False)["totals"]["pushes"] == 1


def test_a_burst_is_sent_to_every_chat_and_only_once(conn, monkeypatch):
    """The burst alert is the free tier now: it goes to every chat, paid or not, and a chat told
    of a token is not told again inside the re-alert window."""
    from fomo_agent.pipeline import watch
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    monkeypatch.setattr("fomo_agent.pipeline.safety.depth", lambda *a, **k: 50_000.0)
    bot.subscribe(conn, "free", None)
    bot.subscribe(conn, "paid", None)
    from fomo_agent.pipeline import pro
    pro.grant(conn, "paid", 30)
    sent_to = []
    monkeypatch.setattr(bot, "Telegram", lambda: type("T", (), {"send": lambda self, c, t, preview=False: sent_to.append(str(c))})())
    h = {"mint": "0x" + "9" * 40, "sym": "HOT2", "conviction": 4.4, "wallets": 3, "usd": 9000.0, "px": 0.01,
         "first_ts": db.now() - 120, "last_ts": db.now(), "age_s": 300, "window_s": 1800, "liq": 50_000.0,
         "who": ["ace"], "scores": [88], "avg_score": 88.0}
    assert watch.push(conn, [h]) == 2 and sorted(sent_to) == ["free", "paid"]
    assert watch.push(conn, [h]) == 0, "the same burst is one message"


def test_a_launch_is_pushed_only_while_it_is_one(conn, monkeypatch):
    """An hour after the first trusted wallet went in, a launch is history: still on /fresh, not
    in anybody's pocket."""
    monkeypatch.setattr(settings, "telegram_launch_max_age_min", 60)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch(age_s=59 * 60), a_launch(mint="0x" + "d" * 40, sym="OLD", age_s=3 * 3600)], "hours": 6})
    kinds = [(k, t["sym"]) for k, t, _ in bot.due(conn)]
    assert kinds == [("launch", "HOT")]


def test_a_launch_the_cohort_stopped_buying_is_not_pushed(conn, monkeypatch):
    """Heat can cross the bar minutes after the last trusted buy; by then it is history."""
    monkeypatch.setattr(settings, "telegram_launch_max_gap_s", 360)
    live = a_launch(); live["last_ts"] = db.now() - 60
    late = a_launch(mint="0x" + "d" * 40, sym="LATE"); late["last_ts"] = db.now() - 700
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [live, late], "hours": 6})
    assert [t["sym"] for _, t, _ in bot.due(conn)] == ["HOT"]


def test_a_burst_on_a_token_already_told_of_is_silent(conn, monkeypatch):
    """The same event is not told twice, whichever surface named it first."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_dedupe_s", 600)
    bot.subscribe(conn, "one", None)
    from fomo_agent.pipeline import watch
    other = "0x" + "b" * 40
    bot.mark_sent(conn, "one", other)
    sent_to = []
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr(bot, "Telegram", lambda: type("T", (), {"send": lambda self, c, t, preview=False: sent_to.append(str(c))})())
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    monkeypatch.setattr("fomo_agent.pipeline.safety.depth", lambda *a, **k: 50_000.0)
    h = {"mint": other, "sym": "SAME", "conviction": 4.4, "wallets": 3, "usd": 9000.0, "px": 0.01,
         "first_ts": db.now() - 120, "last_ts": db.now(), "age_s": 300, "window_s": 1800, "liq": 50_000.0,
         "who": ["ace"], "scores": [88], "avg_score": 88.0}
    assert watch.push(conn, [h]) == 0 and sent_to == []


def test_a_new_subscriber_starts_from_now(conn, monkeypatch):
    """Two hundred people joined in an afternoon and every one of them got the same three-hour-old
    launch. What is due at the moment of joining is treated as already seen."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    bot.subscribe(conn, "late", "joiner")
    mint = a_launch()["mint"]
    assert bot.already_sent(conn, "late", mint, 3600), "what was due at the door is not news"


def a_burst(mint="0x" + "9" * 40, sym="HOT2"):
    return {"mint": mint, "sym": sym, "conviction": 4.4, "wallets": 3, "usd": 9000.0, "px": 0.01,
            "first_ts": db.now() - 120, "last_ts": db.now(), "age_s": 300, "window_s": 1800,
            "liq": 50_000.0, "who": ["ace"], "scores": [88], "avg_score": 88.0}


def test_a_blocked_chat_unsubscribes_itself(conn, monkeypatch):
    from fomo_agent.pipeline import watch
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    monkeypatch.setattr("fomo_agent.pipeline.safety.depth", lambda *a, **k: 50_000.0)
    bot.subscribe(conn, "blocked", None)
    tg = FakeTelegram(fail_for=["blocked"])
    monkeypatch.setattr(bot, "Telegram", lambda: tg)
    assert watch.push(conn, [a_burst()]) == 0
    assert bot.subscribers(conn) == [], "a chat that blocked us is dropped, not retried forever"


def test_every_permanent_refusal_unsubscribes(conn, monkeypatch):
    """Telegram says why in the body; the bare status line is what httpx would have shown, and
    "Client error '403 Forbidden'" has to count too - for a day it did not, and eleven blocked
    chats were retried on every broadcast."""
    from fomo_agent.pipeline import watch
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    monkeypatch.setattr("fomo_agent.pipeline.safety.depth", lambda *a, **k: 50_000.0)
    for i in range(4):
        bot.subscribe(conn, f"gone{i}", None)

    class Refusing(FakeTelegram):
        def send(self, chat_id, text, preview=False):
            reasons = ("Forbidden: user is deactivated", "Forbidden: bot was kicked from the group chat",
                       "Client error '403 Forbidden' for url 'https://api.telegram.org/x'", "Bad Request: chat not found")
            raise RuntimeError(reasons[int(str(chat_id)[-1])])

    monkeypatch.setattr(bot, "Telegram", lambda: Refusing())
    watch.push(conn, [a_burst()])
    assert bot.subscribers(conn) == []
    assert not bot.gone(RuntimeError("ReadTimeout: the network blinked")), "a hiccup is retried"


def test_a_sweep_with_nothing_qualifying_writes_nothing(conn):
    assert bot.sweep_launches(conn) == {"qualified": 0, "recorded": 0}
    assert conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0] == 0


def test_run_once_answers_and_sweeps(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "low", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})

    class Poller(FakeTelegram):
        def updates(self, offset, timeout=None):
            return [{"update_id": 1, "message": {"chat": {"id": 7}, "text": "/top"}}]

    tg = Poller()
    stats = bot.run(conn, tg, once=True)
    assert stats["handled"] == 1 and stats["recorded"] == 1
    assert {c for c, _ in tg.sent} == {"7"}, "the loop answers; it does not push"


def test_fresh_command_reads_the_launch_feed(conn):
    """The bot and the page rank launches by the same measure."""
    from fomo_agent.bot import fmt_fresh, handle_text

    empty = fmt_fresh({"tokens": [], "hours": 24, "min_buyers": 2})
    assert "sitting in what it already holds" in empty

    feed = {
        "hours": 24, "drained": 3, "min_buyers": 2,
        "tokens": [{"sym": "PORT", "mint": "0x" + "a" * 40, "heat": 1.83, "buyers": 5,
                    "lead_minutes": 8.0, "who": ["fibs", "cissy"], "scores": [78, 80]}],
    }
    text = fmt_fresh(feed)
    assert "$PORT" in text and "heat 1.83" in text
    assert "first 8m after launch" in text
    assert "fibs 78" in text and "cissy 80" in text, "a list of names works as well as a string"
    assert "3 more had trusted buying" in text

    assert "FRESH" in handle_text(conn, "/fresh", 1, "u")
    assert "FRESH" in handle_text(conn, "/launches", 1, "u"), "the name it goes by now"
    assert "/launches" in handle_text(conn, "/help", 1, "u")


def test_a_hot_launch_is_recorded_and_nobody_is_told(conn, monkeypatch):
    """Two feeds describe the same token; neither is pushed, and the ledger holds one row."""
    monkeypatch.setattr(settings, "telegram_min_heat", 2.0)
    bot.subscribe(conn, "chat", "u", min_conviction=0.1)

    hot = {"mint": "0x" + "e" * 40, "sym": "HOT", "heat": 4.2, "buyers": 5, "avg_score": 80.0,
           "lead_minutes": 6.0, "age_h": 3.0, "usd": 21_000.0, "liq": 90_000.0,
           "who": ["ace", "mid"], "scores": [88, 74]}
    cold = {**hot, "mint": "0x" + "f" * 40, "sym": "COLD", "heat": 0.4}
    # the same token also qualifies as a signal, which is the normal case for a hot launch
    same = {"mint": hot["mint"], "sym": "HOT", "conviction": 9.0, "buyers": 5, "avg_score": 80.0,
            "first_ts": db.now() - 600, "usd": 21_000.0, "liq": 90_000.0,
            "who": "ace,mid", "scores": "88,74"}
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [hot, cold], "hours": 6})
    monkeypatch.setattr(bot.analyze, "signals", lambda *a, **k: [same])

    assert bot.sweep_launches(conn) == {"qualified": 1, "recorded": 1}
    assert conn.execute("SELECT COUNT(*) FROM bot_sent").fetchone()[0] == 0
    text = bot.due(conn)[0][2]
    assert "$HOT" in text and "launch" in text and "heat 4.20" in text
    assert "first wallet in" in text and "6 min" in text, "the lead time is the headline"
    assert "COLD" not in text, "below the heat floor, so no message"

    # the signal feed knows the same token; neither surface pushes it
    assert bot.sweep_launches(conn)["recorded"] == 0, "the same launch is one row, whoever named it"


def test_a_launch_with_no_known_open_time_does_not_claim_to_be_first(conn):
    """`lead_minutes` is None when we never learned when the pool opened.

    Printing 0 there would read as "they were in at the very start" — the opposite of unknown, and
    the most flattering possible misreading of a gap in our own data.
    """
    d = {"hours": 24, "counts": {"active": 1}, "joined": [], "joined_n": 0, "theses": [],
         "signals": [], "exits": [],
         "fresh": [{"mint": "0x" + "d" * 40, "sym": "PONS", "heat": 3.2, "buyers": 4,
                   "lead_minutes": None}],
         "health": {"ok": True, "checks": []}}
    text = bot.fmt_digest(d)
    assert "launch time unknown" in text and "0m after" not in text

    d["fresh"][0]["lead_minutes"] = 7.4
    assert "first in 7m after the pool opened" in bot.fmt_digest(d)


def test_a_quiet_day_says_so_in_one_line(conn):
    d = {"hours": 24, "counts": {"active": 169, "watch": 153, "dropped": 52}, "joined": [],
         "joined_n": 0, "signals": [], "fresh": [], "exits": [], "theses": [],
         "health": {"ok": True, "checks": []}}
    text = bot.fmt_digest(d)
    assert "Nothing moved" in text and "That is information too" in text
    assert "169 follow" in text, "the roster is still worth stating"


def test_a_launch_waits_its_first_minutes(conn, monkeypatch):
    """DEED: a seeding wave began at 19:22 and the launch went out at 19:24, seeded after the
    fact. The first trusted buy has to be a few minutes old before the launch is one."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_launch_min_age_s", 180)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch(age_s=100)], "hours": 6})
    assert bot.due(conn) == [], "a hundred seconds in: not yet"
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch(age_s=200)], "hours": 6})
    assert len(bot.due(conn)) == 1


def test_the_journal_never_carries_the_token(monkeypatch):
    import logging
    from fomo_agent.cli import Redacting
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    f = Redacting("%(message)s")
    rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "poll failed: ReadError for url https://api.telegram.org/bot%s/getUpdates",
                            ("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",), None)
    assert f.format(rec) == "poll failed: ReadError for url https://api.telegram.org/bot***/getUpdates"
    try:
        raise RuntimeError("boom https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ/x")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "tick failed", (), sys.exc_info())
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in f.format(rec) and "bot***/x" in f.format(rec)


def test_a_held_push_is_said_once_an_hour(caplog):
    import logging
    from fomo_agent.pipeline import watch
    watch._held.clear()
    with caplog.at_level(logging.DEBUG, logger="fomo_agent.pipeline.watch"):
        for _ in range(5):
            watch.held("0xabc", "burst on %s not pushed: %s", "AGE", "the 3rd $AGE in 14 days")
        watch.held("0xdef", "burst on %s not pushed: %s", "DOG", "the 5th $DOG in 14 days")
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and "AGE" in warned[0].message and "DOG" in warned[1].message
    assert sum(1 for r in caplog.records if r.levelno == logging.DEBUG) == 4


def test_a_rate_limited_send_waits_the_time_telegram_names(monkeypatch):
    """Eight paying chats never met the Bot API's ceiling; four hundred meet it on the first
    alert, and a reader who missed it because we sent too fast is the change undone."""
    import httpx
    from fomo_agent.bot.telegram import Telegram
    slept, tries = [], []
    monkeypatch.setattr(settings, "telegram_retry_after_s", 2.0)
    monkeypatch.setattr(time, "sleep", slept.append)

    def handler(request):
        tries.append(1)
        if len(tries) < 3:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram(token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tg.send("7", "hello")["message_id"] == 1
    assert len(tries) == 3 and [round(s) for s in slept] == [7, 7], "the wait Telegram named, not ours"


def test_the_bulk_send_keeps_the_gap_and_carries_on_past_a_bad_chat(monkeypatch):
    import httpx
    from fomo_agent.bot.telegram import Telegram
    gaps, bad = [], []
    monkeypatch.setattr(settings, "telegram_send_gap_s", 0.04)
    monkeypatch.setattr(time, "sleep", gaps.append)

    def handler(request):
        import json as _json
        chat = _json.loads(request.content)["chat_id"]
        if chat == "blocked":
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram(token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    sent = tg.send_each(["a", "blocked", "c"], "hi", on_error=lambda c, e: bad.append(c))
    assert sent == 2 and bad == ["blocked"]
    assert gaps == [0.04, 0.04], "a gap between sends, none before the first"
