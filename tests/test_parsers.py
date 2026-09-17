import json
from pathlib import Path

import pytest

from fomo_agent import db
from fomo_agent.models import ScoreResult
from fomo_agent.pipeline.score import parse_score
from fomo_agent.sources.dexscreener import filter_pairs
from fomo_agent.sources.filters import filter_tokens
from fomo_agent.sources.geckoterminal import parse_pool
from fomo_agent.sources.codex import parse_maker_event, parse_result
from fomo_agent.sources.fomo import (parse_execution_addresses, parse_holders, parse_leaderboard,
                                     parse_top_holdings, parse_trade_rows)
from fomo_agent.sources.trenches import parse_fill, parse_trader
from fomo_agent.sources.helius import parse_swap

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ---------- helius ----------

def test_helius_swap_event_buy_and_sell():
    txs = load("helius_swap_sample.json")
    buy = parse_swap(txs[0], "WALLET_A")
    assert buy and buy.side == "buy" and buy.mint == "MINT_X"
    assert buy.sol_amount == pytest.approx(1.5)
    assert buy.token_amount == pytest.approx(123456.0)

    sell = parse_swap(txs[1], "WALLET_A")
    assert sell and sell.side == "sell" and sell.sol_amount == pytest.approx(2.2)


def test_helius_fallback_transfers():
    txs = load("helius_swap_sample.json")
    t = parse_swap(txs[2], "WALLET_A")
    assert t and t.side == "buy" and t.mint == "MINT_Y" and t.sol_amount == pytest.approx(0.5)


def test_helius_ignores_other_wallet_and_token_to_token():
    txs = load("helius_swap_sample.json")
    assert parse_swap(txs[3], "WALLET_A") is None
    assert parse_swap(txs[4], "WALLET_A") is None


# ---------- dexscreener ----------

def test_dexscreener_filter():
    pairs = load("dexscreener_pairs_sample.json")
    now = 1756900000 + 3600 * 5  # 5h after creation
    got = filter_pairs(pairs, min_mcap=5_000_000, max_age_hours=24, now_ts=now)
    assert [(t.chain, t.mint) for t in got] == [("robinhood", "0xmeme"), ("solana", "MINT_BIG"), ("base", "0xbase")]
    big = next(t for t in got if t.mint == "MINT_BIG")
    assert big.liquidity_usd == 250000  # best pair by liquidity wins
    assert big.created_at == 1756900000


# ---------- geckoterminal ----------

def test_gecko_parse_and_filter():
    from datetime import datetime, timezone

    pools = load("gecko_pools_sample.json")["data"]
    toks = [t for t in map(parse_pool, pools) if t]
    assert len(toks) == 3  # broken entry dropped
    meme = next(t for t in toks if t.mint == "0xmeme")
    assert meme.chain == "robinhood" and meme.symbol == "MEME" and meme.source == "geckoterminal"
    assert meme.mcap_usd == pytest.approx(110621631.5)  # mcap "0.0" -> fdv fallback
    assert meme.created_at == int(datetime(2026, 9, 4, 4, 10, 31, tzinfo=timezone.utc).timestamp())
    now = meme.created_at + 3600 * 5
    got = filter_tokens(toks, min_mcap=500_000, max_age_hours=24, now_ts=now)
    assert [t.mint for t in got] == ["0xmeme"]  # CHUMP too old, DUST too small
    assert filter_tokens(toks, min_mcap=500_000, max_age_hours=24, min_liquidity=5_000_000, now_ts=now) == []
    assert filter_tokens(toks, min_mcap=500_000, max_age_hours=24, max_mcap=100_000_000, now_ts=now) == []


def test_cross_source_dedupe_is_case_insensitive():
    from fomo_agent.sources.dexscreener import parse_pair

    dex = parse_pair({"chainId": "robinhood", "baseToken": {"address": "0xAbC", "symbol": "X"},
                      "liquidity": {"usd": 100}, "marketCap": 1_000_000, "pairCreatedAt": 1756900000000})
    gecko = parse_pool({"attributes": {"name": "X / WETH", "pool_created_at": "2026-09-03T12:26:40Z",
                                       "fdv_usd": "1000000", "reserve_in_usd": "500"},
                        "relationships": {"base_token": {"data": {"id": "robinhood_0xabc"}}}})
    got = filter_tokens([dex, gecko], min_mcap=1, max_age_hours=24, now_ts=1756900000 + 60)
    assert len(got) == 1 and got[0].source == "geckoterminal"  # higher liquidity wins


