"""Telegram bot: routing, formatting and the alert loop, all without a network.

The transport is faked, so these cover the parts that decide what a subscriber actually receives —
which is where a bug is expensive: a wrong threshold or a broken dedupe means either silence or
a stream of duplicates, and both lose the subscriber.
"""
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
    assert [c for c, _ in tg.commands][:2] == ["hot", "signals"]


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


def test_signals_are_not_pushed_launches_are(conn, monkeypatch):
    """The fixture has a signal ($PONS, conviction well over any floor). Nobody is pushed it: the
    signal feed answers /signals and nothing else. A launch goes to everyone, whatever number they
    once gave /subscribe."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "low", None, min_conviction=0.5)
    bot.subscribe(conn, "high", None, min_conviction=99.0)
    assert bot.broadcast(conn, FakeTelegram())["sent"] == 0

    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    tg = FakeTelegram()
    stats = bot.broadcast(conn, tg)
    assert stats["subscribers"] == 2 and stats["sent"] == 2 and stats["launches"] == 2
    assert all("$HOT" in text for _, text in tg.sent)


def test_the_same_token_is_not_sent_twice(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "low", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    tg = FakeTelegram()

    assert bot.broadcast(conn, tg)["sent"] == 1
    again = bot.broadcast(conn, tg)
    assert again["sent"] == 0 and again["skipped"] == 1
    assert len(tg.sent) == 1


def test_a_launch_is_pushed_only_while_it_is_one(conn, monkeypatch):
    """An hour after the first trusted wallet went in, a launch is history: still on /fresh, not
    in anybody's pocket."""
    monkeypatch.setattr(settings, "telegram_launch_max_age_min", 60)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch(age_s=59 * 60), a_launch(mint="0x" + "d" * 40, sym="OLD", age_s=3 * 3600)], "hours": 6})
    kinds = [(k, t["sym"]) for k, t, _ in bot.due(conn)]
    assert kinds == [("launch", "HOT")]


def test_a_launch_the_cohort_stopped_buying_is_not_pushed(conn, monkeypatch):
    """Heat can cross the bar minutes after the last trusted buy; by then it is history."""
    monkeypatch.setattr(settings, "telegram_launch_max_gap_s", 120)
    live = a_launch(); live["last_ts"] = db.now() - 60
    late = a_launch(mint="0x" + "d" * 40, sym="LATE"); late["last_ts"] = db.now() - 400
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [live, late], "hours": 6})
    assert [t["sym"] for _, t, _ in bot.due(conn)] == ["HOT"]


def test_one_message_per_token_whichever_came_first(conn, monkeypatch):
    """A burst told two minutes ago makes the launch on the same token silent, and the other way
    round: the same event is not told twice."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_dedupe_s", 600)
    bot.subscribe(conn, "one", None)
    launch = a_launch()
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [launch], "hours": 6})
    bot.mark_sent(conn, "one", f"hot:{launch['mint']}")
    tg = FakeTelegram()
    assert bot.broadcast(conn, tg)["sent"] == 0 and bot.broadcast(conn, tg)["skipped"] == 1
    # and a burst after a launch
    from fomo_agent.pipeline import watch
    other = "0x" + "b" * 40
    bot.mark_sent(conn, "one", other)
    sent_to = []
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr(bot, "Telegram", lambda: type("T", (), {"send": lambda self, c, t, preview=False: sent_to.append(str(c))})())
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    h = {"mint": other, "sym": "SAME", "conviction": 4.4, "wallets": 3, "usd": 9000.0, "px": 0.01,
         "first_ts": db.now() - 120, "last_ts": db.now(), "age_s": 300, "window_s": 1800, "liq": 50_000.0,
         "who": ["ace"], "scores": [88], "avg_score": 88.0}
    assert watch.push(conn, [h]) == 0 and sent_to == []


def test_a_new_subscriber_starts_from_now(conn, monkeypatch):
    """Two hundred people joined in an afternoon and every one of them got the same three-hour-old
    launch. Now what is due at the moment of joining is treated as already seen."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    bot.subscribe(conn, "late", "joiner")
    tg = FakeTelegram()
    assert bot.broadcast(conn, tg)["sent"] == 0, "the launch that was already due is not a push for a newcomer"
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch(), a_launch(mint="0x" + "c" * 40, sym="NEW")], "hours": 6})
    assert bot.broadcast(conn, tg)["sent"] == 1 and "$NEW" in tg.sent[0][1]


def test_a_blocked_chat_unsubscribes_itself(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "blocked", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    tg = FakeTelegram(fail_for=["blocked"])

    stats = bot.broadcast(conn, tg)
    assert stats["errors"] == 1 and stats["sent"] == 0
    assert bot.subscribers(conn) == [], "a chat that blocked us is dropped, not retried forever"


def test_every_permanent_refusal_unsubscribes(conn, monkeypatch):
    """Telegram says why in the body; the bare status line is what httpx would have shown, and
    "Client error '403 Forbidden'" has to count too - for a day it did not, and eleven blocked
    chats were retried on every broadcast."""
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    for i, reason in enumerate(("Forbidden: user is deactivated", "Forbidden: bot was kicked from the group chat",
                                "Client error '403 Forbidden' for url 'https://api.telegram.org/x'",
                                "Bad Request: chat not found")):
        bot.subscribe(conn, f"gone{i}", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})

    class Refusing(FakeTelegram):
        def send(self, chat_id, text, preview=False):
            reasons = ("Forbidden: user is deactivated", "Forbidden: bot was kicked from the group chat",
                       "Client error '403 Forbidden' for url 'https://api.telegram.org/x'", "Bad Request: chat not found")
            raise RuntimeError(reasons[int(str(chat_id)[-1])])

    bot.broadcast(conn, Refusing())
    assert bot.subscribers(conn) == []
    assert not bot.gone(RuntimeError("ReadTimeout: the network blinked")), "a hiccup is retried"


def test_broadcast_with_no_subscribers_does_nothing(conn):
    tg = FakeTelegram()
    assert bot.broadcast(conn, tg) == {"subscribers": 0, "sent": 0, "launches": 0,
                                       "skipped": 0, "errors": 0}
    assert tg.sent == []


def test_run_once_polls_and_broadcasts(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "low", None, min_conviction=0.5)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})

    class Poller(FakeTelegram):
        def updates(self, offset, timeout=None):
            return [{"update_id": 1, "message": {"chat": {"id": 7}, "text": "/top"}}]

    tg = Poller()
    stats = bot.run(conn, tg, once=True)
    assert stats["handled"] == 1 and stats["sent"] == 1
    assert {c for c, _ in tg.sent} == {"7", "low"}


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
    assert "/fresh" in handle_text(conn, "/help", 1, "u")


def test_a_hot_launch_is_pushed_once_and_not_again_by_the_other_feed(conn, monkeypatch):
    """Two feeds describe the same token; a subscriber should hear about it once."""
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

    tg = FakeTelegram()
    stats = bot.broadcast(conn, tg)
    assert stats["sent"] == 1 and stats["launches"] == 1
    assert len(tg.sent) == 1
    text = tg.sent[0][1]
    assert "$HOT" in text and "launch" in text and "heat 4.20" in text
    assert "first wallet in" in text and "6 min" in text, "the lead time is the headline"
    assert "COLD" not in text, "below the heat floor, so no message"

    # the signal feed knows the same token; the dedup is per token, not per feed
    assert bot.broadcast(conn, FakeTelegram())["sent"] == 0


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
