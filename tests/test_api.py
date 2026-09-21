"""The HTTP API, against a temporary database and with no network.

These guard the contract the site and any third-party client will depend on: the shape of a
response, the bounds on a query string, and the promise that a quote asset never reads as a signal.
"""
import json

import pytest
from fastapi.testclient import TestClient

from fomo_agent import api, db
from fomo_agent.config import settings
from fomo_agent.sources.rpc import USDG

ACE = "0x" + "a" * 40      # scores 88
MID = "0x" + "b" * 40      # scores 74
DUD = "0x" + "c" * 40      # scores 30
TOKEN = "0x" + "d" * 40
UNKNOWN = "0x" + "9" * 40


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "api.db")
    c = db.connect()
    now = db.now()
    with db.tx(c):
        for addr, handle, uid, score, status in ((ACE, "ace", "u1", 88, "active"),
                                                 (MID, "mid", "u2", 74, "active"),
                                                 (DUD, "dud", "u3", 30, "dropped")):
            db.upsert_trader(c, addr, chain="robinhood", fomo_handle=handle, fomo_user_id=uid,
                             score=score, status=status, pnl_30d=2_000_000.0,
                             ai_summary=f"{handle} verdict", ai_model="claude-opus-5",
                             tags=json.dumps({"style": ["swing"], "red_flags": ["one-hit"]}),
                             stats_json=json.dumps({"win_rate": 0.6}))
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS", liquidity_usd=500_000)
        db.upsert_token(c, USDG, chain="robinhood", symbol="USDG", liquidity_usd=9_000_000)
        c.execute("INSERT INTO fomo_positions(trade_id, user_id, token, chain, unrealized_pnl, "
                  "cost_basis, seen_at) VALUES(?,?,?,?,?,?,?)",
                  ("p1", "u1", TOKEN, "robinhood", 900_000.0, 30_000.0, now))
        for i, (addr, mint) in enumerate(((ACE, TOKEN), (MID, TOKEN), (DUD, TOKEN),
                                          (ACE, USDG), (MID, USDG))):
            db.insert_trade(c, sig=f"0xsig{i}", address=addr, chain="robinhood", mint=mint,
                            side="buy", usd_value=5_000.0, ts=now - 900, source="rpc")
    c.close()
    api.limiter.hits.clear()
    api._responses.clear()   # the response cache is module-wide; a test must not read another's
    return TestClient(api.app)


