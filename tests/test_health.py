"""The checks that say what a person has to do, on a ledger and a tape planted for the purpose."""
import json

from fomo_agent import db
from fomo_agent.pipeline import health


def names(rows):
    return {r["name"]: r for r in rows}


def test_lost_scans_and_a_blind_watcher_raise_flags(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "h.db")
    now = db.now()
    monkeypatch.setattr(health, "router_alive", lambda conn, rpc=None: {"name": "router", "ok": True, "detail": "x"})
    with db.tx(conn):
        for i in range(10):
            good = i < 6
            conn.execute("INSERT INTO runs(kind, started_at, finished_at, stats_json) VALUES('track', ?, ?, ?)",
                         (now - 3600 * 5 + i * 1000, now - 3600 * 5 + i * 1000 + 60,
                          json.dumps({"wallets": 400 if good else 0, "errors": 0 if good else 400})))
        # fills every minute for five hours, then a forty-minute hole, then more
        t = now - 6 * 3600
        n = 0
        while t < now - 3600:
            conn.execute("INSERT INTO trades(sig, address, chain, mint, side, ts, source) VALUES(?, 'a', 'robinhood', 'm', 'buy', ?, 'rpc')", (f"s{n}", t))
            n += 1
            t += 60 if not (now - 2 * 3600 < t < now - 2 * 3600 + 2400) else 2400
    rows = names(health.checks(conn))
    assert rows["collect scans"]["ok"] is False and "4 of 10" in rows["collect scans"]["detail"] and "24h" in rows["collect scans"]["detail"]
    assert rows["watcher gaps"]["ok"] is False and "40 min" in rows["watcher gaps"]["detail"]


def test_a_quiet_healthy_day_raises_nothing_new(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "q.db")
    monkeypatch.setattr(health, "router_alive", lambda conn, rpc=None: {"name": "router", "ok": True, "detail": "x"})
    rows = names(health.checks(conn))
    assert rows["collect scans"]["ok"] and rows["watcher gaps"]["ok"]
