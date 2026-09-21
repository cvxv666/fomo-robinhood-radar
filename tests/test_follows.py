"""/follow: a wallet's fills to the chats that asked, real fills only, within the caps."""
from fomo_agent import db
from fomo_agent.bot import fmt_fill, handle_text
from fomo_agent.config import settings
from fomo_agent.pipeline import follows

W = "0x" + "1" * 40
W2 = "0x" + "2" * 40
MINT = "0x" + "a" * 40


def setup(conn):
    with db.tx(conn):
        db.upsert_trader(conn, W, chain="robinhood", fomo_handle="unipcs", score=96, status="active")
        db.upsert_trader(conn, W2, chain="robinhood", fomo_handle="rasmr", score=80, status="active")
        db.upsert_token(conn, MINT, chain="robinhood", symbol="JERRY")
        conn.execute("INSERT INTO bot_subscribers(chat_id, username, subscribed_at, active) VALUES('7', 'u', 0, 1)")


def test_follow_by_handle_any_case_or_address_within_the_cap(tmp_path, monkeypatch):
    # the gate is off in tests, so every chat is entitled: the PRO cap is the one in force
    monkeypatch.setattr(settings, "follow_free_max", 1)
    monkeypatch.setattr(settings, "follow_pro_max", 2)
    conn = db.connect(tmp_path / "f.db")
    setup(conn)
    ok, msg = follows.follow(conn, 7, "UNIPCS")
    assert ok and "Following unipcs" in msg and "1 of 2" in msg
    ok, msg = follows.follow(conn, 7, "unipcs")
    assert ok and "Already" in msg
    ok, msg = follows.follow(conn, 7, W2)
    assert ok and "rasmr" in msg
    ok, msg = follows.follow(conn, 7, "0x" + "3" * 40)
    assert not ok and "do not track" in msg
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "4" * 40, chain="robinhood", fomo_handle="third", score=70, status="active")
    ok, msg = follows.follow(conn, 7, "third")
    assert not ok and "the most it can" in msg
    assert [f["fomo_handle"] for f in follows.following(conn, 7)] == ["unipcs", "rasmr"]
    ok, msg = follows.unfollow(conn, 7, "rasmr")
    assert ok and len(follows.following(conn, 7)) == 1
    assert "FOLLOWING" in handle_text(conn, "/following", chat_id=7, username="u")
    assert "Unfollowed 1" in handle_text(conn, "/unfollow all", chat_id=7, username="u")


def test_only_real_fills_are_sent_once_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "follow_max_per_hour", 2)
    conn = db.connect(tmp_path / "f.db")
    setup(conn)
    follows.follow(conn, 7, "unipcs")
    now = db.now()
    with db.tx(conn):
        db.insert_trade(conn, sig="a", address=W, chain="robinhood", mint=MINT, side="buy", usd_value=2400.0, token_amount=1.0, ts=now - 20, source="rpc", kind="trade")
        db.insert_trade(conn, sig="b", address=W, chain="robinhood", mint=MINT, side="buy", usd_value=30.0, token_amount=1.0, ts=now - 15, source="rpc", kind="seed")
        db.insert_trade(conn, sig="c", address=W2, chain="robinhood", mint=MINT, side="buy", usd_value=500.0, token_amount=1.0, ts=now - 10, source="rpc", kind="trade")
        db.insert_trade(conn, sig="d", address=W, chain="robinhood", mint=MINT, side="sell", usd_value=900.0, token_amount=1.0, ts=now - 5, source="rpc", kind="trade")
        db.insert_trade(conn, sig="e", address=W, chain="robinhood", mint=MINT, side="buy", usd_value=100.0, token_amount=1.0, ts=now - 1, source="rpc", kind="trade")
    due = follows.alerts(conn, ["a", "b", "c", "d", "e"], now)
    assert [(c, f["sig"]) for c, f in due] == [("7", "a"), ("7", "d")], "a seed is not the wallet's fill, rasmr is not followed, the third is over the cap"
    assert follows.alerts(conn, ["a", "d"], now) == [], "sent once"
    text = fmt_fill(due[0][1], now)
    assert "unipcs" in text and "bought" in text and "$2.4k" in text and "$JERRY" in text and "buy on fomo" in text
    assert "sold" in fmt_fill(due[1][1], now)


def test_a_chat_that_blocked_the_bot_hears_nothing_and_nothing_is_written_down(tmp_path):
    """Six Forbiddens a night, each written down as sent: the follows of a gone chat wait for
    it to come back, and until then no fill goes near them."""
    conn = db.connect(tmp_path / "f.db")
    setup(conn)
    follows.follow(conn, 7, "unipcs")
    now = db.now()
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET active = 0 WHERE chat_id = '7'")
        db.insert_trade(conn, sig="a", address=W, chain="robinhood", mint=MINT, side="buy", usd_value=2400.0, token_amount=1.0, ts=now - 20, source="rpc", kind="trade")
    assert follows.alerts(conn, ["a"], now) == []
    assert conn.execute("SELECT COUNT(*) FROM bot_sent").fetchone()[0] == 0
    assert len(follows.following(conn, 7)) == 1, "the follow itself is kept for the day the chat is back"
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET active = 1 WHERE chat_id = '7'")
    assert [f["sig"] for _, f in follows.alerts(conn, ["a"], now)] == ["a"]


def test_the_one_tap_form_follows_too(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    setup(conn)
    assert "Following unipcs" in handle_text(conn, "/follow_UNIPCS", chat_id=7, username="u")
    assert "Already following" in handle_text(conn, "/follow_unipcs@fomoradarRH_bot", chat_id=7, username="u")
