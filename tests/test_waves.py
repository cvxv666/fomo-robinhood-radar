"""The seeder's queue of 17 Sep, and the cohorts it must not be confused with."""
from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import analyze, hot, provenance

W = ["0x" + f"{i:040x}" for i in range(1, 20)]
NCAT, CPU, PAWS = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def roster(conn):
    with db.tx(conn):
        for i, w in enumerate(W):
            db.upsert_trader(conn, w, chain="robinhood", fomo_handle=f"w{i}", score=85 + (i % 10), status="active")
        db.upsert_token(conn, NCAT, chain="robinhood", symbol="NCAT", liquidity_usd=18_000)
        db.upsert_token(conn, CPU, chain="robinhood", symbol="CPU", liquidity_usd=97_000)
        db.upsert_token(conn, PAWS, chain="robinhood", symbol="PAWSINU", liquidity_usd=76_000)


def buy(conn, sig, w, mint, usd, ts, kind="flow", side="buy"):
    db.insert_trade(conn, sig=sig, address=w, chain="robinhood", mint=mint, side=side, usd_value=usd,
                    token_amount=usd * 1000, ts=ts, source="rpc", kind=kind)


def the_queue(conn, t0, sizes=None):
    """NCAT as it happened: thirteen wallets, one fill each, six seconds apart, $30 each."""
    with db.tx(conn):
        for i in range(13):
            buy(conn, f"n{i}", W[i], NCAT, (sizes or [29.7] * 13)[i], t0 + 6 * i + (i > 8) * 20)
        # the second $30 the fifth target got, as Rowdy did
        buy(conn, "n4b", W[4], NCAT, 29.7, t0 + 30)


def a_cohort(conn, t0):
    """CPU as it happened: eight wallets over sixteen minutes, mixed sizes, two of them twice."""
    with db.tx(conn):
        for i, (dt, usd) in enumerate([(0, 998), (236, 994), (504, 3859), (517, 1988), (640, 2754), (698, 6554), (799, 3025), (933, 5881)]):
            buy(conn, f"c{i}", W[i], CPU, usd, t0 + dt)
        buy(conn, "c0b", W[0], CPU, 499, t0 + 40)
        buy(conn, "c3b", W[3], CPU, 497, t0 + 530)


def a_pile_in(conn, t0):
    """PAWSINU at +874s: six wallets inside a minute, three of them buying twice - a real rush."""
    with db.tx(conn):
        for i, (dt, usd, n) in enumerate([(874, 2973, 1), (887, 4178, 3), (897, 1990, 1), (915, 1492, 3), (931, 1996, 2), (1017, 994, 1)]):
            for k in range(n):
                buy(conn, f"p{i}{k}", W[i], PAWS, usd / n, t0 + dt + 3 * k)
        for i, dt in enumerate([0, 170, 483]):
            buy(conn, f"q{i}", W[10 + i], PAWS, 900, t0 + dt)


def test_the_queue_is_a_wave_and_the_cohorts_are_not(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    the_queue(conn, now - 600)
    a_cohort(conn, now - 1500)
    a_pile_in(conn, now - 1500)
    provenance.refresh_medians(conn)
    out = provenance.classify(conn, since=now - 3600)
    assert out["waves"] == 1
    w = provenance.waves(conn, since=now - 3600, now=now, dry=True)
    assert w == [], "already marked: a wave is found once"
    kinds = {r["sig"]: r["kind"] for r in conn.execute("SELECT sig, kind FROM trades")}
    assert all(kinds[f"n{i}"] == "seed" for i in range(13)) and kinds["n4b"] == "seed", "every fill of the queue, the second one too"
    assert all(kinds[f"c{i}"] == "trade" for i in range(8)) and kinds["p11"] == "trade"
    assert provenance.seeded(conn, NCAT, now)["seeded"] is True
    assert provenance.seeded(conn, CPU, now)["seeded"] is False


def test_a_wave_fires_no_burst_and_no_launch(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    the_queue(conn, now - 600)
    a_cohort(conn, now - 1500)
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 3600)
    burning = hot.hot_now(conn, "robinhood", delta=4.0, window_s=1800, min_wallets=3, now=now)
    assert [h["sym"] for h in burning] == ["CPU"]
    fresh = analyze.fresh(conn, "robinhood", hours=24, now=now)["tokens"]
    assert "NCAT" not in [t["sym"] for t in fresh] and "CPU" in [t["sym"] for t in fresh]


def test_randomised_sizes_do_not_hide_the_queue(tmp_path):
    """PXPND: $32 to $299, a different order - still one fill each, six seconds apart."""
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    the_queue(conn, now - 600, sizes=[42, 65, 299, 199, 44, 40, 32, 100, 69, 38, 66, 47, 52])
    provenance.refresh_medians(conn)
    assert len(provenance.waves(conn, since=now - 3600, now=now, dry=True)) == 1


def test_five_whales_in_a_minute_are_not_a_wave(tmp_path):
    """The size valve: single fills, seconds apart, but $2,000 each is a rush, not a script."""
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    with db.tx(conn):
        for i in range(6):
            buy(conn, f"b{i}", W[i], CPU, 2000, now - 600 + 8 * i)
    assert provenance.waves(conn, since=now - 3600, now=now, dry=True) == []


def test_four_in_a_queue_are_not_yet_a_wave(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    with db.tx(conn):
        for i in range(4):
            buy(conn, f"b{i}", W[i], CPU, 30, now - 600 + 8 * i)
    assert provenance.waves(conn, since=now - 3600, now=now, dry=True) == []


def test_a_burst_needs_a_span_and_a_tape_that_is_not_leaving(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hot_min_span_s", 180)
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    roster(conn)
    with db.tx(conn):
        # six wallets, mixed sizes, all inside ninety seconds - and buying twice, so not a wave
        for i in range(6):
            buy(conn, f"f{i}", W[i], CPU, 800 + 100 * i, now - 300 + 15 * i, kind="trade")
            buy(conn, f"g{i}", W[i], CPU, 300, now - 290 + 15 * i, kind="trade")
    assert hot.hot_now(conn, "robinhood", delta=3.0, window_s=1800, min_wallets=3, now=now) == [], "ninety seconds is not a cohort"
    monkeypatch.setattr(settings, "hot_min_span_s", 60)
    assert [h["sym"] for h in hot.hot_now(conn, "robinhood", delta=3.0, window_s=1800, min_wallets=3, now=now)] == ["CPU"]
    with db.tx(conn):
        # Hound: the window's entrants bought $8,100 while the tape sold $5,000 of it
        for i in range(5):
            buy(conn, f"s{i}", W[10 + i], CPU, 1000, now - 200 + i, kind="trade", side="sell")
    assert hot.hot_now(conn, "robinhood", delta=3.0, window_s=1800, min_wallets=3, now=now) == [], "a distribution is not an entry"
