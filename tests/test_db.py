"""Storage invariants that no caller is allowed to break."""
import pytest

from fomo_agent import db

WALLET = "0x" + "a" * 40
MINT = "0x" + "d" * 40
TX = "0x" + "1" * 64


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "db.db")


def test_the_same_fill_from_two_sources_lands_once(conn):
    """Signatures carry a source-specific suffix, so the primary key alone is not enough."""
    common = dict(address=WALLET, chain="robinhood", mint=MINT, side="buy", ts=db.now())
    with db.tx(conn):
        assert db.insert_trade(conn, sig=f"{TX}:57526", usd_value=10.31, source="trenches", **common)
        # the chain reports the same trade, numbered by log index instead of tape sequence
        assert not db.insert_trade(conn, sig=f"{TX}:83", usd_value=10.30, source="rpc", **common)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_different_fills_in_one_transaction_both_land(conn):
    common = dict(address=WALLET, chain="robinhood", ts=db.now(), source="rpc")
    with db.tx(conn):
        assert db.insert_trade(conn, sig=f"{TX}:1", mint=MINT, side="buy", **common)
        assert db.insert_trade(conn, sig=f"{TX}:2", mint=MINT, side="sell", **common)
        assert db.insert_trade(conn, sig=f"{TX}:3", mint="0x" + "e" * 40, side="buy", **common)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


def test_fill_key_ignores_the_signature_suffix():
    base = dict(sig=f"{TX}:57526", address=WALLET, mint=MINT, side="buy")
    assert db.fill_key(base) == db.fill_key({**base, "sig": f"{TX}:83"})
    assert db.fill_key(base) != db.fill_key({**base, "side": "sell"})
    assert db.fill_key({}) == ":::"


def test_a_position_never_stores_a_negative_cost(conn):
    """fomo's pnl can exceed a holding's value because it counts profit already taken out."""
    with db.tx(conn):
        db.upsert_fomo_position(conn, trade_id="p1", user_id="u1", token=MINT,
                                unrealized_pnl=900_000.0, cost_basis=-40_000.0, avg_entry=-2.0)
    row = conn.execute("SELECT * FROM fomo_positions WHERE trade_id='p1'").fetchone()
    assert row["cost_basis"] is None and row["avg_entry"] is None
    assert row["unrealized_pnl"] == 900_000.0, "the profit itself is still real"


def test_a_position_keeps_a_real_cost(conn):
    with db.tx(conn):
        db.upsert_fomo_position(conn, trade_id="p2", user_id="u1", token=MINT,
                                unrealized_pnl=100.0, cost_basis=25.0)
    assert conn.execute("SELECT cost_basis FROM fomo_positions WHERE trade_id='p2'").fetchone()[0] == 25.0


def test_a_connection_survives_being_closed_on_another_thread(tmp_path):
    """FastAPI may run a handler on one worker thread and its teardown on another."""
    import threading

    conn = db.connect(tmp_path / "threads.db")
    conn.execute("SELECT COUNT(*) FROM traders").fetchone()

    error: list[Exception] = []

    def close_elsewhere():
        try:
            conn.execute("SELECT COUNT(*) FROM trades").fetchone()
            conn.close()
        except Exception as e:  # noqa: BLE001 - the point of the test is that this does not happen
            error.append(e)

    t = threading.Thread(target=close_elsewhere)
    t.start()
    t.join()
    assert error == [], f"connection refused a cross-thread close: {error}"


def test_a_held_token_is_re_quoted_once_its_price_goes_stale(tmp_path):
    """A price marks an open position, so it has to be re-asked; a name never does."""
    from fomo_agent.pipeline.new_tokens import stale_price_tokens

    conn = db.connect(tmp_path / "prices.db")
    now = db.now()
    held, sold_out, untracked = ("0x" + "1" * 40), ("0x" + "2" * 40), ("0x" + "3" * 40)
    with db.tx(conn):
        for mint in (held, sold_out, untracked):
            db.upsert_token(conn, mint, chain="robinhood", symbol="X")
        db.insert_trade(conn, sig="0x1", address="0xw", chain="robinhood", mint=held, side="buy",
                        usd_value=100.0, ts=now, source="rpc")
        db.insert_trade(conn, sig="0x2", address="0xw", chain="robinhood", mint=sold_out,
                        side="sell", usd_value=100.0, ts=now, source="rpc")

    due = [r["token"] for r in stale_price_tokens(conn)]
    assert held in due, "somebody's money is in it"
    assert sold_out not in due and untracked not in due, "nothing to mark, nothing to ask about"

    with db.tx(conn):
        db.upsert_token(conn, held, price_usd=0.01, price_at=now - 600, checked_at=now - 600)
    assert stale_price_tokens(conn) == [], "a token just asked about is not asked again"
    assert [r["token"] for r in stale_price_tokens(conn, max_age_s=60)] == [held], "an old one is"
    # a buy from three weeks ago does not keep a token in the queue on its own ...
    old = "0x" + "4" * 40
    with db.tx(conn):
        db.upsert_token(conn, old, chain="robinhood", symbol="OLD")
        db.upsert_trader(conn, "0xw", chain="robinhood", status="active")
        db.insert_trade(conn, sig="0x4", address="0xw", chain="robinhood", mint=old, side="buy",
                        usd_value=100.0, ts=now - 21 * 86400, source="rpc")
    assert old not in [r["token"] for r in stale_price_tokens(conn, max_age_s=60)], "nobody has been in it for weeks"
    # ... but a balance that says the wallet still holds it does
    with db.tx(conn):
        db.save_holdings(conn, {("0xw", old): 1234.0})
    assert old in [r["token"] for r in stale_price_tokens(conn, max_age_s=60)], "still held, still marked"

    # a token no source can price is stamped as asked, so it stops crowding out the ones with one
    with db.tx(conn):
        db.upsert_token(conn, held, checked_at=now)
        db.upsert_token(conn, old, checked_at=now)
    assert stale_price_tokens(conn, max_age_s=60) == []


