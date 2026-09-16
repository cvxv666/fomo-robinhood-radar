"""PRO access: a quote is an exact amount, a burn of that amount is a payment, and the gate
opens for exactly the chats that paid. No network: the price is planted in the tokens table and
the burns are handed to the settler as parsed transfer logs.

The expensive bugs here are the quiet ones - a paying subscriber not credited, a burn credited
twice, an honest subscriber locked out during the grace - so that is what these pin down.
"""
import pytest

from fomo_agent import bot, db
from fomo_agent.config import settings
from fomo_agent.pipeline import pro, watch

TOKEN = "0x" + "f" * 40
DEAD = "0x000000000000000000000000000000000000dead"
WALLET = "0x" + "1" * 40


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, preview=False):
        self.sent.append((str(chat_id), text))
        return {"message_id": len(self.sent)}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pro_price_usd", 20.0)
    monkeypatch.setattr(settings, "pro_token", TOKEN)
    monkeypatch.setattr(settings, "pro_burn_address", DEAD)
    monkeypatch.setattr(settings, "pro_days", 30)
    monkeypatch.setattr(settings, "pro_grace_until", 0)
    monkeypatch.setattr(settings, "pro_trial_days", 0)
    monkeypatch.setattr(settings, "pro_price_max_age_s", 10 ** 9)
    c = db.connect(tmp_path / "pro.db")
    with db.tx(c):
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="FOMOBRAIN", price_usd=0.000336, price_at=db.now(), decimals=18)
    return c


def burn(tokens: float, tx: str = "0x" + "ab" * 32, frm: str = WALLET) -> dict:
    return {"token": TOKEN, "frm": frm, "to": DEAD, "raw": int(round(tokens * 10 ** 18)), "tx": tx, "index": 0, "block": 100}


def test_a_quote_is_at_least_the_price_and_unique_among_open_ones(conn):
    q1 = pro.quote(conn, "a")
    q2 = pro.quote(conn, "b")
    need = 20 / 0.000336
    assert q1["tokens"] >= need and q2["tokens"] >= need
    assert q1["tokens"] != q2["tokens"]
    assert q1["tokens"] - need < need * 0.03, "the rounding is a couple of percent, not a tenth"
    assert q1["code"] != q2["code"] and len(q1["code"]) == 5
    assert pro.quote(conn, "a")["code"] == q1["code"], "asking again inside the window is the same quote"


def test_exact_amounts_never_collide_and_stay_small():
    taken = set()
    for _ in range(200):
        t = pro.exact_amount(59_523.8, taken)
        assert t not in taken and t >= 59_523.8
        taken.add(t)
    assert pro.exact_amount(1_050.0, set()) >= 1_050 and pro.exact_amount(1_050.0, set()) < 1_300


def test_a_burn_of_the_exact_amount_is_credited_once(conn):
    q = pro.quote(conn, "42")
    now = db.now()
    p = pro.record(conn, burn(q["tokens"]), now, now)
    assert p["chat_id"] == "42" and p["paid_until"] == now + 30 * 86400
    assert pro.entitled(conn, "42", now) and not pro.entitled(conn, "43", now)
    assert conn.execute("SELECT status, tx FROM pro_quotes WHERE code=?", (q["code"],)).fetchone()["status"] == "paid"
    assert pro.record(conn, burn(q["tokens"]), now, now) is None, "the same transaction again is nothing"
    assert pro.paid_until(conn, "42") == now + 30 * 86400
    again = pro.record(conn, burn(q["tokens"], tx="0x" + "cd" * 32), now, now)
    assert again["chat_id"] is None, "a second burn of a paid amount belongs to nobody until claimed"


def test_paying_again_stacks_the_days(conn):
    now = db.now()
    pro.grant(conn, "42", 30, now)
    q = pro.quote(conn, "42", now)
    pro.record(conn, burn(q["tokens"]), now, now)
    assert pro.paid_until(conn, "42") == now + 60 * 86400


