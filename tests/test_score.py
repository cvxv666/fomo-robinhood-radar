

def test_received_tokens_are_marked_unearned_and_empty_wallets_are_not_exported(tmp_path):
    from fomo_agent import db
    from fomo_agent.pipeline import score
    conn = db.connect(tmp_path / "u.db")
    w = "0x" + "e5" * 20
    with db.tx(conn):
        db.upsert_trader(conn, w, chain="robinhood", fomo_handle="rich", fomo_user_id="u9", status="tracking")
        conn.execute("INSERT INTO fomo_users(user_id, handle) VALUES('u9', 'rich')")
        db.upsert_token(conn, "0x" + "77" * 20, chain="robinhood", symbol="GIFT")
        db.upsert_token(conn, "0x" + "88" * 20, chain="robinhood", symbol="REAL")
        conn.execute("INSERT INTO fomo_positions(trade_id, user_id, chain, token, symbol, unrealized_pnl, cost_basis) VALUES('p1','u9','robinhood',?, 'GIFT', 4600000, NULL)", ("0x" + "77" * 20,))
        conn.execute("INSERT INTO fomo_positions(trade_id, user_id, chain, token, symbol, unrealized_pnl, cost_basis) VALUES('p2','u9','robinhood',?, 'REAL', 5000, 2500)", ("0x" + "88" * 20,))
        db.upsert_trader(conn, "0x" + "f6" * 20, chain="robinhood", fomo_handle="ghost", status="tracking")
    ctx = score.build_context(conn, w)
    by = {p["symbol"]: p for p in ctx["open_positions"]}
    assert by["GIFT"]["unearned"] is True and by["REAL"]["unearned"] is False
    assert ctx["unearned_pnl"] == {"positions": 1, "usd": 4600000}
    out = score.export_contexts(conn, tmp_path / "p.json", unscored_only=True)
    assert out["exported"] == 1 and out["nothing_to_judge"] == 1


def test_a_distributing_wallet_is_watch_at_most_and_the_card_says_so(tmp_path, monkeypatch):
    """ArtofConviction: +$261k by fomo, $125k sold against $11k bought in the week. The model
    hears it in the context, the verdict is capped at watch, and the profile carries the reason."""
    from fomo_agent import db, bot
    from fomo_agent.config import settings
    from fomo_agent.models import ScoreResult
    from fomo_agent.pipeline import analyze, score
    monkeypatch.setattr(settings, "distrib_min_sold_usd", 50_000)
    monkeypatch.setattr(settings, "distrib_ratio", 5.0)
    monkeypatch.setattr(settings, "pnl_gap_usd", 100_000)
    conn = db.connect(tmp_path / "d.db")
    w, mint, now = "0x" + "a7" * 20, "0x" + "55" * 20, db.now()
    with db.tx(conn):
        db.upsert_trader(conn, w, chain="robinhood", fomo_handle="artof", status="tracking", pnl_30d=260_934.0,
                         stats_json='{"realized_pnl": -67998, "win_rate": 0.26}')
        db.upsert_token(conn, mint, chain="robinhood", symbol="BAG")
        for i in range(14):
            db.insert_trade(conn, sig=f"b{i}", address=w, chain="robinhood", mint=mint, side="buy", usd_value=750.0, ts=now - 86400 * 3 - i, source="rpc")
        for i in range(37):
            db.insert_trade(conn, sig=f"s{i}", address=w, chain="robinhood", mint=mint, side="sell", usd_value=3400.0, ts=now - 86400 * 2 - i, source="rpc")
    ctx = score.build_context(conn, w)
    assert "distributing" in ctx["flags"]["distributing"] and "$125.8k" in ctx["flags"]["distributing"]
    res = ScoreResult(score=82, status="active", style=["swing"], red_flags=[], summary="Big fomo PnL, repeatable.", confidence=0.7)
    score.apply_score(conn, w, res, "test")
    row = db.get_trader(conn, w)
    assert row["status"] == "watch" and "Held to watch: distributing" in row["ai_summary"]
    assert "distributing" in row["tags"]
    a = analyze.analyze_trader(conn, "artof")
    assert a["distributing"] and a["flow_7d"]["sold"] == 37 * 3400.0
    assert a["pnl_gap"]["gap"] == 260_934.0 + 67_998
    card = bot.fmt_trader(a)
    assert "watch" in card and "apart" in card and "fomo counts open positions" in card
    # a wallet that buys and sells alike is trading, not distributing; a small seller neither
    assert analyze.distributing(60_000, 70_000) is None and analyze.distributing(100, 20_000) is None