def test_decimals_survive_between_collection_passes(tmp_path):
    """Each pass is a new process; what the chain answered once must not be asked again."""
    conn = db.connect(tmp_path / "dec.db")
    a, b = ("0x" + "1" * 40), ("0x" + "2" * 40)
    with db.tx(conn):
        db.upsert_token(conn, a, chain="robinhood", symbol="A")
    assert db.token_decimals(conn) == {}

    with db.tx(conn):
        assert db.save_token_decimals(conn, {a: 6, b: 18, "0xnope": None}) == 2
    assert db.token_decimals(conn) == {a: 6, b: 18}, "a token we had never seen is stored too"

    with db.tx(conn):
        assert db.save_token_decimals(conn, {a: 6, b: 18}) == 0, "nothing new, nothing written"


def test_holdings_are_stored_and_re_read_when_stale(tmp_path):
    """A balance is a fact with a timestamp; the pass that refreshes it works oldest-first."""
    from fomo_agent.pipeline.holdings import stale_pairs
    from fomo_agent.sources.rpc import USDG

    conn = db.connect(tmp_path / "hold.db")
    wallet, token = "0x" + "a" * 40, "0x" + "1" * 40
    with db.tx(conn):
        db.upsert_trader(conn, wallet, chain="robinhood", status="active")
        for i, mint in enumerate((token, USDG)):
            db.insert_trade(conn, sig=f"0x{i}", address=wallet, chain="robinhood", mint=mint,
                            side="buy", usd_value=100.0, ts=db.now(), source="rpc")

    assert stale_pairs(conn) == [(wallet, token)], "the stablecoin is cash, not a position"

    with db.tx(conn):
        assert db.save_holdings(conn, {(wallet, token): 12.5}) == 1
    assert db.holdings_for(conn, wallet)[token][0] == 12.5
    assert stale_pairs(conn) == [], "a fresh read is not repeated"
    with db.tx(conn):
        conn.execute("UPDATE holdings SET ts = ?", (db.now() - 600,))
    assert stale_pairs(conn, max_age_s=60) == [(wallet, token)], "a stale one is"

    with db.tx(conn):
        db.save_holdings(conn, {(wallet, token): 0.0})
    assert db.holdings_for(conn, wallet)[token][0] == 0.0, "a zero overwrites, it does not vanish"


def test_backfill_widens_the_shallowest_histories_first(tmp_path):
    """A repeated run should reach the wallets we know least about, not deepen the deepest."""
    from fomo_agent.pipeline.backfill import wallets_to_backfill

    conn = db.connect(tmp_path / "bf.db")
    now = db.now()
    deep, shallow, untouched = ("0x" + c * 40 for c in "abc")
    with db.tx(conn):
        for a in (deep, shallow, untouched):
            db.upsert_trader(conn, a, chain="robinhood", status="active")
        db.upsert_trader(conn, "0x" + "d" * 40, chain="robinhood", status="dropped")
        db.insert_trade(conn, sig="0xd", address=deep, chain="robinhood", mint="0xm",
                        side="buy", ts=now - 30 * 86400, source="rpc")
        db.insert_trade(conn, sig="0xs", address=shallow, chain="robinhood", mint="0xm",
                        side="buy", ts=now - 3600, source="rpc")

    order = wallets_to_backfill(conn)
    assert order.index(shallow) < order.index(deep), "an hour of history before a month of it"
    assert untouched in order, "a wallet with no tape at all still needs one"
    assert "0x" + "d" * 40 not in order, "a dropped wallet is not worth the requests"