def test_health_reports_freshness(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["chain"] == "robinhood"
    assert body["stale_seconds"] < 3600


def test_stats_excludes_quote_assets_from_the_fill_count(client):
    body = client.get("/api/stats").json()
    assert body["traders"] == 3 and body["scored"] == 3
    assert body["active"] == 2 and body["dropped"] == 1
    assert body["fills"] == 3, "the two USDG rows are plumbing, not fills"


def test_signals_rank_by_conviction_and_name_the_buyers(client):
    body = client.get("/api/signals?hours=24").json()
    assert [s["sym"] for s in body["signals"]] == ["PONS"], "USDG never reaches the feed"
    sig = body["signals"][0]
    assert sig["buyers"] == 2, "the wallet scoring 30 does not count"
    assert sig["who"] == ["ace", "mid"] and sig["scores"] == [88, 74]
    assert sig["conviction"] == pytest.approx(0.88 ** 2 + 0.74 ** 2)


def test_signal_window_and_limits_are_bounded(client):
    assert client.get("/api/signals?hours=0").status_code == 422
    assert client.get("/api/signals?hours=100000").status_code == 422
    assert client.get("/api/signals?limit=9999").status_code == 422
    assert client.get("/api/signals?min_buyers=1").status_code == 422


def test_leaderboard_unpacks_tags(client):
    body = client.get("/api/leaderboard?status=active").json()
    assert [t["handle"] for t in body["traders"]] == ["ace", "mid"]
    assert body["traders"][0]["style"] == ["swing"]
    assert body["traders"][0]["red_flags"] == ["one-hit"]


def test_leaderboard_status_is_validated(client):
    assert client.get("/api/leaderboard?status=nonsense").status_code == 422
    assert client.get("/api/leaderboard?status=all").json()["count"] == 3


def test_trader_by_handle_and_by_address(client):
    for who in ("ace", ACE, ACE.upper()):
        body = client.get(f"/api/trader/{who}").json()
        assert body["score"] == 88 and body["summary"] == "ace verdict"
    assert client.get("/api/trader/nobody").status_code == 404


def test_trader_carries_the_open_book(client):
    body = client.get("/api/trader/ace").json()
    assert [p["sym"] for p in body["positions"]] == ["PONS"]
    assert body["open_pnl"] == 900_000
    # the page needs every part of the book, not only what is still held
    for key in ("closed", "realized_usd", "round_trips", "wins", "win_rate", "pre_tape", "tape_from"):
        assert key in body, key


def test_token_answers_with_the_holders(client):
    body = client.get(f"/api/token/{TOKEN}").json()
    assert body["symbol"] == "PONS" and body["tracked"] is True
    assert body["trusted_holders"] == 1
    assert [h["handle"] for h in body["holders"]] == ["ace"]


def test_a_quote_asset_is_flagged_rather_than_ranked(client):
    body = client.get(f"/api/token/{USDG}").json()
    assert body["is_quote"] is True


def test_an_untracked_token_still_answers(client, monkeypatch):
    """The difference between a list and a tool: an unknown address gets a real reply."""
    monkeypatch.setattr(api, "live_lookup", lambda conn, mint: False)
    body = client.get(f"/api/token/{UNKNOWN}").json()
    assert body["tracked"] is False
    assert body["holders"] == [] and body["conviction"] == 0


def test_nonsense_is_rejected_before_it_reaches_the_database(client):
    assert client.get("/api/token/hello").status_code == 400


def test_search_routes_a_handle_an_address_and_a_fragment(client):
    assert client.get("/api/search?q=ace").json()["kind"] == "trader"
    assert client.get(f"/api/search?q={UNKNOWN}").json()["kind"] == "token"
    body = client.get("/api/search?q=ac").json()
    assert body["kind"] == "suggestions" and body["traders"][0]["handle"] == "ace"
    assert client.get("/api/search?q=zzzzz").json()["kind"] == "none"


def test_tape_only_carries_trusted_wallets(client):
    body = client.get("/api/tape").json()
    assert {f["handle"] for f in body["fills"]} == {"ace", "mid"}
    assert all(f["sym"] != "USDG" for f in body["fills"])


def test_a_second_reader_inside_ten_seconds_is_served_from_memory(client):
    """The feed queries walk the whole tape; forty readers in the same ten seconds are one
    question, answered once."""
    api._responses.clear()
    first = client.get("/api/signals?hours=24")
    second = client.get("/api/signals?hours=24")
    assert first.headers["x-cache"] == "miss" and second.headers["x-cache"] == "hit"
    assert first.json() == second.json()
    other = client.get("/api/signals?hours=6")
    assert other.headers["x-cache"] == "miss", "a different window is a different question"
    assert "x-cache" not in client.get("/api/health").headers, "health is never remembered"


def test_the_site_talking_to_itself_is_not_one_visitor(client, monkeypatch):
    """The server-side renderer calls from loopback with no forwarded address. Every reader of
    the site went through that one key, so 120 calls a minute was the whole site's budget."""
    monkeypatch.setattr(api.limiter, "per_minute", 2)
    api.limiter.hits.clear()
    # loopback, no X-Forwarded-For: the renderer. Never limited.
    loop = TestClient(api.app, client=("127.0.0.1", 40000))
    for _ in range(6):
        assert loop.get("/api/stats").status_code == 200
    # the same calls with a forwarded address are a visitor, and are
    codes = [client.get("/api/stats", headers={"x-forwarded-for": "203.0.113.9"}).status_code
             for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_rate_limit_returns_429_rather_than_dying(client, monkeypatch):
    api._responses.clear()
    monkeypatch.setattr(api.limiter, "per_minute", 3)
    api.limiter.hits.clear()
    codes = [client.get("/api/health").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]
    assert client.get("/api/health").json()["error"] == "rate limited"


def test_openapi_describes_the_product(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "FOMO Robinhood Radar"
    assert "/api/signals" in spec["paths"] and "/api/token/{mint}" in spec["paths"]


def test_chart_answers_with_candles_and_says_why_when_it_cannot(client, monkeypatch):
    """A token page without a chart is a smaller answer, not a broken one."""
    body = client.get(f"/api/token/{TOKEN}/chart").json()
    assert body["candles"] == [] and body["pool"] is None
    assert "no pool" in body["why"], "the reason is stated rather than 404'd"

    conn = db.connect()
    with db.tx(conn):
        db.upsert_token(conn, TOKEN, pool_address="0xpool")
    conn.close()
    monkeypatch.setattr(api, "candles_for", lambda pool, chain, span, **kw: [[1, 2, 3, 1, 2, 9]])

    body = client.get(f"/api/token/{TOKEN}/chart?span=24h").json()
    assert body["pool"] == "0xpool" and body["span"] == "24h"
    assert body["candles"] == [[1, 2, 3, 1, 2, 9]]
    assert client.get(f"/api/token/{TOKEN}/chart?span=1y").status_code == 422, "spans are fixed"


def test_candles_are_served_from_memory_between_requests(monkeypatch):
    """Every visitor to a token page would otherwise cost one upstream request."""
    calls = []

    class FakeGecko:
        def ohlcv(self, chain, pool, timeframe, aggregate, limit):
            calls.append((chain, pool, timeframe, limit))
            return [[1, 2, 3, 1, 2, 9]]

    monkeypatch.setitem(api._clients, "gecko", FakeGecko())
    api._candles.clear()

    assert api.candles_for("0xpool", "robinhood", "7d") == [[1, 2, 3, 1, 2, 9]]
    assert api.candles_for("0xpool", "robinhood", "7d") == [[1, 2, 3, 1, 2, 9]]
    assert len(calls) == 1, "the second visitor is served from the cache"
    assert calls[0][2] == "hour" and calls[0][3] == 168, "7d is a week of hourly candles"

    api.candles_for("0xpool", "robinhood", "30d")
    assert calls[1][2] == "day", "a month is daily candles, not 720 hourly ones"


def test_the_api_never_waits_for_the_candle_source(monkeypatch):
    """Forty request threads asleep on GeckoTerminal's thirty-a-minute was the whole API hanging.
    The impatient client answers at once when the allowance is spent, and the chart says why."""
    from fomo_agent.sources.geckoterminal import GeckoTerminal, Busy
    import httpx

    gt = GeckoTerminal(client=httpx.Client(base_url="http://127.0.0.1:9"), patient=False)
    gt.limiter.max = 0   # no room at all
    import time as _t
    t0 = _t.monotonic()
    try:
        gt.ohlcv("robinhood", "0xpool", "hour", 1, 24)
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = e
    assert _t.monotonic() - t0 < 0.5, "it did not sleep"
    # ohlcv itself catches HTTPError and returns [], so Busy surfaces as an empty answer
    assert raised is None


def test_the_upstream_client_is_one_per_process(monkeypatch):
    """A client per request brought a rate limiter per request, which limits nothing, and a
    connection pool per request that nothing closed: 581 open sockets and 2.3 GB after a day."""
    made = []

    class FakeGecko:
        def __init__(self, **kw):
            made.append(self)

        def ohlcv(self, *a):
            return [[1, 2, 3, 1, 2, 9]]

    import fomo_agent.sources.geckoterminal as gt
    monkeypatch.setattr(gt, "GeckoTerminal", FakeGecko)
    monkeypatch.delitem(api._clients, "gecko", raising=False)
    api._candles.clear()
    for pool in ("0xa", "0xb", "0xc"):
        api.candles_for(pool, "robinhood", "24h")
    assert len(made) == 1 and api.gecko() is made[0]


def test_expired_candles_leave_the_cache(monkeypatch):
    """An entry past its TTL was never served but was never dropped either, so the cache only
    ever grew — one entry per pool per span for every token a crawler had ever opened."""
    class FakeGecko:
        def ohlcv(self, *a):
            return [[1, 2, 3, 1, 2, 9]]

    monkeypatch.setitem(api._clients, "gecko", FakeGecko())
    api._candles.clear()
    api.candles_for("0xold", "robinhood", "7d")
    # age it past the TTL by hand
    at, rows, ttl = api._candles[("0xold", "7d")]
    api._candles[("0xold", "7d")] = (at - ttl - 1, rows, ttl)
    api.candles_for("0xnew", "robinhood", "7d")
    assert ("0xold", "7d") not in api._candles and ("0xnew", "7d") in api._candles


def test_a_settled_bursts_candles_are_kept_for_hours_and_a_fresh_ones_for_minutes(client, monkeypatch):
    """Three workers refreshing forty day-old pools every five minutes were most of the box's
    GeckoTerminal allowance, for a scorecard whose numbers had settled the day before."""
    from fomo_agent import db
    calls = []

    class FakeGecko:
        def ohlcv(self, *a):
            calls.append(a)
            return [[1, 2, 3, 1, 2, 9]]

    monkeypatch.setitem(api._clients, "gecko", FakeGecko())
    api._candles.clear()
    conn = db.connect()
    now = db.now()
    with db.tx(conn):
        db.upsert_token(conn, "0x" + "5" * 40, chain="robinhood", symbol="OLD", pool_address="0xpoolold")
        db.upsert_token(conn, "0x" + "6" * 40, chain="robinhood", symbol="NEW", pool_address="0xpoolnew")
        for mint, ts in (("0x" + "5" * 40, now - 2 * 86400), ("0x" + "6" * 40, now - 600)):
            conn.execute("INSERT INTO bursts(mint, chain, ts, px, conviction, wallets, usd, window_s, age_s, who) "
                         "VALUES(?, 'robinhood', ?, 1.0, 2.0, 3, 900.0, 600, 60, '[]')", (mint, ts))
    candles = api._pool_candles(conn, 168)
    candles("0x" + "5" * 40); candles("0x" + "6" * 40)
    assert api._candles[("0xpoolold", "7d")][2] == api.SETTLED_TTL
    assert api._candles[("0xpoolnew", "7d")][2] == api.CANDLE_TTLS["7d"]
    conn.close()


def test_the_candles_one_worker_fetched_serve_the_next_from_the_db(client, monkeypatch):
    """Three workers, three copies, three requests for one chart: the DB holds the one copy."""
    from fomo_agent import db
    calls = []

    class FakeGecko:
        def ohlcv(self, *a):
            calls.append(a)
            return [[1, 2, 3, 1, 2, 9]]

    monkeypatch.setitem(api._clients, "gecko", FakeGecko())
    api._candles.clear()
    conn = db.connect()
    assert api.candles_for("0xshared", "robinhood", "24h", conn=conn) == [[1, 2, 3, 1, 2, 9]]
    api._candles.clear()   # another worker: nothing in memory
    assert api.candles_for("0xshared", "robinhood", "24h", conn=conn) == [[1, 2, 3, 1, 2, 9]]
    assert len(calls) == 1, "the second worker read the first one's answer"
    # past the span's TTL the DB copy is stale and the source is asked again
    with db.tx(conn):
        conn.execute("UPDATE candle_cache SET fetched_at = fetched_at - ?", (api.CANDLE_TTLS["24h"] + 1,))
    api._candles.clear()
    api.candles_for("0xshared", "robinhood", "24h", conn=conn)
    assert len(calls) == 2
    conn.close()


def test_fresh_endpoint_carries_the_filters_it_applied(client):
    """A reader has to know what was left out before trusting what was let in."""
    body = client.get("/api/fresh?hours=24&min_liquidity=1000&min_buyers=1").json()
    assert body["hours"] == 24 and body["min_liquidity"] == 1000 and body["min_buyers"] == 1
    assert "drained" in body and isinstance(body["tokens"], list)
    assert client.get("/api/fresh?hours=999").status_code == 422


def test_hot_endpoint_carries_the_feed_and_its_scorecard(client):
    body = client.get("/api/hot").json()
    assert body["delta"] > 0 and body["window_min"] > 0
    assert body["now"] == [] and body["recent"] == [], "the fixture's buys are spread, not bursting"


def test_a_signal_says_whether_it_arrived_in_a_burst(client):
    from fomo_agent import db as _db
    c = _db.connect()
    with _db.tx(c):
        c.execute("INSERT INTO bursts(mint, chain, ts, conviction, wallets, usd, px, window_s, age_s, who) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (TOKEN, "robinhood", _db.now() - 600, 4.2, 4, 9000.0,
                                                   1.0, 1800, 3000, "[]"))
    c.close()
    rows = client.get("/api/signals").json()["signals"]
    pons = next(r for r in rows if r["mint"] == TOKEN)
    assert pons["burst"] and pons["burst"]["conviction"] == 4.2


def test_an_unknown_address_is_looked_up_once_an_hour(monkeypatch, tmp_path):
    """Crawlers ask about the same unknown addresses over and over; a miss is remembered."""
    from fomo_agent import api as api_mod, db
    calls = []
    monkeypatch.setattr("fomo_agent.pipeline.new_tokens.lookup_tokens", lambda *a, **k: (calls.append(1) or ([], 0)))
    api_mod._missed.clear()
    conn = db.connect(tmp_path / "m.db")
    mint = "0x" + "77" * 20
    assert api_mod.live_lookup(conn, mint) is False and len(calls) == 1
    assert api_mod.live_lookup(conn, mint) is False and len(calls) == 1, "remembered as a miss"
    api_mod._missed[mint] -= api_mod.MISS_TTL + 1
    assert api_mod.live_lookup(conn, mint) is False and len(calls) == 2, "asked again once the hour is up"


def test_a_pro_key_reads_faster_and_a_lapsed_one_does_not(client, monkeypatch):
    """No key: the address's 120. A PRO chat's key: its own, larger allowance. A key whose chat
    is no longer PRO: refused with a reason. No User-Agent at all: refused before anything."""
    from fomo_agent import bot
    from fomo_agent.pipeline import keys, pro
    monkeypatch.setattr(settings, "pro_price_usd", 20.0)
    monkeypatch.setattr(settings, "pro_token", "0x" + "f" * 40)
    monkeypatch.setattr(api.limiter, "per_minute", 2)
    monkeypatch.setattr(api.keyed, "per_minute", 4)
    api.limiter.hits.clear(); api.keyed.hits.clear(); api.keyring._seen.clear(); api._responses.clear()
    conn = db.connect()
    bot.subscribe(conn, "42", None)
    pro.grant(conn, "42", 30)
    key = keys.issue(conn, "42")
    h = {"x-forwarded-for": "203.0.113.7"}
    assert [client.get("/api/health", headers=h).status_code for _ in range(3)] == [200, 200, 429]
    with_key = {**h, "x-api-key": key}
    assert [client.get("/api/health", headers=with_key).status_code for _ in range(5)] == [200, 200, 200, 200, 429]
    assert client.get("/api/health", headers={**h, "x-api-key": "nope"}).status_code == 401
    # the chat's PRO lapses: the key is answered from memory for a minute, then refused
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET paid_until=1 WHERE chat_id='42'")
    api.keyring._seen.clear()
    assert client.get("/api/health", headers=with_key).status_code == 401
    # no user agent at all
    assert client.get("/api/health", headers={**h, "user-agent": ""}).status_code == 400
    # the bot hands keys to PRO chats only, and a second ask replaces the first
    pro.grant(conn, "42", 30)
    text = bot.handle_text(conn, "/apikey", "42", None)
    assert "revoked" in text and keys.current(conn, "42")["key"] in text and keys.current(conn, "42")["key"] != key
    bot.subscribe(conn, "free", None)
    assert "PRO" in bot.handle_text(conn, "/apikey", "free", None)
