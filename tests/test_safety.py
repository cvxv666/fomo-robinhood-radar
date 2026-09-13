"""Can it be sold - the check that keeps a honeypot out of every feed."""
import pytest

from fomo_agent import db
from fomo_agent.pipeline import analyze, hot, safety
from fomo_agent.sources.rpc import RpcError

TRAP = "0x" + "d" * 40      # lets you buy, not sell
FINE = "0x" + "f" * 40      # a normal token
POOL = "0x" + "1" * 40
W = ["0x" + f"{i:040x}" for i in range(1, 5)]


class FakeRpc:
    """Answers balanceOf with a balance and reverts a transfer to the pool for the trap."""

    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        tx = params[0]
        if tx["data"].startswith("0x70a08231"):           # balanceOf
            return "0x" + "1" + "0" * 20
        if tx["data"].startswith("0xa9059cbb"):           # transfer(pool, amount)
            if tx["to"] == TRAP:
                raise RpcError("eth_call: execution reverted: sell disabled")
            return "0x" + "0" * 63 + "1"
        return "0x"


class FakeGt:
    def __init__(self, buys=0, sells=0, age_s=0):
        self.buys, self.sells, self.age_s = buys, sells, age_s

    def pool(self, chain, pool):
        return {"transactions": {"h24": {"buys": self.buys, "sells": self.sells}}, "created_at": db.now() - self.age_s}


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "safety.db")
    now = db.now()
    with db.tx(c):
        for i, w in enumerate(W):
            db.upsert_trader(c, w, chain="robinhood", fomo_handle=f"w{i}", score=85, status="active")
        db.upsert_token(c, TRAP, chain="robinhood", symbol="TRAP", pool_address=POOL, liquidity_usd=20_000, created_at=now - 1200)
        db.upsert_token(c, FINE, chain="robinhood", symbol="FINE", pool_address=POOL, liquidity_usd=20_000, created_at=now - 1200)
        for mint in (TRAP, FINE):
            for i, w in enumerate(W[:3]):
                db.insert_trade(c, sig=f"{mint[:6]}{i}", address=w, chain="robinhood", mint=mint, side="buy",
                                usd_value=800.0, token_amount=1000.0, ts=now - 600 + i * 60, source="rpc")
    return c


def test_a_transfer_that_reverts_is_a_no(conn):
    rpc = FakeRpc()
    v = safety.check(conn, TRAP, rpc=rpc, gt=FakeGt(), force=True)
    assert v["sellable"] == 0 and "reverts" in v["note"]
    assert conn.execute("SELECT sellable FROM tokens WHERE mint=?", (TRAP,)).fetchone()[0] == 0
    # the probe went from a wallet that holds it, moving 1% to the pool
    method, params = [c for c in rpc.calls if c[1][0]["data"].startswith("0xa9059cbb")][0]
    assert params[0]["from"] in W[:3] and params[0]["to"] == TRAP


def test_a_transfer_that_goes_through_is_a_yes(conn):
    v = safety.check(conn, FINE, rpc=FakeRpc(), gt=FakeGt(), force=True)
    assert v["sellable"] == 1


def test_a_pool_with_buys_and_no_sells_is_a_no_when_the_chain_cannot_say(conn):
    class NoRpc:
        def call(self, *a):
            raise RpcError("down")
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=12, sells=0, age_s=3600), force=True)
    assert v["sellable"] == 0 and "not one sell" in v["note"]
    # too young to have had a sell yet: not known, not a no
    v = safety.check(conn, TRAP, rpc=NoRpc(), gt=FakeGt(buys=12, sells=0, age_s=300), force=True)
    assert v["sellable"] is None


def test_a_tracked_wallet_that_sold_settles_it(conn):
    class NoRpc:
        def call(self, *a):
            raise RpcError("down")
    with db.tx(conn):
        db.insert_trade(conn, sig="out", address=W[1], chain="robinhood", mint=FINE, side="sell",
                        usd_value=300.0, token_amount=300.0, ts=db.now() - 60, source="rpc")
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=12, sells=0, age_s=3600), force=True)
    assert v["sellable"] == 1 and "sold" in v["note"]


def test_a_no_is_final_and_a_recent_answer_is_reused(conn):
    rpc = FakeRpc()
    safety.check(conn, TRAP, rpc=rpc, gt=FakeGt(), force=True)
    n = len(rpc.calls)
    v = safety.check(conn, TRAP, rpc=rpc, gt=FakeGt())
    assert v["sellable"] == 0 and not v["asked"] and len(rpc.calls) == n


def test_an_unsellable_token_is_out_of_every_feed(conn):
    safety.check(conn, TRAP, rpc=FakeRpc(), gt=FakeGt(), force=True)
    safety.check(conn, FINE, rpc=FakeRpc(), gt=FakeGt(), force=True)
    syms = {s["sym"] for s in analyze.signals(conn, "robinhood", hours=24, limit=10)}
    assert "FINE" in syms and "TRAP" not in syms
    fresh = {t["sym"] for t in analyze.fresh(conn, "robinhood", hours=24, limit=10)["tokens"]}
    assert "TRAP" not in fresh
    burning = {h["sym"] for h in hot.hot_now(conn, "robinhood", delta=1.0, window_s=1800, min_wallets=3)}
    assert "FINE" in burning and "TRAP" not in burning
    t = analyze.analyze_token(conn, TRAP)
    assert t["sellable"] == 0 and "reverts" in t["sell_note"]


def test_the_sweep_asks_about_todays_tokens_once(conn):
    rpc = FakeRpc()
    stats = safety.sweep(conn, limit=10, rpc=rpc, gt=FakeGt())
    assert stats == {"asked": 2, "unsellable": 1, "sellable": 1, "unknown": 0}
    again = safety.sweep(conn, limit=10, rpc=rpc, gt=FakeGt())
    assert again["asked"] == 0, "a fresh verdict is not asked for again for a while"