def test_backfill_narrows_a_range_the_endpoint_refuses(tmp_path, monkeypatch):
    """The endpoint knows how wide a range it will serve; a timeout is that answer, not a loss."""
    from fomo_agent.config import settings
    from fomo_agent.pipeline import backfill as bf

    monkeypatch.setattr(settings, "backfill_min_window_blocks", 100)
    monkeypatch.setattr(settings, "backfill_cooldown_s", 0)
    conn = db.connect(tmp_path / "bfn.db")
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "a" * 40, chain="robinhood", status="active")

    asked, served = [], []

    class Chain:
        requests = 0
        throttled = False

        def block_number(self):
            return 10_000

        def block_timestamp(self, b):
            return 1_700_000_000 + b

        def windows(self, back_to, head=None, span=None):
            return [(9_000, 10_000)]

        def load_decimals(self, known):
            pass

        def known_decimals(self):
            return {}

        def scan(self, wallets, first, last):
            asked.append((first, last))
            self.requests += 1
            if last - first > 400:                      # too wide, like the real endpoint
                raise RuntimeError("eth_getLogs: log query timed out")
            if not served and not self.throttled:        # and once, too fast
                self.throttled = True
                raise RuntimeError("rate limited after 4 attempts")
            served.append((first, last))
            return {}

    stats = bf.backfill(conn, days=1, rpc=Chain(), max_requests=100)
    assert stats["narrowed"] >= 2, "a refused range became halves, and those halves halved again"
    assert stats["waited"] == 1, "and a 429 was waited out rather than skipped"
    assert all(last - first <= 400 for first, last in served), "only ranges it would serve"
    covered = sorted(served)
    assert covered[0][0] == 9_000 and covered[-1][1] == 10_000, "the whole range still got asked"
    assert stats["failed"] == 0


def test_a_later_pass_completes_a_fill_without_overwriting_it(tmp_path):
    """Sources know different things about the same trade; the second one fills the gaps."""
    conn = db.connect(tmp_path / "fill.db")
    key = dict(sig="0xabc", address="0xw", chain="robinhood", mint="0xm", side="buy")

    with db.tx(conn):
        # the live tracker priced it but recorded no size
        assert db.insert_trade(conn, **key, usd_value=100.0, ts=10, source="rpc") is True
        # the backfill comes back over the same block with the size, and a different price
        assert db.insert_trade(conn, **key, usd_value=999.0, token_amount=42.0, ts=10,
                               source="rpc") is False

    row = conn.execute("SELECT usd_value, token_amount FROM trades").fetchone()
    assert row["token_amount"] == 42.0, "the gap is filled"
    assert row["usd_value"] == 100.0, "and what was already recorded is not second-guessed"


def test_thesis_keeps_when_it_was_said(tmp_path):
    """An edited thesis replaces its text but not its date.

    When someone said a thing is most of what it is worth: a call written before the token moved is
    evidence, the same words added after the move are commentary, and only the timestamp separates
    them. So `seen_at` follows the edit and `first_seen_at` does not.
    """
    conn = db.connect(tmp_path / "t.db")
    with db.tx(conn):
        assert db.upsert_thesis(conn, trade_id="t1", user_id="u1", mint="0xaa",
                                text="early, before the chart existed", cost_usd=5000) is True
    first = conn.execute("SELECT first_seen_at, seen_at FROM theses WHERE trade_id='t1'").fetchone()

    with db.tx(conn):
        # a second sighting is not a new thesis
        assert db.upsert_thesis(conn, trade_id="t1", user_id="u1", mint="0xaa",
                                text="edited after it ran 40x", cost_usd=5000) is False
    row = conn.execute("SELECT text, first_seen_at FROM theses WHERE trade_id='t1'").fetchone()
    assert row["text"] == "edited after it ran 40x"
    assert row["first_seen_at"] == first["first_seen_at"]

    # nothing to say is not a thesis, and neither is whitespace
    with db.tx(conn):
        assert db.upsert_thesis(conn, trade_id="t2", user_id="u1", mint="0xaa", text="   ") is False
        assert db.upsert_thesis(conn, trade_id="t3", user_id="u1", mint="0xaa", text=None) is False
    assert conn.execute("SELECT COUNT(*) c FROM theses").fetchone()["c"] == 1


def test_theses_only_from_wallets_with_a_verdict(tmp_path):
    """An unscored handle writing 'wagmi' is noise; the whole product is that whose money it is decides."""
    conn = db.connect(tmp_path / "t.db")
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "1" * 40, chain="robinhood")
        conn.execute("UPDATE traders SET score=88, status='active' WHERE address=?", ("0x" + "1" * 40,))
        db.upsert_fomo_user(conn, "u1", handle="scored")
        conn.execute("UPDATE fomo_users SET onchain_address=? WHERE user_id='u1'", ("0x" + "1" * 40,))
        db.upsert_fomo_user(conn, "u2", handle="unscored")
        db.upsert_thesis(conn, trade_id="t1", user_id="u1", mint="0xaa", text="the scored one")
        db.upsert_thesis(conn, trade_id="t2", user_id="u2", mint="0xaa", text="the unscored one")

    out = db.theses_for_token(conn, "0xaa")
    assert [t["handle"] for t in out] == ["scored"]
    assert out[0]["score"] == 88
