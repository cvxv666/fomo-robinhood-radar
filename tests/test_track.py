

def test_a_lost_roster_scan_is_one_warning_not_one_per_wallet(tmp_path, caplog):
    """Three lost scans wrote 1,596 lines; the fact is one line and a count."""
    import logging
    from fomo_agent import db
    from fomo_agent.pipeline import track

    conn = db.connect(tmp_path / "t.db")
    with db.tx(conn):
        for i in range(5):
            db.upsert_trader(conn, "0x" + f"{i:040x}", chain="robinhood", status="active")

    class Broken:
        requests = 0

        def supports(self, chain):
            return True

        def get_trades(self, address, chain="robinhood", since_ts=None):
            raise RuntimeError("scan failed earlier this pass: rate limited after 4 attempts")

    with caplog.at_level(logging.WARNING):
        stats = track.track_all(conn, trackers=[Broken()])
    assert stats["errors"] == 5 and stats["skipped"] == 5
    lines = [r.message for r in caplog.records if "scan failed" in r.message or "skipped" in r.message]
    assert lines == ["track: 5 wallets skipped after the roster scan failed this pass"]
