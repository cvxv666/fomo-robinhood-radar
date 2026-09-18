"""The record reads the ledger back as verdicts and a paper run; nothing is recomputed to look better."""
from fomo_agent import db
from fomo_agent.bot import fmt_paper, fmt_record, handle_text
from fomo_agent.pipeline import pushes, record

M = ["0x" + c * 40 for c in "abcdef"]


def ledger(conn, now):
    with db.tx(conn):
        for i, m in enumerate(M):
            db.upsert_token(conn, m, chain="robinhood", symbol=f"T{i}", price_usd=1.5 if i == 0 else 0.5)
    # six alerts an hour apart; the follow-up read on five of them, the sixth still open
    reads = [
        {"best": 2.4, "peak_min": 14, "now": 1.9, "vol": 800_000},   # reached 2x
        {"best": 1.3, "peak_min": 30, "now": 0.6, "vol": 90_000},    # above entry, gave it back
        {"best": 1.0, "peak_min": 0, "now": 0.4, "vol": 40_000},     # never above
        {"best": 1.02, "peak_min": 5, "now": 1.02, "vol": 995},      # dead pool
        {"best": 1.01, "peak_min": 3, "now": 1.01, "vol": 600},      # seeded (marked below)
    ]
    ids = []
    for i, m in enumerate(M):
        pid = pushes.record(conn, "burst" if i % 2 == 0 else "launch", {"mint": m, "px": 1.0, "price": 1.0, "wallets": 6, "conviction": 4.5},
                            chats=300, now=now - (6 - i) * 3600)
        ids.append(pid)
        if i < 5:
            pushes.close(conn, pid, reads[i], now - (6 - i) * 3600 + 3600)
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "9" * 40, chain="robinhood", score=90, status="active")
        db.insert_trade(conn, sig="s1", address="0x" + "9" * 40, chain="robinhood", mint=M[4], side="buy",
                        usd_value=30.0, token_amount=100.0, ts=now - 7200, source="rpc", kind="seed")
    return ids


def test_verdicts_and_the_paper_run(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    now = db.now()
    ledger(conn, now)
    rep = record.report(conn, days=7, now=now)
    by = {r["sym"]: r for r in rep["pushes"]}
    assert [by[f"T{i}"]["verdict"] for i in range(6)] == ["2x", "above", "below", "dead", "seeded", "open"]
    assert by["T0"]["now"] == 1.5 and by["T1"]["now"] == 0.5, "now is the stored quote against the entry"
    t = rep["totals"]
    assert t["pushes"] == 6 and t["clean"] == 3 and t["above_entry"] == 2 and t["reached_2x"] == 1
    assert t["seeded"] == 1 and t["dead"] == 1
    # the paper run: $100 at 1.0, out at the hour read - seeded and dead included, open not yet
    assert t["paper"]["trades"] == 5
    assert t["paper"]["pnl"] == round(100 * (0.9 - 0.4 - 0.6 + 0.02 + 0.01), 2)
    assert t["paper"]["wins"] == 3 and t["paper"]["best"] == 90.0 and t["paper"]["worst"] == -60.0
    assert [c["sym"] for c in rep["curve"]] == ["T0", "T1", "T2", "T3", "T4"] and rep["curve"][-1]["total"] == t["paper"]["pnl"]
    assert record.report(conn, days=7, kind="launch", now=now)["totals"]["pushes"] == 3


def test_the_bot_answers_record_and_paper(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    now = db.now()
    ledger(conn, now)
    out = handle_text(conn, "/record", chat_id=1, username="u")
    assert "THE RECORD" in out and "2 of 3" in out and "reached" in out and "seeded" in out and "dead" in out
    out = handle_text(conn, "/paper", chat_id=1, username="u")
    assert "PAPER" in out and "5" in out and "won" in out
    rep = record.report(conn, days=7, now=now)
    assert "running" in fmt_paper(rep) and "$T0" in fmt_record(rep)