def test_a_rounded_amount_is_claimed_by_hash(conn):
    q = pro.quote(conn, "7")
    now = db.now()
    p = pro.record(conn, burn(60_000), now, now)    # the subscriber typed a round number
    assert p["chat_id"] is None
    ok, why = pro.claim(conn, "7", p["tx"], rpc=None, now=now)
    assert ok and "PRO until" in why
    assert pro.entitled(conn, "7", now)
    ok, why = pro.claim(conn, "8", p["tx"], rpc=None, now=now)
    assert not ok and "another chat" in why
    short = pro.record(conn, burn(30_000, tx="0x" + "ef" * 32), now, now)
    ok, why = pro.claim(conn, "9", short["tx"], rpc=None, now=now)
    assert not ok and "under" in why
    big = pro.record(conn, burn(q["tokens"] * 2 + 5, tx="0x" + "12" * 32), now, now)
    ok, why = pro.claim(conn, "10", big["tx"], rpc=None, now=now)
    assert ok and "2 months" in why and pro.paid_until(conn, "10") == now + 60 * 86400


def test_the_gate_lets_through_paid_grace_and_nobody_else(conn, monkeypatch):
    now = db.now()
    bot.subscribe(conn, "old", None)
    bot.subscribe(conn, "paid", None)
    pro.grant(conn, "paid", 30, now)
    assert pro.entitled(conn, "paid", now) and not pro.entitled(conn, "old", now)
    monkeypatch.setattr(settings, "pro_grace_until", now + 7 * 86400)
    assert pro.entitled(conn, "old", now), "everyone who was here before the gate stays on until the date"
    assert not pro.entitled(conn, "old", now + 8 * 86400)
    monkeypatch.setattr(settings, "pro_price_usd", 0.0)
    assert pro.entitled(conn, "nobody", now), "no price is no gate"


def test_pushes_go_only_to_entitled_chats(conn, monkeypatch):
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    bot.subscribe(conn, "free", None)
    bot.subscribe(conn, "paid", None)
    pro.grant(conn, "paid", 30)
    from tests.test_bot import a_launch
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    tg = FakeTelegram()
    stats = bot.broadcast(conn, tg)
    assert stats["sent"] == 1 and tg.sent[0][0] == "paid"
    # the watcher's burst push runs the same gate
    sent_to = []
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr(bot, "Telegram", lambda: type("T", (), {"send": lambda self, c, t, preview=False: sent_to.append(str(c))})())
    monkeypatch.setattr("fomo_agent.pipeline.safety.check", lambda *a, **k: {"sellable": 1, "note": ""})
    # another token: a burst on the one just pushed as a launch would be the same event twice
    h = {"mint": "0x" + "9" * 40, "sym": "HOT2", "conviction": 4.4, "wallets": 3, "usd": 9000.0, "px": 0.01,
         "first_ts": db.now() - 120, "last_ts": db.now(), "age_s": 300, "window_s": 1800, "liq": 50_000.0,
         "who": ["ace"], "scores": [88], "avg_score": 88.0}
    assert watch.push(conn, [h]) == 1 and sent_to == ["paid"]


def test_free_feeds_and_pro_feeds(conn, monkeypatch):
    monkeypatch.setattr(settings, "public_site_url", "https://radar.test")
    bot.subscribe(conn, "free", None)
    monkeypatch.setattr(bot.analyze, "leaderboard", lambda *a, **k: [])
    assert "PRO" in bot.handle_text(conn, "/hot", "free", None)
    assert "PRO" not in bot.handle_text(conn, "/top", "free", None) or "leaderboard" in bot.handle_text(conn, "/top", "free", None).lower()
    quote_text = bot.handle_text(conn, "/pro", "free", None)
    q = pro.open_quote(conn, "free")
    assert str(q["tokens"]) in quote_text and DEAD in quote_text.lower() and q["code"] in quote_text
    assert "free tier" in bot.handle_text(conn, "/help", "free", None)
    pro.grant(conn, "free", 30)
    assert "PRO until" in bot.handle_text(conn, "/help", "free", None)
    assert "PRO until" in bot.handle_text(conn, "/status", "free", None)


