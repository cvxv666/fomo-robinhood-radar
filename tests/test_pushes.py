"""The push ledger and the hour-later look back: a push is one row with the cohort's price, and
an hour on the chats that got it hear the peak, the now and the volume."""
from fomo_agent import bot, db
from fomo_agent.config import settings
from fomo_agent.pipeline import pushes
import pytest
from tests.test_bot import FakeTelegram, a_launch, conn as conn_bot  # noqa: F401

MINT = "0x" + "e" * 40


def test_a_launch_is_one_row_with_a_price_and_nobody_told(conn_bot, monkeypatch):
    """Launches left the push on 22 September and kept the ledger: the row carries the cohort's
    price and `chats` of zero, and the hour-later read still measures it."""
    conn = conn_bot
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_followup_min", 60)
    bot.subscribe(conn, "a", None); bot.subscribe(conn, "b", None)
    now = db.now()
    with db.tx(conn):
        db.insert_trade(conn, sig="px1", address="0x" + "a" * 40, chain="robinhood", mint=MINT, side="buy",
                        usd_value=200.0, token_amount=100_000.0, ts=now - 30, source="rpc", kind="trade")
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    assert bot.sweep_launches(conn)["recorded"] == 1
    rows = conn.execute("SELECT * FROM pushes").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "launch" and rows[0]["chats"] == 0
    assert abs(rows[0]["px"] - 0.002) < 1e-9, "the last trusted buy's price"
    assert rows[0]["followup_due"] == rows[0]["ts"] + 3600
    bot.sweep_launches(conn)
    assert conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0] == 1, "the same event is not written twice"


def test_an_hour_later_the_same_chats_hear_the_peak(conn_bot, monkeypatch):
    """The burst is what is pushed now, so the look-back is the burst's: the chats that got it,
    still subscribed, hear what the hour did."""
    conn = conn_bot
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_followup_min", 60)
    bot.subscribe(conn, "a", None); bot.subscribe(conn, "late", None)
    now = db.now()
    with db.tx(conn):
        db.insert_trade(conn, sig="px1", address="0x" + "a" * 40, chain="robinhood", mint=MINT, side="buy",
                        usd_value=200.0, token_amount=100_000.0, ts=now - 30, source="rpc", kind="trade")
        db.upsert_token(conn, MINT, chain="robinhood", symbol="HOT", pool_address="0xpool")
        conn.execute("UPDATE bot_subscribers SET active=0 WHERE chat_id='late'")
    bot.mark_sent(conn, "a", f"hot:{MINT}")
    bot.mark_sent(conn, "late", f"hot:{MINT}")
    pushes.record(conn, "burst", {"mint": MINT, "sym": "HOT", "conviction": 4.2, "wallets": 3,
                                  "px": 0.002, "liq": 50_000.0}, 2, now)
    row = conn.execute("SELECT * FROM pushes").fetchone()
    ts = row["ts"]
    candles = [[ts - 100, 0.002, 0.0021, 0.0019, 0.002, 900.0], [ts + 1200, 0.002, 0.0037, 0.002, 0.0035, 12_000.0],
               [ts + 2700, 0.0035, 0.0036, 0.0028, 0.0029, 8_000.0]]

    class GT:
        def ohlcv(self, *a, **k):
            return candles

    tg = FakeTelegram()
    assert bot.followups(conn, tg, now=ts + 1800) == 0, "not yet"
    real = pushes.measure
    monkeypatch.setattr(pushes, "measure", lambda conn, row, gt=None, now=None: real(conn, row, GT(), now))
    sent = bot.followups(conn, tg, now=ts + 3601)
    assert sent == 1 and tg.sent[0][0] == "a", "the chat that got the push and is still here"
    text = tg.sent[0][1]
    assert "60 min after the push" in text and "\u00d71.85" in text and "at +22 min" in text and "\u00d71.45" in text and "$20.9k" in text
    closed = conn.execute("SELECT * FROM pushes").fetchone()
    assert closed["followup_at"] and closed["best"] == 1.85 and closed["peak_min"] == 22 and closed["vol_usd"] == 20900
    assert bot.followups(conn, tg, now=ts + 3700) == 0, "told once"


def test_a_launch_nobody_was_sent_is_measured_and_told_to_nobody(conn_bot, monkeypatch):
    """The shadow rows keep the question measurable; the hour-later message is for the chats that
    got something, and a launch went to none."""
    from fomo_agent.pipeline import record
    conn = conn_bot
    monkeypatch.setattr(settings, "telegram_followup_min", 60)
    now = db.now()
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", symbol="HOT", pool_address="0xpool")
        # a launch takes its entry from the last trusted buy, as the push did
        db.insert_trade(conn, sig="px9", address="0x" + "a" * 40, chain="robinhood", mint=MINT, side="buy",
                        usd_value=200.0, token_amount=100_000.0, ts=now - 30, source="rpc", kind="trade")
    pid = pushes.record(conn, "launch", {"mint": MINT, "sym": "HOT", "heat": 3.0, "buyers": 4}, 0, now, shadow=True)
    assert conn.execute("SELECT COUNT(*) FROM x_posts").fetchone()[0] == 0
    candles = [[now - 100, 0.002, 0.0021, 0.0019, 0.002, 900.0],
               [now + 1200, 0.002, 0.004, 0.002, 0.0035, 12_000.0],
               [now + 2700, 0.0035, 0.0036, 0.003, 0.0031, 8_000.0]]

    class GT:
        def ohlcv(self, *a, **k):
            return candles

    real = pushes.measure
    monkeypatch.setattr(pushes, "measure", lambda conn, row, gt=None, now=None: real(conn, row, GT(), now))
    tg = FakeTelegram()
    assert bot.followups(conn, tg, now=now + 3601) == 0 and tg.sent == []
    row = conn.execute("SELECT followup_at, best FROM pushes WHERE id=?", (pid,)).fetchone()
    assert row["followup_at"] and row["best"] == 2.0, "measured all the same"
    assert record.report(conn, days=30)["totals"]["pushes"] == 0
    assert record.report(conn, days=30, sent_only=False)["totals"]["reached_2x"] == 1