def test_dexscreener_carries_the_price_that_marks_a_position():
    """The quote arrives as a string in the same response the liquidity comes from."""
    from fomo_agent.sources.dexscreener import parse_pair

    t = parse_pair({"chainId": "robinhood", "baseToken": {"address": "0xAbC", "symbol": "X"},
                    "liquidity": {"usd": 100}, "priceUsd": "0.00004212"})
    assert t.price_usd == pytest.approx(0.00004212)
    assert parse_pair({"chainId": "robinhood", "baseToken": {"address": "0xAbC"}}).price_usd is None


def test_geckoterminal_token_lookup_answers_everything_at_once():
    """One entry of /tokens/multi carries the name, the base unit, the price and the depth."""
    from fomo_agent.sources.geckoterminal import parse_token

    t = parse_token({"attributes": {
        "address": "0x013E4B9b74C33Bab243dA01217c8184b75BE1de0", "name": "MOMO", "symbol": "MOMO",
        "decimals": 18, "price_usd": "0.000003429806871", "fdv_usd": "3429.806870627",
        "total_reserve_in_usd": "361.574036719", "market_cap_usd": None,
    }}, "robinhood")
    assert t.mint == "0x013e4b9b74c33bab243da01217c8184b75be1de0", "addresses are normalised"
    assert t.symbol == "MOMO" and t.decimals == 18
    assert t.price_usd == pytest.approx(0.000003429806871)
    assert t.mcap_usd == pytest.approx(3429.806870627), "FDV stands in when mcap is null"
    assert t.liquidity_usd == pytest.approx(361.574036719)

    assert parse_token({"attributes": {}}, "robinhood") is None, "no address, no token"
    odd = parse_token({"attributes": {"address": "0xa", "decimals": 999}}, "robinhood")
    assert odd.decimals is None, "a nonsense base unit is refused rather than believed"


# ---------- codex ----------

def test_codex_parse():
    res = load("codex_filtertokens_sample.json")["data"]["filterTokens"]["results"]
    toks = [t for t in map(parse_result, res) if t]
    assert len(toks) == 2
    meme, googl = toks
    assert meme.chain == "robinhood" and meme.mint == "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
    assert meme.mcap_usd == pytest.approx(110836271.2) and meme.liquidity_usd == pytest.approx(3612199.5)
    assert meme.created_at == 1788495031 and meme.source == "codex" and meme.dex == "Uniswap V4"
    assert meme.sniper_pct == pytest.approx(4.1) and meme.holders == 5400
    assert googl.chain == "solana" and googl.mint.startswith("4rkNS")


# ---------- codex wallet tracking ----------

def test_codex_maker_events_parse():
    items = load("codex_maker_events_sample.json")["data"]["getTokenEventsForMaker"]["items"]
    addr = "Eh5RHDhjyiuXSMRsAXFvXvbV5i1waBtKjg1vM5KrYcgX"
    trades = [t for t in (parse_maker_event(e, addr) for e in items) if t]
    # the token-to-token event (no liquidityToken) is dropped
    assert len(trades) == 3

    sell = trades[0]
    assert sell.side == "sell" and sell.chain == "solana"
    assert sell.mint == "bKU4TGmXxaMmcjL2htnSKfRT9Voig9KmPvo8Scupump"   # the non-quote leg
    assert sell.token_amount == pytest.approx(438.616993)
    assert sell.usd_value == pytest.approx(322.91126, rel=1e-5)
    assert sell.sol_amount == pytest.approx(3.0993823)                  # |amount1| / 1e9
    assert sell.address == addr and sell.source == "codex"

    buy = trades[1]
    assert buy.side == "buy" and buy.sig == "SIG_BUY_CODEX"
    assert buy.sol_amount == pytest.approx(3.627927324)

    evm = trades[2]
    assert evm.chain == "robinhood"
    assert evm.mint == "0x385f4f8ae47651ce5f58f5265395a669f8281e18"      # lowercased
    assert evm.sol_amount is None                                        # no SOL leg off Solana
    assert evm.usd_value == pytest.approx(1500.5)


