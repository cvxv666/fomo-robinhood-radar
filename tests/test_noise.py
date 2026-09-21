"""Addresses that are not traders: found by fill rate, dropped, forgotten, never resolved to."""
import json

from fomo_agent import db
from fomo_agent.config import settings
from fomo_agent.pipeline import hot, noise, resolve

BOT = "0x" + "b0" * 20
HUMAN = "0x" + "aa" * 20
MINT = "0x" + "11" * 20


def fills(conn, address, n, mint=MINT, now=None, usd=5.0):
    now = now or db.now()
    with db.tx(conn):
        for i in range(n):
            db.insert_trade(conn, sig=f"{address[:6]}{i}", address=address, chain="robinhood",
                            mint=mint if i % 2 else "0x" + f"{i:040x}", side="buy", usd_value=usd,
                            token_amount=1000.0, ts=now - 3500 + i % 3000, source="rpc", kind="trade")


def test_a_hyperactive_address_is_quarantined_and_its_fills_forgotten(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "noise_fills_per_hour", 100)
    conn = db.connect(tmp_path / "n.db")
    with db.tx(conn):
        db.upsert_trader(conn, BOT, chain="robinhood", fomo_handle="stav", status="tracking", tags=json.dumps({"red_flags": []}))
        db.upsert_trader(conn, HUMAN, chain="robinhood", fomo_handle="ace", status="active", score=80)
        conn.execute("INSERT INTO fomo_users(user_id, handle, onchain_address) VALUES('u1', 'stav', ?)", (BOT,))
    fills(conn, BOT, 400)
    fills(conn, HUMAN, 30)
    found = noise.sweep(conn)
    assert [f["address"] for f in found] == [BOT] and found[0]["removed"] == 400
    row = db.get_trader(conn, BOT)
    assert row["status"] == "dropped" and "bot" in json.loads(row["tags"])["red_flags"]
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE address=?", (BOT,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE address=?", (HUMAN,)).fetchone()[0] == 30, "the person keeps theirs"
    assert conn.execute("SELECT onchain_address, onchain_note FROM fomo_users WHERE user_id='u1'").fetchone()[0] is None
    assert db.noise(conn) == {BOT}
    assert noise.sweep(conn) == [], "gone, so not found again"


