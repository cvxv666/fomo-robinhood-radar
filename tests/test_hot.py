"""The burst rule, live and replayed, and the watcher that runs it every few seconds."""
from __future__ import annotations

from fomo_agent import db
from fomo_agent.bot import fmt_hot, fmt_hot_list
from fomo_agent.config import settings
from fomo_agent.models import Trade
from fomo_agent.pipeline import hot, watch

W = ["0x" + c * 40 for c in "abcdefgh"]
TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def buys(*rows):
    """(minute, wallet index, score) -> the tuple _bursts reads: (ts, address, score, usd, px)."""
    return [(m * 60, W[i], s, 1000.0, 1.0) for m, i, s in rows]


# ---------------------------------------------------------------- the rule


def test_a_burst_is_conviction_inside_the_window_not_over_the_day():
    """Four 80s inside half an hour fire; the same four spread over five hours do not."""
    quick = buys((0, 0, 80), (5, 1, 80), (12, 2, 80), (20, 3, 80))
    slow = buys((0, 0, 80), (90, 1, 80), (180, 2, 80), (300, 3, 80))
    assert len(hot._bursts(quick, 2.5, 30 * 60, 3)) == 1
    assert hot._bursts(slow, 2.5, 30 * 60, 3) == []


def test_it_fires_on_the_buy_that_tips_it_and_says_who():
    b = hot._bursts(buys((0, 0, 80), (5, 1, 80), (12, 2, 80), (20, 3, 80)), 2.5, 30 * 60, 3)[0]
    assert b.ts == 20 * 60, "the fourth buy is the one that crosses 2.5"
    assert b.wallets == 4 and b.conviction == 2.56
    assert b.who == W[:4] and b.usd == 4000.0


def test_weak_scores_do_not_add_up_to_a_burst():
    """Six wallets at 40 are 0.96 of conviction. Headcount is not the measure."""
    six = buys(*((i, i, 40) for i in range(6)))
    assert hot._bursts(six, 2.0, 30 * 60, 3) == []


def test_once_fires_a_single_time_per_token_and_otherwise_rearms():
    rows = buys((0, 0, 80), (5, 1, 80), (10, 2, 80), (12, 3, 80),
                (200, 4, 80), (205, 5, 80), (210, 6, 80), (212, 7, 80))
    assert len(hot._bursts(rows, 2.5, 30 * 60, 3, once=True)) == 1
    assert len(hot._bursts(rows, 2.5, 30 * 60, 3, once=False)) == 2, "two separate bursts"


# ---------------------------------------------------------------- live


def seed(conn, now):
    with db.tx(conn):
        for i, (score, status) in enumerate(((88, "active"), (80, "active"), (76, "active"),
                                             (70, "watch"), (30, "dropped"))):
            db.upsert_trader(conn, W[i], chain="robinhood", fomo_handle=f"w{i}", score=score,
                             status=status)
        db.upsert_token(conn, TOKEN, chain="robinhood", symbol="BURST", liquidity_usd=50_000)
        db.upsert_token(conn, OTHER, chain="robinhood", symbol="SLOW")
        # BURST: four trusted wallets in twelve minutes, and a dud (earlier) who does not count
        for i, mins in ((0, 14), (1, 9), (2, 5), (3, 2), (4, 20)):
            db.insert_trade(conn, sig=f"b{i}", address=W[i], chain="robinhood", mint=TOKEN,
                            side="buy", usd_value=500.0 * (i + 1), token_amount=1000.0,
                            ts=now - mins * 60, source="rpc")
        # a wallet that bought BURST yesterday and again now is not a new entrant now
        db.insert_trade(conn, sig="old", address=W[0], chain="robinhood", mint=TOKEN, side="buy",
                        usd_value=100.0, token_amount=100.0, ts=now - 86400, source="rpc")
        # SLOW: the same wallets over six hours
        for i, mins in ((0, 350), (1, 240), (2, 120), (3, 3)):
            db.insert_trade(conn, sig=f"s{i}", address=W[i], chain="robinhood", mint=OTHER,
                            side="buy", usd_value=1000.0, token_amount=1000.0,
                            ts=now - mins * 60, source="rpc")