def test_codex_maker_event_rejects_incomplete():
    base = {"transactionHash": "S", "timestamp": 1, "eventDisplayType": "Buy",
            "token0Address": "A", "token1Address": "So11111111111111111111111111111111111111112",
            "liquidityToken": "So11111111111111111111111111111111111111112",
            "data": {"amountNonLiquidityToken": "1", "amount1": "1000000000"}}
    assert parse_maker_event(base, "W") is not None
    assert parse_maker_event({**base, "eventDisplayType": "Mint"}, "W") is None
    assert parse_maker_event({**base, "data": {}}, "W") is None
    assert parse_maker_event({**base, "liquidityToken": "OTHER"}, "W") is None


# ---------- manual scoring round-trip ----------

def test_manual_score_import(tmp_path):
    from fomo_agent import db as _db
    from fomo_agent.pipeline.score import export_contexts, import_results

    conn = _db.connect(tmp_path / "s.db")
    _db.upsert_trader(conn, "W1", source="manual", status="tracking")
    _db.insert_trade(conn, sig="s1", address="W1", chain="solana", mint="M", side="buy", ts=_db.now(), sol_amount=1.0)
    conn.commit()

    out = tmp_path / "pending.json"
    assert export_contexts(conn, out, force=True)["exported"] == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["wallets"][0]["address"] == "W1" and "instructions" in payload

    results = tmp_path / "res.json"
    results.write_text(json.dumps([
        {"address": "W1", "score": 80, "status": "active", "style": ["sniper"], "red_flags": [],
         "summary": "ok", "confidence": 0.7},
        {"address": "GHOST", "score": 10, "status": "dropped", "summary": "x", "confidence": 0.1},
        {"address": "W1", "score": 999, "status": "active", "summary": "bad", "confidence": 0.5},
    ]), encoding="utf-8")
    stats = import_results(conn, results, "manual:test")
    assert stats == {"imported": 1, "invalid": 1, "unknown_address": 1, "by_status": {"active": 1}}

    row = _db.get_trader(conn, "W1")
    assert row["score"] == 80 and row["status"] == "active" and row["ai_model"] == "manual:test"
    hist = conn.execute("SELECT COUNT(*) FROM score_history WHERE address='W1'").fetchone()[0]
    assert hist == 1


# ---------- fomo (phase 0 fixtures, recorded from a real session) ----------

def test_fomo_leaderboard_parse():
    rows = parse_leaderboard(load("fomo_leaderboard_sample.json"), "30d")
    assert len(rows) == 3
    top = rows[0]
    assert top.fomo_handle == "unipcs"
    assert top.fomo_user_id == "36adb85a-c0fd-5fa8-916d-8fdc32fe4237"
    assert top.pnl_30d == pytest.approx(16881439.25, rel=1e-6)
    assert top.pnl_7d is None and top.pnl_24h is None      # only the queried window is filled
    assert top.trades_cnt == 2653
    assert top.evm_address == top.evm_address.lower()      # EVM addresses normalized
    assert top.source == "leaderboard_30d"
    # the profile address is recorded but is NOT the on-chain trading wallet
    assert top.profile_address == top.address


def test_fomo_holders_parse():
    mint = "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
    rows = parse_holders(load("fomo_holders_sample.json"), mint)
    assert len(rows) == 3
    h = rows[0]
    assert h.fomo_handle == "frankdegods" and h.mint == mint
    assert h.pnl_usd == pytest.approx(890695.48)
    assert h.cost_basis_usd == pytest.approx(1491.28)
    assert h.avg_hold_seconds == 115087 and h.is_dev is False
    # a different mint must not match this payload
    assert parse_holders(load("fomo_holders_sample.json"), "0xdeadbeef") == []


def test_fomo_execution_addresses():
    """The phase-0 gotcha: swaps come from wallets that differ from the profile address."""
    swaps = load("fomo_swaps_sample.json")["responseObject"]["swaps"]
    addrs = parse_execution_addresses(swaps)
    assert addrs["solana"] == "923mtHovLkMhsniQVsfpKKwXYM5GhaEq3X9RyWxRho4C"
    assert addrs["robinhood"] == "0x3e98397dca6adda872b161297a0fa34288f623b9"
    holder = parse_holders(load("fomo_holders_sample.json"), "0x385f4f8ae47651ce5f58f5265395a669f8281e18")[0]
    assert holder.profile_address != addrs["solana"]
    assert parse_execution_addresses([]) == {}