def test_resolution_never_picks_a_noise_address(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    with db.tx(conn):
        conn.execute("INSERT INTO noise_addresses(address, reason, ts) VALUES(?, 'bot', 1)", (BOT,))
        conn.execute("INSERT INTO fomo_users(user_id, handle) VALUES('u1', 'ace')")
        for i in range(4):
            conn.execute("INSERT INTO fomo_swaps(user_id, chain, token, ts, side, swap_id) VALUES('u1','robinhood',?,?,?,?)",
                         (MINT, 1000 + i * 100, "buy", f"s{i}"))
    # the bot is in every window; the person is in every window too, with a bystander each time
    fetch = lambda token, ts, side: {BOT, HUMAN, "0x" + f"{ts:040x}"}  # noqa: E731
    address, info = resolve.resolve_user(conn, fetch, "u1", "robinhood")
    assert address == HUMAN, info


def test_candles_in_another_unit_are_brought_into_the_fill_price(tmp_path):
    """The 32-byte-id pools' candles come back a thousand times the fill; they are evidence
    once rescaled, and a 700x that was really 0.7x reads as 0.7x."""
    conn = db.connect(tmp_path / "c.db")
    ts, px = 1_000_000, 0.001
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", price_usd=0.0007)
    thousand = [[ts + 600 * i, 1.0 - 0.05 * i, 1.1 - 0.05 * i, 0.9 - 0.05 * i, 0.95 - 0.05 * i, 0] for i in range(6)]
    o = hot.outcome(conn, MINT, ts, px, candles=thousand, now=ts + 7200)
    assert o["best"] == 1.1 and o["now"] == 0.7
    off = [[ts + 600, 0.037, 0.04, 0.03, 0.035, 0]]   # 37x the fill: no unit makes sense of it
    o2 = hot.outcome(conn, MINT, ts, px, candles=off, now=ts + 7200)
    assert o2["best"] is None and o2["now"] == 0.7, "not evidence; the tape (empty) and the quote answer"


def test_a_pool_thinner_than_the_burst_is_not_evidence(tmp_path):
    """SYNTH: the cohort put $1,532 in; the pool GeckoTerminal offered had $253 of volume and one
    trade at 384x. Candles carrying less than half the burst's own size are set aside."""
    conn = db.connect(tmp_path / "v.db")
    ts, px = 1_000_000, 1.34e-6
    with db.tx(conn):
        conn.execute("INSERT INTO bursts(mint, chain, ts, conviction, wallets, usd, px, window_s) VALUES(?,?,?,?,?,?,?,?)",
                     (MINT, "robinhood", ts, 5.15, 7, 1532.0, px, 1800))
        db.upsert_token(conn, MINT, chain="robinhood", price_usd=1.44e-6)
    thin = [[ts + 600, 5.96e-6, 5.96e-6, 5.96e-6, 5.96e-6, 0], [ts + 20_000, 5.96e-6, 0.000516, 5.96e-6, 0.000516, 253]]
    o = hot.outcome(conn, MINT, ts, px, candles=thin, now=ts + 40_000)
    assert o["best"] is None and o["now"] == round(1.44e-6 / px, 2)
    real = [[ts + 600, 1.3e-6, 1.5e-6, 1.2e-6, 1.4e-6, 900], [ts + 1200, 1.4e-6, 1.6e-6, 1.3e-6, 1.45e-6, 1200]]
    assert hot.outcome(conn, MINT, ts, px, candles=real, now=ts + 40_000)["best"] == round(1.6e-6 / px, 2)


def test_the_deepest_sane_pool_is_the_one_read(tmp_path):
    from fomo_agent.sources.geckoterminal import parse_token, pool_fee
    assert pool_fee("SYNTH / USDG 89%") == 89.0 and pool_fee("SYNTH / WETH 0.25%") == 0.25 and pool_fee(None) is None
    mint = "0x4534dda9ee44a9869f57aab2a85a175a940911f8"
    item = {"attributes": {"address": mint, "symbol": "SYNTH", "decimals": 9, "price_usd": "0.0000032", "fdv_usd": "320497", "total_reserve_in_usd": "85"},
            "relationships": {"top_pools": {"data": [{"id": "robinhood_0xjunk"}, {"id": "robinhood_0xreal"}]}}}
    pools = {"robinhood_0xjunk": {"type": "pool", "attributes": {"name": "SYNTH / USDG 89%", "reserve_in_usd": "85", "base_token_price_usd": "0.0000032"},
                                  "relationships": {"base_token": {"data": {"id": "robinhood_" + mint}}}},
             "robinhood_0xreal": {"type": "pool", "attributes": {"name": "SYNTH / WETH 0.25%", "reserve_in_usd": "12", "base_token_price_usd": "0.00000144"},
                                  "relationships": {"base_token": {"data": {"id": "robinhood_" + mint}}}}}
    t = parse_token(item, "robinhood", pools)
    assert t.pool_address == "0xreal" and t.price_usd == 1.44e-6, "the trap pool is skipped even though it is deeper"
    assert parse_token(item, "robinhood", {}).pool_address == "0xjunk", "without attributes the first id stands"
    only_junk = {"robinhood_0xjunk": pools["robinhood_0xjunk"]}
    item1 = {**item, "relationships": {"top_pools": {"data": [{"id": "robinhood_0xjunk"}]}}}
    t1 = parse_token(item1, "robinhood", only_junk)
    assert t1.pool_address is None and t1.price_usd is None, "a trap is the only pool: no pool, no price"


def test_the_tape_peak_ignores_dust_and_direct_rows(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ts, px = 1_000_000, 0.001
    with db.tx(conn):
        db.insert_trade(conn, sig="a", address=HUMAN, chain="robinhood", mint=MINT, side="buy", usd_value=50.0, token_amount=40_000.0, ts=ts + 60, source="rpc", kind="trade")
        db.insert_trade(conn, sig="b", address=BOT, chain="robinhood", mint=MINT, side="buy", usd_value=0.5, token_amount=1.0, ts=ts + 120, source="rpc", kind="dust")
        db.insert_trade(conn, sig="c", address=BOT, chain="robinhood", mint=MINT, side="sell", usd_value=700.0, token_amount=1.0, ts=ts + 180, source="rpc", kind="direct")
    o = hot.outcome(conn, MINT, ts, px, now=ts + 7200)
    assert o["best"] == 1.25 and o["fills"] == 1


def test_a_token_with_only_trap_pools_is_unsellable(tmp_path):
    from fomo_agent.pipeline import safety
    conn = db.connect(tmp_path / "trap.db")
    with db.tx(conn):
        db.upsert_token(conn, MINT, chain="robinhood", symbol="SYNTH", liquidity_usd=0.0)
    v = safety.check(conn, MINT, rpc=None, gt=None)
    assert v["sellable"] == 0 and "trap" in v["note"]


def test_clones_by_name_are_named_in_the_push(tmp_path):
    from fomo_agent import bot
    from fomo_agent.pipeline import analyze
    conn = db.connect(tmp_path / "clone.db")
    now = db.now()
    a, b, c = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "c3" * 20
    with db.tx(conn):
        db.upsert_token(conn, a, chain="robinhood", symbol="musebook", created_at=now - 7200)
        db.upsert_token(conn, b, chain="robinhood", symbol="MUSEBOOK", created_at=now - 3600, sellable=0)
        db.upsert_token(conn, c, chain="robinhood", symbol="musebook", created_at=now - 600)
        db.upsert_token(conn, "0x" + "d4" * 20, chain="robinhood", symbol="musebook", created_at=now - 3 * 86400)
        conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES('1', ?, ?)", (a, now - 7000))
    clones = analyze.namesakes(conn, "musebook", c, now)
    assert [x["mint"] for x in clones] == [a, b], "the two from today, oldest first; three days ago does not count"
    t = {"sym": "musebook", "mint": c, "heat": 2.5, "buyers": 3, "avg_score": 80.0, "lead_minutes": 4.0, "age_h": 0.2,
         "usd": 1000.0, "liq": 20000.0, "who": ["x"], "scores": [80], "clones": clones}
    text = bot.fmt_launch(t, now)
    assert "3rd <b>$musebook</b> in 24h" in text and "honeypot" in text and "pushed" in text
    assert bot.clone_line({"sym": "X", "mint": c}) is None


def test_a_borrowed_name_is_named_and_needs_half_again_the_bar(tmp_path, monkeypatch):
    """The 21 Sep ZEC: the fourth ZEC on the chain in two weeks, wearing Zcash's ticker, a
    honeypot. Neither the day-old clone rule nor anything else said so in the message."""
    from fomo_agent import bot
    from fomo_agent.config import settings
    from fomo_agent.pipeline import analyze
    conn = db.connect(tmp_path / "name.db")
    now = db.now()
    z1, z2, z3, z4 = ("0x" + f"{i:02x}" * 20 for i in (1, 2, 3, 4))
    with db.tx(conn):
        db.upsert_token(conn, z1, chain="robinhood", symbol="ZEC", created_at=now - 11 * 86400, sellable=0)
        db.upsert_token(conn, z2, chain="robinhood", symbol="ZEC", created_at=now - 3 * 86400)
        db.upsert_token(conn, z3, chain="robinhood", symbol="zec", created_at=now - 40 * 86400)
        db.upsert_token(conn, z4, chain="robinhood", symbol="ZEC", created_at=now - 1200)
        db.upsert_token(conn, "0x" + "ee" * 20, chain="robinhood", symbol="PAWS", created_at=now - 1200)
    assert analyze.listed("ZEC") and analyze.listed("$sol") and not analyze.listed("PAWS")
    b = analyze.borrowed_name(conn, "ZEC", z4, now)
    assert b["listed"] and [x["mint"] for x in b["earlier"]] == [z1, z2], "forty days ago is out of the fortnight"
    assert "listed ticker" in b["why"] and "3rd $ZEC on this chain in 14 days" in b["why"]
    assert analyze.borrowed_name(conn, "PAWS", "0x" + "ee" * 20, now) is None
    t = {"sym": "ZEC", "mint": z4, "heat": 2.5, "buyers": 3, "avg_score": 80.0, "lead_minutes": 4.0, "age_h": 0.2,
         "usd": 1000.0, "liq": 20000.0, "who": ["ace", "mid"], "scores": [88, 74], "clones": [], "borrowed": b}
    text = bot.fmt_launch(t, now)
    assert "listed ticker" in text and "3rd $ZEC" in text and "11d ago honeypot" in text and "3d ago" in text
    assert "follow the best of them: /follow_ace" in text
    # the bar: half again the heat for a launch, half again the conviction for a burst
    monkeypatch.setattr(settings, "telegram_min_heat", 2.0)
    monkeypatch.setattr(settings, "namesake_bar_mult", 1.5)
    conn.execute("INSERT INTO bot_subscribers(chat_id, username, subscribed_at, active) VALUES('7', 'u', 0, 1)")
    conn.commit()
    monkeypatch.setattr(settings, "telegram_alert_window_h", 24)
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [dict(t, heat=2.5)], "hours": 6})
    monkeypatch.setattr(bot, "sell_check", lambda *a, **k: {"sellable": None, "note": ""}, raising=False)
    from fomo_agent.pipeline import safety, deployers
    monkeypatch.setattr(safety, "check", lambda *a, **k: {"sellable": None, "note": ""})
    monkeypatch.setattr(safety, "only_the_cohort", lambda *a, **k: None)
    monkeypatch.setattr(deployers, "objection", lambda *a, **k: None)
    assert bot.due(conn, now) == [], "heat 2.5 is under 3.0 for a borrowed name"
    monkeypatch.setattr(bot.analyze, "fresh", lambda *a, **k: {"tokens": [dict(t, heat=3.2)], "hours": 6})
    out = bot.due(conn, now)
    assert len(out) == 1 and "listed ticker" in out[0][2]


def test_the_follow_line_names_the_best_wallet_or_nothing():
    from fomo_agent import bot
    assert bot.follow_line({"who": ["ace", "mid"], "scores": [74, 88]}) == "follow the best of them: /follow_mid"
    assert bot.follow_line({"who": "ace,mid", "scores": "90,10"}) == "follow the best of them: /follow_ace"
    assert bot.follow_line({"who": ["0x12345678"], "scores": [90]}) is None, "an address is not a handle"
    assert bot.follow_line({"who": ["a.b"], "scores": [90]}) == "follow the best of them: /follow a.b"
    assert bot.follow_line({}) is None
