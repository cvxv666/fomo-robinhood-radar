"""Wallet resolution: fomo tells us which token was traded and when, never by whom.

These cover the decision logic offline. The Codex lookup that fills the windows is exercised
against live data by `cli resolve --handle`.
"""
import pytest

from fomo_agent.pipeline.resolve import decide, rank_candidates
from fomo_agent.sources.fomo import parse_swap_rows


def test_quiet_windows_outweigh_crowded_ones():
    """Being one of three makers is evidence; being one of a hundred is not."""
    quiet_a = {"0xme", "0xa", "0xb"}
    quiet_b = {"0xme", "0xc", "0xd"}
    crowded = {"0xme", *(f"0x{i:03d}" for i in range(100))}
    ranked = dict((a, s) for a, s, _ in rank_candidates([quiet_a, quiet_b, crowded]))
    assert max(ranked, key=ranked.get) == "0xme"
    # sharing two quiet windows is worth far more than sharing one crowded one
    assert ranked["0xme"] > 2 * ranked["0xa"]
    assert ranked["0xa"] > 30 * ranked["0x042"]


def test_rank_ignores_empty_windows():
    assert rank_candidates([set(), set()]) == []
    ranked = rank_candidates([set(), {"0xme", "0xa"}])
    assert [r[0] for r in ranked] == ["0xme", "0xa"] or [r[0] for r in ranked] == ["0xa", "0xme"]
    assert all(r[2] == 1 for r in ranked)


def test_decide_requires_hits_and_a_margin():
    strong = rank_candidates([{"0xme", "0xa"}, {"0xme", "0xb"}, {"0xme", "0xc"}])
    addr, info = decide(strong, used=3, min_hits=3, min_ratio=1.4)
    assert addr == "0xme" and info["hits"] == 3

    # same wallet, only two windows: not enough evidence
    thin = rank_candidates([{"0xme", "0xa"}, {"0xme", "0xb"}])
    addr, info = decide(thin, used=2, min_hits=3, min_ratio=1.4)
    assert addr is None and "hits" in info["reason"]

    # two wallets that always appear together cannot be told apart
    tied = rank_candidates([{"0xme", "0xtwin"}] * 4)
    addr, info = decide(tied, used=4, min_hits=3, min_ratio=1.4)
    assert addr is None and "ratio" in info["reason"]

    assert decide([], used=0) == (None, {"reason": "no candidates", "windows": 0})


def test_a_single_candidate_wins_outright():
    addr, info = decide(rank_candidates([{"0xme"}] * 3), used=3)
    assert addr == "0xme" and info["ratio"] is None      # nothing to compare against


def test_every_swap_is_its_own_window(tmp_path):
    """Fifty-seven swaps in two names used to be two windows; now they are as many as asked for."""
    from fomo_agent import db
    from fomo_agent.pipeline.resolve import user_windows
    conn = db.connect(tmp_path / "w.db")
    with db.tx(conn):
        conn.execute("INSERT INTO fomo_users(user_id, handle) VALUES('u1', 'visi')")
        for i in range(6):
            conn.execute("INSERT INTO fomo_swaps(user_id, chain, token, ts, side, swap_id) VALUES('u1','robinhood',?,?,?,?)",
                         ("0x" + "a" * 40 if i % 2 else "0x" + "b" * 40, 1000 + i * 100, "buy", f"s{i}"))
    assert len(user_windows(conn, "u1", "robinhood", 12)) == 6
    assert len(user_windows(conn, "u1", "robinhood", 4)) == 4


def test_a_failed_resolution_is_stamped_and_not_retried_at_once(tmp_path):
    from fomo_agent import db
    from fomo_agent.pipeline import resolve as r
    conn = db.connect(tmp_path / "f.db")
    with db.tx(conn):
        conn.execute("INSERT INTO fomo_users(user_id, handle, pnl_30d) VALUES('u1', 'visi', 10)")
        conn.execute("INSERT INTO fomo_swaps(user_id, chain, token, ts, side, swap_id) VALUES('u1','robinhood',?,?,?,?)",
                     ("0x" + "a" * 40, 1000, "buy", "s1"))

    class NoMakers:
        requests = 0
    # drive resolve_pending with a maker source that never finds anybody
    import fomo_agent.pipeline.resolve as mod
    mod_source = mod.maker_source
    mod.maker_source = lambda chain, codex=None, rpc=None: ((lambda token, ts, side: set()), NoMakers())
    try:
        first = r.resolve_pending(conn, chain="robinhood", limit=5)
        again = r.resolve_pending(conn, chain="robinhood", limit=5)
    finally:
        mod.maker_source = mod_source
    assert first["attempted"] == 1 and first["unresolved"] == 1
    assert again["attempted"] == 0, "stamped, so the next pass moves on"
    row = conn.execute("SELECT onchain_at, onchain_note FROM fomo_users WHERE user_id='u1'").fetchone()
    assert row["onchain_at"] and "no candidates" in row["onchain_note"]


def test_swap_rows_split_legs_and_drop_quote_tokens():
    swap = {
        "id": "abc", "createdAt": "2026-09-04T07:40:45.450Z",
        "inTokenAddress": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC, tells us nothing
        "inNetworkId": 1399811149, "inHumanAmount": 4974.94, "humanUsdAmountIn": 4996.69,
        "outTokenAddress": "0x12D5Ee7917cA430073c3a638Ee1E6f0648a98A01",
        "outNetworkId": 4663, "outHumanAmount": 483884.72, "humanUsdAmountOut": 4910.94,
    }
    rows = parse_swap_rows("user-1", swap)
    assert len(rows) == 1
    r = rows[0]
    assert r["chain"] == "robinhood" and r["side"] == "buy"
    assert r["token"] == "0x12d5ee7917ca430073c3a638ee1e6f0648a98a01"   # lowercased
    assert r["ts"] == 1788507645 and r["usd"] == pytest.approx(4910.94)
    assert r["swap_id"] == "abc:buy"

    assert parse_swap_rows("u", {"id": "x"}) == []                       # no timestamp
    assert parse_swap_rows("u", {"createdAt": "2026-09-04T07:40:45Z"}) == []   # no id