def test_fomo_browser_export_import(tmp_path):
    """The browser export is the only way fomo data gets in (Cloudflare blocks server-side calls)."""
    from fomo_agent import db as _db
    from fomo_agent.pipeline.discover import import_browser_export

    export = {
        "exportedAt": 1788614000,
        "leaderboards": {"30d": load("fomo_leaderboard_sample.json")},
        "holders": {"0x385f4f8ae47651ce5f58f5265395a669f8281e18": load("fomo_holders_sample.json")},
        "swaps": {"aefe2ddd-c580-5245-a2f5-e4ed62f7ef10": load("fomo_swaps_sample.json")},
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf-8")

    conn = _db.connect(tmp_path / "imp.db")
    stats = import_browser_export(conn, path)
    assert stats["periods"] == {"30d": 3}
    assert stats["new_users"] == 6          # 3 leaderboard + 3 holders, all distinct users
    assert stats["swaps"] > 0

    # No traders are invented from fomo's own addresses: they are internal accounts with no
    # on-chain swaps. The swaps are kept as evidence for pipeline/resolve.py instead.
    assert conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0] == 0

    swaps = conn.execute("SELECT * FROM fomo_swaps ORDER BY ts").fetchall()
    assert {s["chain"] for s in swaps} <= {"solana", "robinhood"}
    assert all(s["user_id"] == "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10" for s in swaps)
    assert all(s["token"] == s["token"].lower() or not s["token"].startswith("0x") for s in swaps)

    resolved = conn.execute(
        "SELECT resolved_at FROM fomo_users WHERE user_id='aefe2ddd-c580-5245-a2f5-e4ed62f7ef10'"
    ).fetchone()[0]
    assert resolved is not None

    # importing the same file twice must not duplicate anything
    again = import_browser_export(conn, path)
    assert again["new_users"] == 0 and again["swaps"] == 0


# ---------- rhtrenches (third-party indexer for Robinhood Chain) ----------

def test_trenches_parse_trader():
    rows = load("trenches_traders_sample.json")
    t = parse_trader(rows[0])
    assert t["chain"] == "robinhood"
    assert t["address"] == t["address"].lower()
    assert t["fomo_handle"] and t["trades_cnt"] is not None
    # the stats blob is what reaches the scoring context
    assert "realized_pnl" in t["stats"] and "open_bags" in t["stats"]
    assert parse_trader({}) is None

    # win_rate arrives as a fraction here, but a percentage would be normalized
    assert parse_trader({"address": "0xA", "win_rate": 55})["win_rate"] == pytest.approx(0.55)
    assert parse_trader({"address": "0xA", "win_rate": 0.55})["win_rate"] == pytest.approx(0.55)
    assert parse_trader({"address": "0xA"})["win_rate"] is None


def test_trenches_parse_fill():
    fills = load("trenches_tape_sample.json")
    trades = [t for t in map(parse_fill, fills) if t]
    assert len(trades) == len(fills)
    f, t = fills[0], trades[0]
    assert t.chain == "robinhood" and t.source == "trenches"
    assert t.side in ("buy", "sell") and t.side == f["side"]
    assert t.usd_value == pytest.approx(f["usd"])
    assert t.sol_amount is None                      # no SOL leg off Solana
    assert t.address == f["wallet"].lower() and t.mint == f["token"].lower()
    # the signature must stay unique when one tx produces several fills
    assert t.sig == f"{f['tx']}:{f['id']}"
    assert len({x.sig for x in trades}) == len(trades)


def test_trenches_fill_rejects_incomplete():
    ok = load("trenches_tape_sample.json")[0]
    assert parse_fill({**ok, "side": "transfer"}) is None
    assert parse_fill({**ok, "wallet": None}) is None
    assert parse_fill({**ok, "ts": None}) is None


