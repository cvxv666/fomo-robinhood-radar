"""Whose trade a fill is. The fixtures are two real receipts from 2026-09-11: the swap an outside
key delivered to unipcs by calling the router itself, and a genuine FLYBRAIN buy through fomo."""
import json
import pathlib

from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import analyze, hot, provenance
from fomo_agent.sources.rpc import fill_kind

FIX = pathlib.Path(__file__).parent / "fixtures"
ROUTERS = {r.lower() for r in settings.rpc_routers}
W = ["0x" + c * 40 for c in "abcdefgh"]
SEEDED, HONEST, OLD = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40


def receipts():
    return json.loads((FIX / "rpc_receipts_gift_vs_genuine.json").read_text(encoding="utf-8"))


def test_a_swap_sent_to_the_router_itself_is_nobodys_trade():
    """fomo's relayers send to an entrypoint that calls the router. An outside key that wants a
    swap delivered to somebody else has to call the router directly, and the receipt says so."""
    r = receipts()
    assert fill_kind(r["direct_gift"], ROUTERS) == "direct"
    assert fill_kind(r["genuine_buy"], ROUTERS) == "flow"
    # the signer is a relayer in both cases, which is exactly why it is not the test
    assert r["direct_gift"]["from"] != r["genuine_buy"]["from"]


def seed(conn, now):
    with db.tx(conn):
        for i in range(4):
            db.upsert_trader(conn, W[i], chain="robinhood", fomo_handle=f"w{i}", score=85,
                             status="active")
        db.upsert_token(conn, SEEDED, chain="robinhood", symbol="SEED", liquidity_usd=4000)
        db.upsert_token(conn, HONEST, chain="robinhood", symbol="REAL", liquidity_usd=400_000)
        db.upsert_token(conn, OLD, chain="robinhood", symbol="OLD")
        # each wallet's history, on some other token: a normal size around $500
        for i in range(4):
            for k in range(5):
                db.insert_trade(conn, sig=f"h{i}{k}", address=W[i], chain="robinhood", mint=OLD,
                                side="buy", usd_value=400.0 + 50 * k, token_amount=100.0,
                                ts=now - 86400 * 3 - k * 600, source="rpc", kind="trade")
        # today: fifty cents of SEED pushed to three of them through fomo's flow, and one real buy
        for i in range(3):
            db.insert_trade(conn, sig=f"s{i}", address=W[i], chain="robinhood", mint=SEEDED,
                            side="buy", usd_value=0.6, token_amount=1e9, ts=now - 600 - i,
                            source="rpc", kind="flow")
        db.insert_trade(conn, sig="s3", address=W[3], chain="robinhood", mint=SEEDED, side="buy",
                        usd_value=700.0, token_amount=1e9, ts=now - 500, source="rpc", kind="flow")
        # and all four genuinely bought REAL an hour ago
        for i in range(4):
            db.insert_trade(conn, sig=f"r{i}", address=W[i], chain="robinhood", mint=HONEST,
                            side="buy", usd_value=900.0, token_amount=10.0, ts=now - 3600 - i,
                            source="rpc", kind="flow")


def test_dust_is_judged_against_the_wallets_own_size(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    assert provenance.refresh_medians(conn) == 4
    assert conn.execute("SELECT median_buy_usd FROM traders WHERE address=?", (W[0],)).fetchone()[0] == 500.0
    out = provenance.classify(conn, since=now - 7200)
    assert out == {"dust": 3, "trade": 5, "waves": 0}
    kinds = dict(conn.execute("SELECT sig, kind FROM trades WHERE mint=?", (SEEDED,)).fetchall())
    assert kinds == {"s0": "dust", "s1": "dust", "s2": "dust", "s3": "trade"}


def test_a_seeded_token_leaves_every_feed_and_the_honest_one_stays(tmp_path):
    """Three trusted wallets received dust: the token is seeded, and even the one real $700 buy on
    it does not put it back. REAL, bought by the same four wallets, is untouched."""
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)

    assert provenance.seeded(conn, SEEDED, now) == {"wallets": 3, "real": 1, "dust": 3, "direct": 0, "seed": 0,
                                                    "first_ts": now - 602, "seeded": True}
    assert provenance.seeded(conn, HONEST, now)["seeded"] is False

    sigs = analyze.signals(conn, "robinhood", hours=24)
    assert [s["sym"] for s in sigs] == ["REAL"]
    burning = hot.hot_now(conn, "robinhood", delta=1.5, window_s=2 * 3600, min_wallets=3, now=now)
    assert [h["sym"] for h in burning] == ["REAL"]
    fresh = analyze.fresh(conn, "robinhood", hours=24)["tokens"]
    assert "SEED" not in [t["sym"] for t in fresh]