def test_hot_now_reports_what_is_bursting_and_ignores_what_drifted(tmp_path):
    conn = db.connect(tmp_path / "hot.db")
    now = db.now()
    seed(conn, now)
    out = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)
    assert [h["sym"] for h in out] == ["BURST"]
    h = out[0]
    # W[0] first bought a day ago, so only three of the four are entrants inside the window
    assert h["wallets"] == 3 and h["who"] == ["w1", "w2", "w3"]
    assert h["conviction"] == round(0.8 ** 2 + 0.76 ** 2 + 0.70 ** 2, 2)
    assert h["usd"] == 1000.0 + 1500.0 + 2000.0, "the dud's money is not counted"
    assert h["age_s"] == 86400, "age is from the earliest fill anybody we track made"


def test_the_message_leads_with_the_clock(tmp_path):
    conn = db.connect(tmp_path / "hot.db")
    now = db.now()
    seed(conn, now)
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)[0]
    text = fmt_hot(h, now)
    assert "burst" in text and "in 7 min" in text, "from the 9-minute buy to the 2-minute one"
    assert "wallets in" in text and "3" in text
    assert TOKEN in text, "the contract is there to copy"
    assert "Nothing is bursting" in fmt_hot_list([], 30)


# ---------------------------------------------------------------- the watcher


class FakeChain:
    """Stands in for the RPC: a head that advances and a scan that answers with fills."""

    def __init__(self, head, fills):
        self.head_block, self.fills, self.scans, self.decimals_loaded = head, fills, [], False

    def head(self):
        return self.head_block, 1_700_000_000

    def scan(self, wallets, first, last):
        self.scans.append((len(wallets), first, last))
        return self.fills

    def load_decimals(self, known):
        self.decimals_loaded = True

    def known_decimals(self):
        return {}


def test_a_bursting_token_without_a_name_is_named_on_the_spot(tmp_path, monkeypatch):
    """The naming pass runs every fifteen minutes; a burst is minutes old. One lookup, now."""
    from types import SimpleNamespace
    conn = db.connect(tmp_path / "n.db")
    now = db.now()
    seed(conn, now)
    with db.tx(conn):
        conn.execute("UPDATE tokens SET symbol=NULL WHERE mint=?", (TOKEN,))
    asked = []

    def fake_lookup(chain, mints, **kw):
        asked.append(mints)
        return [SimpleNamespace(mint=TOKEN, chain="robinhood", symbol="NAMED", mcap_usd=1.0,
                                liquidity_usd=2.0, created_at=None, price_usd=3.0, decimals=18,
                                pool_address=None)], 1
    monkeypatch.setattr("fomo_agent.pipeline.new_tokens.lookup_tokens", fake_lookup)
    burning = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)
    assert burning[0]["sym"] == TOKEN[:8], "no name yet"
    assert watch.name(conn, burning) == 1 and asked == [[TOKEN]]
    assert burning[0]["sym"] == "NAMED"
    assert conn.execute("SELECT symbol FROM tokens WHERE mint=?", (TOKEN,)).fetchone()[0] == "NAMED"