def test_fomo_positions_parse_any_shape():
    """The /trades list was only ever seen as a 304, so every plausible wrapping is accepted."""
    trade = {
        "id": "t1", "tokenAddress": "0xAbCdEf0000000000000000000000000000000001",
        "networkId": 4663, "createdAt": "2026-09-04T07:40:45.450Z", "closedAt": None,
        "humanTokenAmount": 11020641.8, "avgEntryPrice": 0.000135317, "avgExitPrice": None,
        "realizedPnlUsd": 0, "unrealizedPnlUsd": 890695.48, "totalCostBasis": 1491.28,
        "tokenMetadata": {"symbol": "MEME", "currentPrice": 0.0809, "liquidity": 2307857.6},
    }
    for payload in ({"responseObject": [trade]},
                    {"responseObject": {"trades": [trade]}},
                    {"responseObject": {"trade": trade}},
                    {"responseObject": [{"trade": trade}]},
                    [trade]):
        rows = parse_trade_rows("u1", payload)
        assert len(rows) == 1, payload
        r = rows[0]
        assert r["trade_id"] == "t1" and r["user_id"] == "u1"
        assert r["chain"] == "robinhood" and r["symbol"] == "MEME"
        assert r["token"] == "0xabcdef0000000000000000000000000000000001"   # lowercased
        assert r["unrealized_pnl"] == pytest.approx(890695.48)
        assert r["cost_basis"] == pytest.approx(1491.28)
        assert r["opened_at"] == 1788507645 and r["closed_at"] is None

    # anything without an id or a token is skipped rather than raising
    assert parse_trade_rows("u1", {"responseObject": [{"id": "x"}, {}, None]}) == []
    assert parse_trade_rows("u1", {}) == []
    assert parse_trade_rows("u1", None) == []


