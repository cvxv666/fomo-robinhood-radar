"""The morning board: the record's numbers, the template filled, the post under 280 characters."""
from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import board, pushes

M = ["0x" + c * 40 for c in "abcde"]


def a_day(conn, now):
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "9" * 40, chain="robinhood", fomo_handle="unipcs", score=90, status="active")
        for i, m in enumerate(M):
            db.upsert_token(conn, m, chain="robinhood", symbol=f"T{i}", price_usd=0.5 if i else 2.0)
            db.insert_trade(conn, sig=f"t{i}", address="0x" + "9" * 40, chain="robinhood", mint=m, side="buy",
                            usd_value=1500.0, token_amount=1.0, ts=now - (5 - i) * 3600 - 60, source="rpc", kind="trade")
    reads = [{"best": 2.4, "peak_min": 14, "now": 1.9, "vol": 800_000}, {"best": 1.3, "peak_min": 30, "now": 0.6, "vol": 90_000},
             {"best": 1.0, "peak_min": 0, "now": 0.4, "vol": 40_000}, {"best": 1.02, "peak_min": 5, "now": 1.02, "vol": 995},
             {"best": 1.5, "peak_min": 9, "now": 1.2, "vol": None}]
    for i, m in enumerate(M):
        pid = pushes.record(conn, "burst" if i % 2 == 0 else "launch", {"mint": m, "px": 1.0, "price": 1.0, "wallets": 6, "conviction": 4.5},
                            chats=300, now=now - (5 - i) * 3600)
        pushes.close(conn, pid, reads[i], now - (5 - i) * 3600 + 3600)


def test_the_board_reads_the_record_and_writes_a_post(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "public_site_url", "https://fomoradar.app")
    conn = db.connect(tmp_path / "b.db")
    now = db.now()
    a_day(conn, now)
    d = board.data(conn, hours=24, now=now)
    # four judged (the dead pool is listed, not judged), three above, one at 2x
    assert d["hero"]["n"] == 3 and d["hero"]["of"] == 4 and d["judged"]["two"] == 1
    assert d["judged"]["best"]["sym"] == "T0"
    assert [r["sym"] for r in d["rows"]] == ["T0", "T1", "T2", "T3", "T4"] and d["more"] == 0
    assert [r["note"] for r in d["rows"]] == ["", "", "", "DEAD POOL", ""]
    assert d["rows"][0]["usd"] == "$1.5K" and d["rows"][0]["now"] == 2.0
    assert d["tape"]["fills"] == 5 and d["stats"][3]["v"] == "$7.5K"
    assert d["timeline"]["marks"][3]["dead"] is True and d["timeline"]["marks"][0]["dead"] is False
    assert "FomoBrainRH" in d["foot_right"] and "fomoradar.app" in d["foot_right"]
    html = board.html(d)
    assert "window.DAY = {" in html and '"sym": "T0"' in html and "/*DAY*/" not in html
    text = board.post_text(d)
    assert len(text) <= 280
    assert "4 alerts · 3 traded above the call · 1 hit ×2 · best ×2.40 ($T0, 14 min in)" in text
    assert text.startswith("The day on Robinhood Chain") and text.endswith("Live calls: bot in bio")


def test_a_quiet_day_still_makes_a_board(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    d = board.data(conn, hours=24)
    assert d["hero"]["quiet"] and d["rows"] == [] and d["section"] == "NO ALERTS"
    assert "quiet" in board.post_text(d).lower() and len(board.post_text(d)) <= 280
    assert d["subtitle"] == "THE DAY" and board.data(conn, hours=168)["subtitle"] == "THE WEEK"