def test_two_seeded_wallets_are_not_yet_a_pattern(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        conn.execute("DELETE FROM trades WHERE sig='s2'")
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    assert provenance.seeded(conn, SEEDED, now)["seeded"] is False
    # two dust buys and one real one: the real one alone is not two buyers, so no signal either way
    assert "SEED" not in [s["sym"] for s in analyze.signals(conn, "robinhood", hours=24)]


def test_dust_cannot_hide_a_token_the_cohort_really_bought(tmp_path):
    """The reverse attack: five dollars of dust into three famous wallets must not remove a token
    forty wallets bought for real. Seeded wallets have to outnumber real buyers."""
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        # three more trusted wallets buy SEED for real, at size: real buyers now 4, seeded 3
        for i in range(4, 7):
            db.upsert_trader(conn, W[i], chain="robinhood", fomo_handle=f"w{i}", score=80,
                             status="active")
            db.insert_trade(conn, sig=f"big{i}", address=W[i], chain="robinhood", mint=SEEDED,
                            side="buy", usd_value=800.0, token_amount=1e9, ts=now - 400 - i,
                            source="rpc", kind="flow")
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    s = provenance.seeded(conn, SEEDED, now)
    assert s["wallets"] == 3 and s["real"] == 4 and s["seeded"] is False
    assert "SEED" in [x["sym"] for x in analyze.signals(conn, "robinhood", hours=24)]


def test_a_scaled_in_buy_is_not_dust(tmp_path):
    """A trader with a $500 median buying $150 is trading. Two percent of the median is $10."""
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        db.insert_trade(conn, sig="probe", address=W[0], chain="robinhood", mint=HONEST, side="buy",
                        usd_value=150.0, token_amount=1.0, ts=now - 50, source="rpc", kind="flow")
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    assert conn.execute("SELECT kind FROM trades WHERE sig='probe'").fetchone()[0] == "trade"


def test_a_direct_fill_counts_for_nothing_even_at_full_size(tmp_path):
    """The 0x3adde gifts were $77 to $1,162 — real money, not dust. Their kind is what excludes them."""
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        for i in range(3):
            db.insert_trade(conn, sig=f"d{i}", address=W[i], chain="robinhood", mint=SEEDED,
                            side="buy", usd_value=600.0, token_amount=1e9, ts=now - 300 - i,
                            source="rpc", kind="direct")
        conn.execute("DELETE FROM trades WHERE sig IN ('s0','s1','s2','s3')")
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    assert provenance.seeded(conn, SEEDED, now)["direct"] == 3
    assert [s["sym"] for s in analyze.signals(conn, "robinhood", hours=24)] == ["REAL"]


def test_verify_fills_settles_the_old_rows_from_their_receipts(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    r = receipts()
    with db.tx(conn):
        db.insert_trade(conn, sig="0xgift:1", address=W[0], chain="robinhood", mint=SEEDED,
                        side="buy", usd_value=416.0, token_amount=1e9, ts=now - 100, source="rpc")
        db.insert_trade(conn, sig="0xreal:1", address=W[1], chain="robinhood", mint=HONEST,
                        side="buy", usd_value=900.0, token_amount=10.0, ts=now - 100, source="rpc")
        # a Codex-era row: the bare hash, no log index
        db.insert_trade(conn, sig="0xreal", address=W[2], chain="robinhood", mint=HONEST,
                        side="buy", usd_value=900.0, token_amount=10.0, ts=now - 100, source="codex")

    class FakeRpc:
        limiter = type("L", (), {"max": 45})()

        def batch(self, method, args):
            return [r["direct_gift"] if a[0] == "0xgift" else r["genuine_buy"] for a in args]

    provenance.refresh_medians(conn)
    out = provenance.verify(conn, days=1, rpc=FakeRpc())
    assert out["checked"] == 2 and out["direct"] == 1 and out["flow"] == 1
    kinds = dict(conn.execute("SELECT sig, kind FROM trades WHERE sig LIKE '0x%'").fetchall())
    assert kinds == {"0xgift:1": "direct", "0xreal:1": "trade", "0xreal": "trade"}


def test_a_gifted_fill_is_not_a_position_in_the_book(tmp_path):
    """The verdict reads the book. A honeypot delivered to the wallet must not sit in it at -100%,
    and a gifted early entry must not read as the wallet being early."""
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        db.insert_trade(conn, sig="g1", address=W[0], chain="robinhood", mint=SEEDED, side="buy",
                        usd_value=900.0, token_amount=1e9, ts=now - 200, source="rpc", kind="direct")
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    mints = {p["token"] for p in analyze.ledger(conn, W[0])}
    assert HONEST in mints and OLD in mints
    assert SEEDED not in mints, "fifty cents of dust and a gifted $900 are not a position"
    fills = analyze.analyze_trader(conn, W[0], hours=24)["fills"]
    assert {f["kind"] for f in fills if f["mint"] == SEEDED} == {"dust", "direct"}, "shown, and named"


def test_the_token_page_says_it_is_seeded(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    now = db.now()
    seed(conn, now)
    provenance.refresh_medians(conn)
    provenance.classify(conn, since=now - 7200)
    a = analyze.analyze_token(conn, SEEDED, hours=24)
    assert a["seeded"]["seeded"] is True and a["seeded"]["wallets"] == 3
    assert {f["kind"] for f in a["flow"]} == {"dust", "trade"}
