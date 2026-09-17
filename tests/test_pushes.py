"""The push ledger and the hour-later look back: a push is one row with the cohort's price, and
an hour on the chats that got it hear the peak, the now and the volume."""
from fomo_agent import bot, db
from fomo_agent.config import settings
from fomo_agent.pipeline import pushes
import pytest
from tests.test_bot import FakeTelegram, a_launch, conn as conn_bot  # noqa: F401

MINT = "0x" + "e" * 40


def test_a_launch_push_is_one_row_with_a_price(conn_bot, monkeypatch):
    conn = conn_bot
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(settings, "telegram_followup_min", 60)
    bot.subscribe(conn, "a", None); bot.subscribe(conn, "b", None)
    now = db.now()
    with db.tx(conn):
        db.insert_trade(conn, sig="px1", address="0x" + "a" * 40, chain="robinhood", mint=MINT, side="buy",
                        usd_value=200.0, token_amount=100_000.0, ts=now - 30, source="rpc", kind="trade")
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    assert bot.broadcast(conn, FakeTelegram())["sent"] == 2
    rows = conn.execute("SELECT * FROM pushes").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "launch" and rows[0]["chats"] == 2
    assert abs(rows[0]["px"] - 0.002) < 1e-9, "the last trusted buy's price"
    assert rows[0]["followup_due"] == rows[0]["ts"] + 3600
    bot.broadcast(conn, FakeTelegram())
    assert conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0] == 1, "the same event is not written twice"


def test_an_hour_later_the_same_chats_hear_the_peak(conn_bot, monkeypatch):
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
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [a_launch()], "hours": 6})
    bot.broadcast(conn, FakeTelegram())
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