def test_one_tick_reads_only_the_blocks_since_last_time_and_writes_the_fills(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    seed(conn, now)
    monkeypatch.setattr(settings, "hot_delta", 99.0)   # nothing bursts in this test
    fill = Trade(sig="new1", address=W[1], chain="robinhood", mint=TOKEN, side="buy",
                 usd_value=10.0, token_amount=10.0, ts=now, source="rpc")
    chain = FakeChain(head=10_000, fills={W[1]: [fill]})
    w = watch.Watch(rpc=chain)

    first = watch.tick(conn, w, now)
    assert first["from"] == 10_000 - settings.watch_start_back_blocks, "a minute back, not zero"
    assert first["fills"] == 1 and chain.decimals_loaded
    assert chain.scans[0][0] == 4, "the roster is the tracked robinhood wallets, dud excluded"

    chain.head_block = 10_200
    second = watch.tick(conn, w, now)
    assert second["from"] == 10_001 and second["blocks"] == 200
    assert second["fills"] == 0, "the same fill again is not a second fill"

    chain.head_block = 10_200 + 50_000
    third = watch.tick(conn, w, now)
    assert third["from"] == chain.head_block - settings.watch_max_range_blocks, "a stall does not become a backfill"


def test_a_burst_is_pushed_once_per_subscriber(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "w.db")
    now = db.now()
    seed(conn, now)
    monkeypatch.setattr(settings, "hot_delta", 1.5)
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    from fomo_agent import bot
    with db.tx(conn):
        bot.subscribe(conn, "chat1", "one")
        bot.subscribe(conn, "chat2", "two")
    sent = []

    class FakeTg:
        def __init__(self):
            pass

        def send(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(bot, "Telegram", FakeTg)
    chain = FakeChain(head=10_000, fills={})
    w = watch.Watch(rpc=chain)
    first = watch.tick(conn, w, now)
    assert first["hot"] == 1 and first["sent"] == 2
    assert {c for c, _ in sent} == {"chat1", "chat2"} and "BURST" in sent[0][1]
    chain.head_block += 200
    again = watch.tick(conn, w, now)
    assert again["hot"] == 1 and again["sent"] == 0, "still bursting, already told"


# ---------------------------------------------------------------- the record and its scorecard


def test_a_burst_is_written_once_and_read_back_with_what_followed(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    now = db.now()
    seed(conn, now)
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)[0]
    assert h["px"] == 2.0, "the last entrant paid $2000 for 1000 tokens"

    assert hot.record(conn, h, quiet_s=12 * 3600, chain="robinhood") is True
    assert hot.record(conn, h, quiet_s=12 * 3600, chain="robinhood") is False, "same burst, next tick"

    # what happened next: a fill at 3x, then one at 1.5x, and the token quotes 0.4x now
    with db.tx(conn):
        db.insert_trade(conn, sig="l1", address=W[1], chain="robinhood", mint=TOKEN, side="sell",
                        usd_value=6000.0, token_amount=1000.0, ts=now + 3600, source="rpc")
        db.insert_trade(conn, sig="l2", address=W[2], chain="robinhood", mint=TOKEN, side="sell",
                        usd_value=3000.0, token_amount=1000.0, ts=now + 7200, source="rpc")
        db.upsert_token(conn, TOKEN, chain="robinhood", price_usd=0.8)
    later = now + 4 * 3600
    rows = hot.recent(conn, "robinhood", hours=24, now=later)
    assert len(rows) == 1
    r = rows[0]
    assert (r["best"], r["last"], r["now"]) == (3.0, 1.5, 0.4)
    assert r["who"] == ["w1", "w2", "w3"] and r["age_at_read_h"] == 4.0


def test_the_pool_candles_correct_a_tape_that_held_through_the_run(tmp_path):
    """The cohort bought at 2.0 and its only later fill is at 3.0; the pool went to 44.0 while
    they sat in it. The tape alone says 1.5x. The candles say 22x, and that is the answer."""
    conn = db.connect(tmp_path / "c.db")
    now = db.now()
    seed(conn, now)
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)[0]
    hot.record(conn, h, quiet_s=3600, chain="robinhood")
    with db.tx(conn):
        db.insert_trade(conn, sig="l1", address=W[1], chain="robinhood", mint=TOKEN, side="sell",
                        usd_value=3000.0, token_amount=1000.0, ts=now + 600, source="rpc")
    burst_ts = h["last_ts"]
    candles = [[burst_ts - 7200, 1.5, 1.6, 1.4, 1.5, 100.0],   # before: does not count
               [burst_ts - 200, 1.9, 44.0, 1.8, 30.0, 90_000.0],    # the candle that holds the burst
               [burst_ts + 3000, 30.0, 33.0, 20.0, 21.0, 50_000.0]]
    r = hot.recent(conn, "robinhood", hours=24, now=now + 4 * 3600,
                   candles_for=lambda mint: candles if mint == TOKEN else None)[0]
    assert r["best"] == 22.0 and r["last"] == 1.5 and r["now"] == 10.5
    plain = hot.recent(conn, "robinhood", hours=24, now=now + 4 * 3600)[0]
    assert plain["best"] == 1.5, "without candles the tape is the only witness"


def test_a_mispriced_tape_row_does_not_beat_the_candles(tmp_path):
    """One FRONTIER sell carried a dollar leg four times the pool's price that minute and read as
    9.3x against a real 4x. With candles that agree with the fill, the pool's high is the peak."""
    conn = db.connect(tmp_path / "m.db")
    now = db.now()
    seed(conn, now)
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)[0]
    hot.record(conn, h, quiet_s=3600, chain="robinhood")
    with db.tx(conn):   # a sell at 18.6 per token while the pool never traded above 8
        db.insert_trade(conn, sig="odd", address=W[1], chain="robinhood", mint=TOKEN, side="sell",
                        usd_value=614.0, token_amount=33.0, ts=now + 600, source="rpc")
    burst_ts = h["last_ts"]
    candles = [[burst_ts - 200, 1.9, 8.0, 1.8, 6.0, 90_000.0], [burst_ts + 3000, 6.0, 7.0, 4.0, 4.5, 50_000.0]]
    r = hot.recent(conn, "robinhood", hours=24, now=now + 4 * 3600,
                   candles_for=lambda mint: candles if mint == TOKEN else None)[0]
    assert r["best"] == 4.0, "the pool's high, not the odd fill"
    plain = hot.recent(conn, "robinhood", hours=24, now=now + 4 * 3600)[0]
    assert plain["best"] == 9.3, "with no candles the tape is all there is, odd rows included"


