"""Can it be sold - the check that keeps a honeypot out of every feed."""
import pytest

from fomo_agent import db
from fomo_agent.config import settings
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


def test_the_silence_speaks_sooner_the_more_have_bought(conn, monkeypatch):
    """ZEC: fifty buyers and no seller at minute 20 went out as a launch; the rule waited for 30."""
    monkeypatch.setattr(settings, "sell_silence_min_buys", 8)
    monkeypatch.setattr(settings, "sell_silence_min_age_s", 1800)
    monkeypatch.setattr(settings, "sell_silence_floor_s", 300)
    assert safety.silence_wait_s(8) == 1800 and safety.silence_wait_s(4) == 1800
    assert safety.silence_wait_s(16) == 900 and safety.silence_wait_s(48) == 300 and safety.silence_wait_s(91) == 300

    class NoRpc:
        def call(self, *a):
            raise RpcError("down")
    # fifty buys, no sell, twenty minutes in: a no now, not at thirty
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=50, sells=0, age_s=1200), force=True)
    assert v["sellable"] == 0 and "50 buys" in v["note"]
    # eight buys at twenty minutes is still early
    v = safety.check(conn, TRAP, rpc=NoRpc(), gt=FakeGt(buys=8, sells=0, age_s=1200), force=True)
    assert v["sellable"] is None


def test_a_silence_verdict_is_asked_again_and_a_first_sell_lifts_it(conn):
    class NoRpc:
        def call(self, *a):
            raise RpcError("down")
    now = db.now()
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=50, sells=0, age_s=1200), now=now, force=True)
    assert v["sellable"] == 0
    # inside its while the stored no is reused, like any answer
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=60, sells=3, age_s=1500), now=now + 60)
    assert v["sellable"] == 0 and not v["asked"]
    # past it, the pool is asked again, and three sells make it a yes; a revert stays final
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=60, sells=3, age_s=1500), now=now + 700)
    assert v["sellable"] == 1 and v["asked"] and "3 sells" in v["note"]
    safety.check(conn, TRAP, rpc=FakeRpc(), gt=FakeGt(), now=now, force=True)
    v = safety.check(conn, TRAP, rpc=FakeRpc(), gt=FakeGt(buys=60, sells=3, age_s=1500), now=now + 700)
    assert v["sellable"] == 0 and not v["asked"]


def test_a_pool_that_is_an_id_is_probed_through_the_router_and_only_a_revert_counts(conn, monkeypatch):
    """A v4-style pool has no account; the sell's first transfer goes to the router instead."""
    router = "0x" + "9" * 40
    monkeypatch.setattr(settings, "rpc_routers", (router,))
    with db.tx(conn):
        conn.execute("UPDATE tokens SET pool_address = ? WHERE mint IN (?, ?)", ("0x" + "ab" * 32, TRAP, FINE))
    rpc = FakeRpc()
    v = safety.check(conn, TRAP, rpc=rpc, gt=FakeGt(), force=True)
    assert v["sellable"] == 0 and "router reverts" in v["note"]
    _, params = [c for c in rpc.calls if c[1][0]["data"].startswith("0xa9059cbb")][0]
    assert params[0]["data"][10:74] == "0" * 24 + "9" * 40, "the transfer went to the router"
    # a transfer to the router that goes through is not a yes: the trap may be keyed elsewhere,
    # and the pool's own count decides
    v = safety.check(conn, FINE, rpc=FakeRpc(), gt=FakeGt(buys=50, sells=0, age_s=1200), force=True)
    assert v["sellable"] == 0 and "not one sell" in v["note"]
    v = safety.check(conn, FINE, rpc=FakeRpc(), gt=FakeGt(buys=50, sells=5, age_s=1200), force=True)
    assert v["sellable"] == 1 and "5 sells" in v["note"]


def test_a_pushed_token_that_proves_unsellable_voids_the_push_to_the_chats_that_got_it(conn, monkeypatch):
    from fomo_agent.pipeline import pushes, webhooks
    from tests.test_bot import FakeTelegram

    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    fired = []
    monkeypatch.setattr(webhooks, "fire", lambda conn, event, item, now=None, wait=False: fired.append((event, item)) or 0)
    tg = FakeTelegram()
    monkeypatch.setattr("fomo_agent.bot.Telegram", lambda *a, **k: tg)
    now = db.now()
    with db.tx(conn):
        for c, active in (("7", 1), ("8", 1), ("9", 0)):
            conn.execute("INSERT INTO bot_subscribers(chat_id, username, subscribed_at, active) VALUES(?, 'u', 0, ?)", (c, active))
            conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?, ?, ?)", (c, FINE, now - 960))
    pushes.record(conn, "launch", {"mint": FINE, "sym": "FINE", "heat": 3.0, "buyers": 4}, 3, now - 960)

    class NoRpc:
        def call(self, *a):
            raise RpcError("down")
    v = safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=91, sells=0, age_s=2160), now=now, force=True)
    assert v["sellable"] == 0
    assert sorted(c for c, _ in tg.sent) == ["7", "8"], "the chats that got the push and are still here"
    text = tg.sent[0][1]
    assert "unsellable, 16m after the push" in text and "91 buys and not one sell" in text and "honeypot" in text
    assert [e for e, _ in fired] == ["void"] and fired[0][1]["minutes_since_push"] == 16 and fired[0][1]["sym"] == "FINE"
    # once: the next look at the same token says nothing more, to anyone
    safety.check(conn, FINE, rpc=NoRpc(), gt=FakeGt(buys=95, sells=0, age_s=2400), now=now + 700, force=True)
    assert len(tg.sent) == 2 and len(fired) == 1
    assert conn.execute("SELECT COUNT(*) FROM bot_sent WHERE mint = ?", ("void:" + FINE,)).fetchone()[0] == 2
    # a token nobody was told about is nobody's business
    v = safety.check(conn, TRAP, rpc=NoRpc(), gt=FakeGt(buys=91, sells=0, age_s=2160), now=now, force=True)
    assert v["sellable"] == 0 and len(tg.sent) == 2
