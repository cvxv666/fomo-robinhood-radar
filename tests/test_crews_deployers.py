"""Crews from the tape, and the creator's record from the chain."""
from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import crews, deployers, hot
from fomo_agent.sources.rpc import TRANSFER_TOPIC

W = ["0x" + f"{i:040x}" for i in range(1, 12)]
M = ["0x" + c * 40 for c in "abcdefgh"]


def roster(conn):
    with db.tx(conn):
        for i, w in enumerate(W):
            db.upsert_trader(conn, w, chain="robinhood", fomo_handle=f"w{i}", score=85, status="active")
        for i, m in enumerate(M):
            db.upsert_token(conn, m, chain="robinhood", symbol=f"T{i}", liquidity_usd=50_000)


def buy(conn, sig, w, m, ts, usd=800.0, kind="trade"):
    db.insert_trade(conn, sig=sig, address=w, chain="robinhood", mint=m, side="buy", usd_value=usd, token_amount=1.0, ts=ts, source="rpc", kind=kind)


def test_a_flock_is_one_crew_and_a_seeding_wave_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "crew_min_shared", 3)
    conn = db.connect(tmp_path / "c.db")
    roster(conn)
    now = db.now()
    with db.tx(conn):
        # W0..W3 follow the same call on five tokens, seconds apart; W4 buys the same tokens an hour later
        for k, m in enumerate(M[:5]):
            t0 = now - 6 * 3600 + k * 600
            for i in range(4):
                buy(conn, f"f{k}{i}", W[i], m, t0 + 10 * i)
            buy(conn, f"l{k}", W[4], m, t0 + 3600)
        # a seeding wave on the sixth token into W5..W9: seed fills make no crew
        for i in range(5, 10):
            buy(conn, f"s{i}", W[i], M[5], now - 3600 + 6 * i, usd=30.0, kind="seed")
    st = crews.compute(conn, days=7, now=now)
    assert st["crews"] == 1 and st["wallets"] == 4 and st["largest"] == 4
    crew = crews.of(conn, W[:6])
    assert len({crew[w] for w in W[:4]}) == 1 and W[4] not in crew and W[5] not in crew
    assert crews.distinct(conn, W[:4]) == 1 and crews.distinct(conn, W[:5]) == 2
    # the flock alone entering a new token is not a burst; with an outsider it is
    with db.tx(conn):
        for i in range(4):
            buy(conn, f"n{i}", W[i], M[6], now - 900 + 120 * i)
    monkeypatch.setattr(settings, "hot_min_span_s", 0)
    assert hot.hot_now(conn, "robinhood", delta=2.0, window_s=1800, min_wallets=3, now=now) == [], "one crew, one opinion"
    with db.tx(conn):
        buy(conn, "n9", W[9], M[6], now - 300)
    assert [h["sym"] for h in hot.hot_now(conn, "robinhood", delta=2.0, window_s=1800, min_wallets=3, now=now)] == ["T6"]


class FakeRpc:
    """A chain with two tokens: one deployed by a key, one launched through a factory."""
    def __init__(self):
        self.calls = 0

    def block_at(self, ts):
        return 1_000_000

    def block_number(self):
        return 1_010_000

    def logs(self, lo, hi, address=None, topics=None):
        self.calls += 1
        T = TRANSFER_TOPIC
        if len(topics) == 1:
            # every transfer of the token after the mint: the launch contract pays the creator first
            return [{"address": address, "transactionHash": "0xbuy", "blockNumber": hex(1_000_101), "logIndex": "0x1",
                     "topics": [T, "0x" + "0" * 24 + "e" * 40, "0x" + "0" * 24 + "c" * 40], "data": "0x" + "1".rjust(64, "0")}]
        tx = "0xdeploy" if address == M[0] else "0xfactory" if address == M[1] else None
        minted_to = ("0x" + "0" * 24 + address[2:]) if address == M[0] else "0x" + "0" * 24 + "e" * 40
        return [{"transactionHash": tx, "blockNumber": hex(1_000_100), "topics": [T, "0x" + "0" * 64, minted_to]}] if tx else []

    def call(self, method, params):
        h = params[0]
        if method == "eth_getTransactionByHash":
            if h == "0x deploy".replace(" ", ""):
                return {"from": "0xKEY" + "1" * 36, "to": None, "input": "0x60a06040" + "00" * 100}
            return {"from": "0xRELAYER" + "2" * 32, "to": "0xFACTORY" + "3" * 32,
                    "input": "0xb052ffc8" + "0" * 64 + "0" * 24 + "c" * 40 + "0" * 64}
        if h == "0xdeploy":
            return {"to": None}
        return {"to": "0xFACTORY" + "3" * 32}


def test_the_creator_is_read_once_and_a_second_token_pays_for_the_first(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "d.db")
    roster(conn)
    now = db.now()
    with db.tx(conn):
        buy(conn, "a", W[0], M[0], now - 7200)
        buy(conn, "b", W[0], M[1], now - 3600)
        buy(conn, "c", W[0], M[2], now - 600)
    rpc = FakeRpc()
    assert deployers.ensure(conn, M[0], rpc, now) == "0xkey" + "1" * 36
    assert deployers.ensure(conn, M[1], rpc, now) == "0x" + "c" * 40, "the first wallet the launch contract paid"
    calls = rpc.calls
    assert deployers.ensure(conn, M[0], rpc, now) == "0xkey" + "1" * 36 and rpc.calls == calls, "stored, not asked again"
    assert deployers.ensure(conn, M[2], rpc, now) is None
    assert conn.execute("SELECT creator FROM tokens WHERE mint=?", (M[2],)).fetchone()[0] == "", "an empty answer is stored too"
    # the key's first token was seeded; its second is not pushed
    with db.tx(conn):
        conn.execute("UPDATE tokens SET creator=? WHERE mint=?", ("0xkey" + "1" * 36, M[3]))
        conn.execute("UPDATE tokens SET first_seen_at=? WHERE mint IN (?, ?)", (now - 3600, M[0], M[3]))
        buy(conn, "s", W[1], M[0], now - 7000, usd=30.0, kind="seed")
    h = deployers.history(conn, "0xkey" + "1" * 36, before_mint=M[3])
    assert h["tokens"] == 1 and h["seeded"] == 1 and h["bad"] == 1 and h["symbols"] == ["T0"]
    why = deployers.objection(conn, M[3], rpc, now)
    assert why and "seeded" in why and "T0" in why
    assert deployers.objection(conn, M[0], rpc, now) is None, "the first token is judged on its own"