def test_top_holdings_come_from_the_leaderboard():
    """Per-token positions ride along in the leaderboard response, so they cost no extra request."""
    payload = {"responseObject": {"leaderboard": [{
        "id": "u1", "userHandle": "someone",
        "topHoldings": [
            {"tokenAddress": "0xAbC", "networkId": 4663, "humanAmount": 100.0,
             "price": 2.0, "value": 200.0, "pnl": 150.0},
            {"tokenAddress": None},                       # skipped
        ],
    }, {"id": None, "topHoldings": [{"tokenAddress": "0xdef"}]}]}}   # no user id -> skipped

    rows = parse_top_holdings(payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "u1" and r["chain"] == "robinhood"
    assert r["token"] == "0xabc" and r["trade_id"] == "h:u1:0xabc"
    assert r["unrealized_pnl"] == pytest.approx(150.0)
    # what was paid is value minus profit, and entry price follows from the amount held
    assert r["cost_basis"] == pytest.approx(50.0)
    assert r["avg_entry"] == pytest.approx(0.5)
    assert r["current_price"] == pytest.approx(2.0)

    assert parse_top_holdings({}) == []
    assert parse_top_holdings({"responseObject": {"leaderboard": [{"id": "u", "topHoldings": []}]}}) == []


# ---------- score schema ----------

def test_parse_score_strict():
    ok = parse_score('{"score": 72, "status": "active", "style": ["sniper"], "red_flags": [], "summary": "x", "confidence": 0.6}')
    assert isinstance(ok, ScoreResult) and ok.score == 72

    fenced = parse_score('```json\n{"score": 10, "status": "dropped", "summary": "bot", "confidence": 0.9}\n```')
    assert fenced.status == "dropped"

    with pytest.raises(Exception):
        parse_score('{"score": 150, "status": "active", "summary": "x", "confidence": 0.5}')
    with pytest.raises(Exception):
        parse_score('{"score": 50, "status": "maybe", "summary": "x", "confidence": 0.5}')


# ---------- db ----------

def test_db_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.upsert_trader(conn, "A", source="manual", status="candidate") is True
    assert db.upsert_trader(conn, "A", pnl_7d=1.5) is False
    row = db.get_trader(conn, "A")
    assert row["pnl_7d"] == 1.5 and row["status"] == "candidate"
    assert db.insert_trade(conn, sig="S1", address="A", mint="M", side="buy", ts=10) is True
    assert db.insert_trade(conn, sig="S1", address="A", mint="M", side="buy", ts=10) is False
    assert db.last_trade_ts(conn, "A") == 10
    conn.commit()


def test_geckoterminal_candles_come_back_oldest_first_and_clean():
    """The API answers newest first; a chart that plots that reads backwards."""
    from fomo_agent.sources.geckoterminal import parse_ohlcv

    rows = parse_ohlcv({"attributes": {"ohlcv_list": [
        [1788868800, 0.0026, 0.0031, 0.0024, 0.0028, 37995.4],
        [1788865200, 0.0023, 0.0027, 0.0021, 0.0026, 64056.3],
        [1788861600, None, 0.0022, 0.0020, 0.0021, 100.0],   # a hole in the row
        [1788858000, 0.002, 0.0, 0.0, 0.002, 5.0],           # a candle with no range
        "nonsense",
    ]}})
    assert [r[0] for r in rows] == [1788865200, 1788868800], "oldest first, junk dropped"
    assert rows[-1][4] == pytest.approx(0.0028) and rows[-1][5] == pytest.approx(37995.4)
    assert parse_ohlcv(None) == [] and parse_ohlcv({}) == []


def test_geckoterminal_token_carries_the_pool_the_chart_is_drawn_from():
    from fomo_agent.sources.geckoterminal import parse_token

    t = parse_token({
        "attributes": {"address": "0xabc", "symbol": "CME", "price_usd": "0.0029"},
        "relationships": {"top_pools": {"data": [{"id": "robinhood_0xff0aa5f0"}, {"id": "x_0xdeep"}]}},
    }, "robinhood")
    assert t.pool_address == "0xff0aa5f0", "deepest pool first, network prefix stripped"
    assert parse_token({"attributes": {"address": "0xabc"}}, "robinhood").pool_address is None


def test_fomo_holders_carry_the_thesis():
    """`comment` is the trader's own note on a position, and the parser used to drop it.

    The fixture is derived from the real capture: shape untouched, a comment added to two of the
    three holders because none of the captured ones had written one. The field name and the fact
    that it is optional come from docs/fomo-endpoints.md.
    """
    mint = "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
    rows = parse_holders(load("fomo_holders_thesis_sample.json"), mint)
    assert len(rows) == 3
    assert rows[0].thesis.startswith("Bought the first dip")
    assert rows[0].trade_id == "8f846848-7ee9-4434-b6bd-872e7ebd8cab"
    # whitespace is not an opinion
    assert rows[1].thesis is None or not rows[1].thesis.strip()
    assert rows[2].is_dev is True and "biased" in rows[2].thesis
    # the holders without a note still parse exactly as before
    assert all(r.mint == mint and r.fomo_user_id for r in rows)


def test_a_thesis_that_is_not_a_string_does_not_sink_the_collection():
    """`comment` is documented as optional and turned out to be an object, not a string.

    Pydantic rejected the row, the exception escaped the holders loop, and the receiver answered
    500 — so a whole collection was lost to one field. A shape we cannot read is a missing thesis.
    """
    from fomo_agent.sources.fomo import thesis_text

    assert thesis_text(None) is None
    assert thesis_text("  written early  ") == "written early"
    assert thesis_text("   ") is None
    # the real shape: the note plus its metadata
    assert thesis_text({"id": "9b08", "text": "still early", "likes": 24, "newerThesis": 0}) \
        == "still early"
    assert thesis_text({"id": "9b08", "comment": "under another name"}) == "under another name"
    # unrecognised, and unrecognised is not fatal
    assert thesis_text({"id": "9b08", "likes": 24}) is None
    assert thesis_text(12345) is None


def test_one_unreadable_token_costs_only_that_token(tmp_path):
    from fomo_agent import db
    from fomo_agent.pipeline.discover import import_browser_export

    conn = db.connect(tmp_path / "ingest.db")
    payload = {
        "leaderboards": {"24h": load("fomo_leaderboard_sample.json")},
        "holders": {
            "0x385f4f8ae47651ce5f58f5265395a669f8281e18": load("fomo_holders_thesis_sample.json"),
            "0xbroken": {"responseObject": "this is not a list of tokens"},
        },
    }
    stats = import_browser_export(conn, payload)
    assert stats["periods"]["24h"] > 0, "the leaderboard still landed"
    assert stats["theses"] >= 1, "the good token still yielded its theses"


def test_the_collection_records_what_it_asked_for(tmp_path):
    """A pass that asked for thirteen tokens and delivered one must not look like a pass that
    asked for one. That gap hid a broken batch through twenty collections."""
    from fomo_agent import db
    from fomo_agent.pipeline.discover import import_browser_export

    conn = db.connect(tmp_path / "asked.db")
    good = "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
    stats = import_browser_export(conn, {
        "asked": [good, "0xsilent", "0xrefused"],
        "holders": {good: load("fomo_holders_thesis_sample.json")},
        "holderErrors": {"0xsilent": "answered with nothing", "0xrefused": "400"},
    })
    assert stats["asked"] == 3, "three were asked for"
    assert len(stats["holders"]) == 1, "one answered"
    assert set(stats["holder_errors"]) == {"0xsilent", "0xrefused"}

