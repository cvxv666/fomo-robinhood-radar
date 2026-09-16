

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