def test_a_burst_with_no_tape_after_it_is_unmeasured_not_flat(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    now = db.now()
    seed(conn, now)
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now)[0]
    hot.record(conn, h, quiet_s=3600, chain="robinhood")
    r = hot.recent(conn, "robinhood", hours=24, now=now + 60)[0]
    assert r["best"] is None and r["last"] is None and r["fills"] == 0


def test_the_digest_carries_the_scorecard(tmp_path, monkeypatch):
    from fomo_agent.bot import fmt_digest
    from fomo_agent.pipeline.digest import daily

    conn = db.connect(tmp_path / "d.db")
    now = db.now()
    seed(conn, now - 5 * 3600)   # the burst was five hours ago
    h = hot.hot_now(conn, "robinhood", delta=1.5, window_s=30 * 60, min_wallets=3, now=now - 5 * 3600)[0]
    hot.record(conn, h, quiet_s=3600, chain="robinhood")
    with db.tx(conn):
        db.insert_trade(conn, sig="l1", address=W[1], chain="robinhood", mint=TOKEN, side="sell",
                        usd_value=5000.0, token_amount=1000.0, ts=now - 3600, source="rpc")
    monkeypatch.setattr("fomo_agent.pipeline.digest.health_report", lambda c: {"ok": True, "checks": []})
    d = daily(conn, hours=24, chain="robinhood")
    assert d["bursts"]["n"] == 1 and d["bursts"]["measured"] == 1
    assert d["bursts"]["reached_2x"] == 1 and d["bursts"]["median_best"] == 2.5
    text = fmt_digest(d)
    assert "Bursts" in text and "1 reached 2x" in text and "best 2.5x" in text
