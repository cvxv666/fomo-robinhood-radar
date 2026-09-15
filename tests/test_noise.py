"""Addresses that are not traders: found by fill rate, dropped, forgotten, never resolved to."""
import json

from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import hot, noise, resolve

BOT = "0x" + "b0" * 20
HUMAN = "0x" + "aa" * 20
MINT = "0x" + "11" * 20


def fills(conn, address, n, mint=MINT, now=None, usd=5.0):
    now = now or db.now()
    with db.tx(conn):
        for i in range(n):
            db.insert_trade(conn, sig=f"{address[:6]}{i}", address=address, chain="robinhood",
                            mint=mint if i % 2 else "0x" + f"{i:040x}", side="buy", usd_value=usd,
                            token_amount=1000.0, ts=now - 3500 + i % 3000, source="rpc", kind="trade")


def test_a_hyperactive_address_is_quarantined_and_its_fills_forgotten(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "noise_fills_per_hour", 100)
    conn = db.connect(tmp_path / "n.db")
    with db.tx(conn):
        db.upsert_trader(conn, BOT, chain="robinhood", fomo_handle="stav", status="tracking", tags=json.dumps({"red_flags": []}))
        db.upsert_trader(conn, HUMAN, chain="robinhood", fomo_handle="ace", status="active", score=80)
        conn.execute("INSERT INTO fomo_users(user_id, handle, onchain_address) VALUES('u1', 'stav', ?)", (BOT,))
    fills(conn, BOT, 400)
    fills(conn, HUMAN, 30)
    found = noise.sweep(conn)
    assert [f["address"] for f in found] == [BOT] and found[0]["removed"] == 400
    row = db.get_trader(conn, BOT)
    assert row["status"] == "dropped" and "bot" in json.loads(row["tags"])["red_flags"]
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE address=?", (BOT,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE address=?", (HUMAN,)).fetchone()[0] == 30, "the person keeps theirs"
    assert conn.execute("SELECT onchain_address, onchain_note FROM fomo_users WHERE user_id='u1'").fetchone()[0] is None
    assert db.noise(conn) == {BOT}
    assert noise.sweep(conn) == [], "gone, so not found again"


def test_resolution_never_picks_a_noise_address(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    with db.tx(conn):
        conn.execute("INSERT INTO noise_addresses(address, reason, ts) VALUES(?, 'bot', 1)", (BOT,))
        conn.execute("INSERT INTO fomo_users(user_id, handle) VALUES('u1', 'ace')")
        for i in range(4):
            conn.execute("INSERT INTO fomo_swaps(user_id, chain, token, ts, side, swap_id) VALUES('u1','robinhood',?,?,?,?)",
                         (MINT, 1000 + i * 100, "buy", f"s{i}"))
    # the bot is in every window; the person is in every window too, with a bystander each time
    fetch = lambda token, ts, side: {BOT, HUMAN, "0x" + f"{ts:040x}"}  # noqa: E731
    address, info = resolve.resolve_user(conn, fetch, "u1", "robinhood")
    assert address == HUMAN, info


def test_candles_in_another_unit_are_brought_into_the_fill_price(tmp_path):
    """The 32-byte-id pools' candles come back a thousand times the fill; they are evidence
    once rescaled, and a 700x that was really 0.7x reads as 0.7x."""
    conn = db.connect(tmp_path / "c.db")
    ts, px = 1_000_000, 0.001
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", price_usd=0.0007)
    thousand = [[ts + 600 * i, 1.0 - 0.05 * i, 1.1 - 0.05 * i, 0.9 - 0.05 * i, 0.95 - 0.05 * i, 0] for i in range(6)]
    o = hot.outcome(conn, MINT, ts, px, candles=thousand, now=ts + 7200)
    assert o["best"] == 1.1 and o["now"] == 0.7
    off = [[ts + 600, 0.037, 0.04, 0.03, 0.035, 0]]   # 37x the fill: no unit makes sense of it
    o2 = hot.outcome(conn, MINT, ts, px, candles=off, now=ts + 7200)
    assert o2["best"] is None and o2["now"] == 0.7, "not evidence; the tape (empty) and the quote answer"


def test_the_tape_peak_ignores_dust_and_direct_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ts, px = 1_000_000, 0.001
    with db.tx(conn):
        db.insert_trade(conn, sig="a", address=HUMAN, chain="robinhood", mint=MINT, side="buy", usd_value=50.0, token_amount=40_000.0, ts=ts + 60, source="rpc", kind="trade")
        db.insert_trade(conn, sig="b", address=BOT, chain="robinhood", mint=MINT, side="buy", usd_value=0.5, token_amount=1.0, ts=ts + 120, source="rpc", kind="dust")
        db.insert_trade(conn, sig="c", address=BOT, chain="robinhood", mint=MINT, side="sell", usd_value=700.0, token_amount=1.0, ts=ts + 180, source="rpc", kind="direct")
    o = hot.outcome(conn, MINT, ts, px, now=ts + 7200)
    assert o["best"] == 1.25 and o["fills"] == 1