def test_a_newcomer_gets_the_trial_if_there_is_one(conn, monkeypatch):
    monkeypatch.setattr(settings, "pro_trial_days", 3)
    bot.subscribe(conn, "new", None)
    assert pro.entitled(conn, "new")
    assert not pro.entitled(conn, "new", db.now() + 4 * 86400)


def test_reminders_fire_once_per_period_and_reset_on_renewal(conn):
    now = db.now()
    bot.subscribe(conn, "p", None)
    pro.grant(conn, "p", 30, now)
    assert pro.reminders(conn, now) == []
    assert pro.reminders(conn, now + 28 * 86400) == [("p", "3d")]
    assert pro.reminders(conn, now + 28 * 86400 + 60) == [], "said once"
    assert pro.reminders(conn, now + 29 * 86400 + 3600) == [("p", "1d")]
    assert pro.reminders(conn, now + 30 * 86400 + 60) == [("p", "expired")]
    assert pro.reminders(conn, now + 30 * 86400 + 120) == []
    pro.grant(conn, "p", 30, now + 30 * 86400 + 200)
    assert pro.reminders(conn, now + 30 * 86400 + 300) == [], "renewed: nothing to say"
    assert pro.reminders(conn, now + 58 * 86400) == [("p", "3d")], "and the sequence starts over for the new end"


def test_notices_are_sent_through_the_bot(conn):
    now = db.now()
    bot.subscribe(conn, "p", None)
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET paid_until=? WHERE chat_id='p'", (now + 3600,))
    tg = FakeTelegram()
    assert bot.notify(conn, tg) == 1 and "one day" in tg.sent[0][1]


def test_settle_asks_the_chain_only_while_a_quote_is_waiting(conn):
    class RPC:
        calls = 0

        def logs(self, a, b, **flt):
            RPC.calls += 1
            return []

    assert pro.settle(conn, RPC(), 1, 2) == (True, []) and RPC.calls == 0, "no quote, no request"
    pro.quote(conn, "42")
    pro.settle(conn, RPC(), 1, 2)
    assert RPC.calls == 1


def test_settle_reads_burns_and_confirms(conn, monkeypatch):
    q = pro.quote(conn, "42")

    class RPC:
        def logs(self, a, b, **flt):
            assert flt["address"] == TOKEN
            return [{"address": TOKEN, "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                                  "0x" + "0" * 24 + WALLET[2:], "0x" + "0" * 24 + DEAD[2:]],
                     "data": hex(q["tokens"] * 10 ** 18), "transactionHash": "0x" + "77" * 32, "logIndex": "0x0", "blockNumber": "0x64"}]

    ok, paid = pro.settle(conn, RPC(), 90, 100)
    assert ok and len(paid) == 1 and paid[0]["chat_id"] == "42"
    assert pro.settle(conn, RPC(), 90, 100) == (True, []), "the same log again credits nothing"


def test_the_cursor_moves_only_on_a_read_that_worked(conn):
    pro.quote(conn, "42")
    asked = []

    class RPC:
        fail = True

        def logs(self, a, b, **flt):
            asked.append((a, b))
            if RPC.fail:
                raise RuntimeError("rate limited")
            return []

    c = pro.Cursor()
    c.advance(conn, RPC(), 100_000)
    assert c.block == 0 and asked[-1] == (100_000 - pro.LOOKBACK_BLOCKS, 100_000), "a fresh process looks back, and a failed read leaves the cursor"
    RPC.fail = False
    c.advance(conn, RPC(), 100_000)
    assert c.block == 100_000
    c.advance(conn, RPC(), 100_050)
    assert asked[-1] == (100_001, 100_050), "then only what is new"
