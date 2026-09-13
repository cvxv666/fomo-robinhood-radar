"""Robinhood Chain RPC tracker.

The fixtures are real chain 4663 responses, trimmed: an `eth_getLogs` page holding both routed
fills and ordinary transfers, and the receipt of one of those fills.
"""
import json
from pathlib import Path

import pytest

from fomo_agent.config import settings
from fomo_agent.sources.rpc import (USDG, WETH, RobinhoodRPC, address_from_topic, fill_usd,
                                    parse_transfer, quote_legs, routed_fills, topic_for)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def logs():
    return load("rpc_transfer_logs.json")


@pytest.fixture
def receipt():
    return load("rpc_receipt.json")


def test_topic_round_trip():
    addr = "0x0a6ebed0155edb4b21d92ad02897a626cd90119e"
    topic = topic_for(addr.upper().replace("0X", "0x"))
    assert len(topic) == 66 and topic.startswith("0x" + "0" * 24)
    assert address_from_topic(topic) == addr


def test_parse_transfer_reads_every_log(logs):
    parsed = [parse_transfer(e) for e in logs["logs"]]
    assert all(parsed), "every fixture log is a Transfer"
    one = parsed[0]
    assert one["to"] == logs["wallet"].lower()
    assert one["raw"] > 0 and one["tx"].startswith("0x")


def test_parse_transfer_rejects_other_events():
    assert parse_transfer({"topics": [], "address": "0x1"}) is None
    assert parse_transfer({"topics": ["0xdead", "0x1", "0x2"], "address": "0x1"}) is None


def test_only_router_legs_count_as_fills(logs):
    """A wallet's log traffic is mostly airdrops; a trade is the leg facing the router."""
    transfers = [parse_transfer(e) for e in logs["logs"]]
    wallet, router = logs["wallet"].lower(), logs["router"].lower()
    fills = routed_fills(transfers, {wallet}, {router})

    assert 0 < len(fills) < len(transfers), "some legs are trades and some are not"
    assert all(f["wallet"] == wallet for f in fills.values())
    assert all(f["side"] in ("buy", "sell") for f in fills.values())
    # a transfer from a stranger is not a fill, however large
    assert not routed_fills(transfers, {wallet}, {"0xsomeoneelse"})


def test_side_follows_the_direction_of_the_router_leg():
    def leg(frm, to):
        return {"token": "0xtok", "frm": frm, "to": to, "raw": 5, "tx": f"0x{frm}{to}",
                "index": 1, "block": 100}

    fills = routed_fills([leg("0xrouter", "0xme"), leg("0xme", "0xrouter")], {"0xme"}, {"0xrouter"})
    assert sorted(f["side"] for f in fills.values()) == ["buy", "sell"]


def test_quote_legs_take_the_largest_hop(receipt):
    """The same dollars pass through several hops, so summing them would double-count."""
    legs = quote_legs(receipt)
    assert USDG in legs and legs[USDG] > 0
    raw = [parse_transfer(e) for e in receipt["logs"]]
    usdg_legs = [t["raw"] for t in raw if t and t["token"] == USDG]
    assert legs[USDG] == max(usdg_legs) / 1e6
    assert legs[USDG] < sum(usdg_legs) / 1e6


def test_fill_usd_prefers_the_stablecoin():
    assert fill_usd({USDG: 120.5, WETH: 1.0}, 2500) == 120.5
    assert fill_usd({WETH: 2.0}, 2500) == 5000
    assert fill_usd({WETH: 2.0}, None) is None, "no price, no dollar figure"
    assert fill_usd({}, 2500) is None


