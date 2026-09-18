"""The trail read: out at the first candle that dips a fifth under the running high, else the last close."""
from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import pushes, record
from fomo_agent.pipeline.hot import outcome

MINT = "0x" + "a" * 40


def test_the_trail_exits_at_the_pullback_or_the_close(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trail_drop", 0.2)
    monkeypatch.setattr(settings, "outcome_min_candle_volume_usd", 0)
    conn = db.connect(tmp_path / "t.db")
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", symbol="T", price_usd=1.0)
    ts = 1_000_000
    # entry 1.0: up to 1.5, up to 2.0, then a candle whose low is 1.5 - under 1.6, the trail sells there
    candles = [[ts, 1.0, 1.5, 0.95, 1.4, 5000], [ts + 300, 1.4, 2.0, 1.7, 1.9, 5000], [ts + 600, 1.9, 1.95, 1.5, 1.6, 5000], [ts + 900, 1.6, 1.7, 1.55, 1.65, 5000]]
    o = outcome(conn, MINT, ts, 1.0, 3600, ts + 3600, candles)
    assert o["best"] == 2.0 and o["now"] == 1.65 and o["trail"] == 1.6
    # never a fifth under the high: out at the last close
    calm = [[ts, 1.0, 1.2, 0.98, 1.1, 5000], [ts + 300, 1.1, 1.3, 1.05, 1.25, 5000]]
    assert outcome(conn, MINT, ts, 1.0, 3600, ts + 3600, calm)["trail"] == 1.25
    # a dip straight from the entry: the stop is under the entry itself
    dive = [[ts, 1.0, 1.05, 0.7, 0.75, 5000]]
    assert outcome(conn, MINT, ts, 1.0, 3600, ts + 3600, dive)["trail"] == 0.8


def test_the_record_carries_both_paper_runs(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    now = db.now()
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", symbol="T", price_usd=1.0)
    pid = pushes.record(conn, "burst", {"mint": MINT, "px": 1.0, "wallets": 5, "conviction": 4.2}, chats=10, now=now - 7200)
    pushes.close(conn, pid, {"best": 2.0, "peak_min": 10, "now": 0.9, "vol": 50_000, "trail": 1.6}, now - 3600)
    rep = record.report(conn, days=1, now=now)
    r = rep["pushes"][0]
    assert r["paper"] == -10.0 and r["paper_trail"] == 60.0
    assert rep["totals"]["paper"]["pnl"] == -10.0 and rep["totals"]["paper_trail"]["pnl"] == 60.0
    assert rep["curve_trail"][-1]["total"] == 60.0 and rep["trail_drop"] == settings.trail_drop