def test_get_trades_costs_two_log_queries_and_two_batches(logs, receipt, monkeypatch):
    """One pass over the roster: two eth_getLogs, two block probes, receipts and decimals batched."""
    wallet, router = logs["wallet"].lower(), logs["router"].lower()
    sent = []

    def fake_post(self, payload):
        sent.append(payload)
        if isinstance(payload, list):  # batched receipts, or batched decimals()
            if payload[0]["method"] == "eth_call":
                return [{"id": c["id"], "result": hex(9)} for c in payload]
            return [{"id": c["id"], "result": receipt} for c in payload]
        method = payload["method"]
        if method == "eth_blockNumber":
            return {"result": hex(1_000_000)}
        if method == "eth_getBlockByNumber":
            block = int(payload["params"][0], 16)
            return {"result": {"timestamp": hex(1_700_000_000 + block // 10)}}
        if method == "eth_getLogs":
            outgoing = payload["params"][0]["topics"][1] is not None
            return {"result": [] if outgoing else logs["logs"]}
        raise AssertionError(f"unexpected call {method}")

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")
    rpc.routers = {router}
    rpc.eth_price = 2500.0  # skip the DexScreener lookup
    rpc.prime([wallet])

    trades = rpc.get_trades(wallet, "robinhood")
    assert trades, "the routed legs became fills"
    assert {t.address for t in trades} == {wallet}
    assert all(t.source == "rpc" and t.chain == "robinhood" for t in trades)
    assert all(t.usd_value and t.usd_value > 0 for t in trades)
    assert all(t.token_amount and t.token_amount > 0 for t in trades), "sizes came off the log"
    assert all(t.ts > 1_700_000_000 for t in trades), "block numbers became timestamps"
    assert len({t.sig for t in trades}) == len(trades), "signatures are unique"

    methods = [p["method"] for p in sent if isinstance(p, dict)]
    assert methods.count("eth_getLogs") == 2
    batches = [p for p in sent if isinstance(p, list)]
    assert len(batches) == 2, "one batch for the receipts, one for the tokens' decimals"
    assert {p[0]["method"] for p in batches} == {"eth_getTransactionReceipt", "eth_call"}

    # a second pass inside the cache window costs nothing more
    before = len(sent)
    rpc.get_trades(wallet, "robinhood")
    assert len(sent) == before


def test_a_range_the_node_will_not_answer_whole_is_asked_in_halves(monkeypatch):
    """The public node caps an answer at 10,000 logs. A range over the cap comes back as an
    error; the client halves it until every piece is answered, and the pieces add up."""
    asked = []

    def fake_post(self, payload):
        f, t = int(payload["params"][0]["fromBlock"], 16), int(payload["params"][0]["toBlock"], 16)
        asked.append((f, t))
        if t - f > 250:
            return {"error": {"code": -32000, "message": "logs matched by query exceeds limit of 10000"}}
        return {"result": [{"block": b} for b in range(f, t + 1)]}

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")
    got = rpc.logs(1, 1000, topics=["0xabc"])
    assert [g["block"] for g in got] == list(range(1, 1001)), "every block once, in order"
    assert len(asked) == 7, "one refused, two halves refused, four quarters answered"


def test_a_failed_scan_is_not_retried_for_every_wallet(monkeypatch):
    """Four hundred wallets asked after one failed scan used to cost eight hundred requests."""
    calls = []

    def fake_post(self, payload):
        calls.append(payload["method"])
        if payload["method"] == "eth_blockNumber":
            return {"result": hex(1_000_000)}
        return {"error": {"code": -32000, "message": "rate limited"}}

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")
    rpc.prime(["0x" + "a" * 40, "0x" + "b" * 40])
    for w in ("0x" + "a" * 40, "0x" + "b" * 40):
        with pytest.raises(Exception):
            rpc.get_trades(w, "robinhood")
    assert calls.count("eth_getLogs") == 1, "the second wallet got the remembered failure"


def test_supports_only_its_own_chain():
    rpc = RobinhoodRPC(url="http://offline")
    assert rpc.supports("robinhood") and not rpc.supports("solana")
    assert rpc.get_trades("0xabc", "solana") == []


def test_decimals_are_asked_once_and_can_be_handed_over(monkeypatch):
    """A token's base unit is a constant, so it costs one call per process and none at all after."""
    asked = []

    def fake_post(self, payload):
        asked.append([c["params"][0]["to"] for c in payload])
        return [{"id": c["id"], "result": hex(6)} for c in payload]

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")
    a, b = "0x" + "1" * 40, "0x" + "2" * 40

    assert rpc.decimals([a, b]) == {a: 6, b: 6}
    assert rpc.decimals([a, b]) == {a: 6, b: 6}, "the second answer comes from memory"
    assert len(asked) == 1 and sorted(asked[0]) == [a, b]

    # a later process starts empty; handing it what the last one learned costs nothing
    fresh = RobinhoodRPC(url="http://offline")
    fresh.load_decimals(rpc.known_decimals())
    assert fresh.decimals([a, b]) == {a: 6, b: 6}
    assert len(asked) == 1, "nothing was asked again"


def test_a_token_that_will_not_answer_is_assumed_standard(monkeypatch):
    """A contract with no decimals(), or a nonsense one, must not poison every size it appears in."""
    replies = ["0x", None, hex(200)]
    monkeypatch.setattr(RobinhoodRPC, "_post",
                        lambda self, payload: [{"id": c["id"], "result": replies[i]}
                                               for i, c in enumerate(payload)])
    rpc = RobinhoodRPC(url="http://offline")
    mints = ["0x" + c * 40 for c in "123"]
    assert set(rpc.decimals(mints).values()) == {18}


def test_balances_ask_the_chain_what_a_wallet_actually_holds(monkeypatch):
    """balanceOf settles what the tape can only guess at, and goes out batched with the decimals."""
    sent = []

    def fake_post(self, payload):
        sent.append(payload)
        if payload[0]["params"][0]["data"] == "0x313ce567":
            return [{"id": c["id"], "result": hex(6)} for c in payload]
        # 2.5 tokens for the first pair, nothing for the second
        results = [hex(2_500_000), hex(0)]
        return [{"id": c["id"], "result": results[i]} for i, c in enumerate(payload)]

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")
    wallet, other = "0x" + "A" * 40, "0x" + "b" * 40
    held, gone = "0x" + "1" * 40, "0x" + "2" * 40

    got = rpc.balances([(wallet, held), (other, gone)])
    assert got[(wallet.lower(), held)] == pytest.approx(2.5), "raw units became tokens"
    assert got[(other, gone)] == 0, "an empty position is an answer, not a missing one"

    call = sent[-1][0]["params"][0]
    assert call["to"] == held and call["data"].startswith("0x70a08231")
    assert call["data"].endswith(wallet[2:].lower()), "the address is the padded argument"
    assert rpc.balances([]) == {} and len(sent) == 2, "nothing to ask, nothing sent"


def test_windows_walk_backwards_in_slices_the_endpoint_will_answer(monkeypatch):
    """eth_getLogs refuses more than ~200k blocks, so depth has to be asked for in pieces."""
    monkeypatch.setattr(settings, "rpc_window_blocks", 1000)

    def fake_post(self, payload):
        if payload["method"] == "eth_blockNumber":
            return {"result": hex(10_000)}
        block = int(payload["params"][0], 16)
        return {"result": {"timestamp": hex(1_700_000_000 + block)}}   # one second per block

    monkeypatch.setattr(RobinhoodRPC, "_post", fake_post)
    rpc = RobinhoodRPC(url="http://offline")

    # head is at t+10000; ask for the last 2500 seconds, i.e. 2500 blocks
    wins = list(rpc.windows(1_700_000_000 + 10_000 - 2_500))
    assert wins[0][1] == 10_000, "newest window first, so an interrupted run keeps what matters"
    assert all(last - first <= 1000 for first, last in wins), "never wider than the endpoint serves"
    assert wins[-1][0] == 7_500, "and it reaches exactly as far back as asked"
    assert all(wins[i][0] > wins[i + 1][1] for i in range(len(wins) - 1)), "no overlap, no gap > 1"


def test_windows_stop_at_the_chain_start(monkeypatch):
    monkeypatch.setattr(settings, "rpc_window_blocks", 1000)
    monkeypatch.setattr(RobinhoodRPC, "_post", lambda self, p: (
        {"result": hex(500)} if p["method"] == "eth_blockNumber"
        else {"result": {"timestamp": hex(1_700_000_000 + int(p["params"][0], 16))}}))
    rpc = RobinhoodRPC(url="http://offline")
    wins = list(rpc.windows(0))
    assert wins[-1][0] == 0, "a chain younger than the request is walked to its own beginning"
