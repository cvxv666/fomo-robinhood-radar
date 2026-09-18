"""SQLite schema, versioned migrations, and small upsert helpers. No ORM."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS traders(
      address TEXT PRIMARY KEY,
      chain TEXT DEFAULT 'solana',
      fomo_handle TEXT,
      source TEXT,
      first_seen_at INTEGER,
      last_seen_at INTEGER,
      pnl_7d REAL, pnl_30d REAL, win_rate REAL, trades_cnt INTEGER,
      status TEXT DEFAULT 'candidate',
      score INTEGER, tags TEXT, ai_summary TEXT, ai_scored_at INTEGER, ai_model TEXT,
      last_tracked_ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS tokens(
      mint TEXT PRIMARY KEY,
      symbol TEXT, mcap_usd REAL, liquidity_usd REAL,
      created_at INTEGER, first_seen_at INTEGER, triggered_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS trades(
      sig TEXT PRIMARY KEY,
      address TEXT, mint TEXT, side TEXT,
      sol_amount REAL, token_amount REAL, usd_value REAL,
      ts INTEGER, source TEXT
    );
    CREATE TABLE IF NOT EXISTS score_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      address TEXT, score INTEGER, status TEXT, model TEXT, reason TEXT, ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT, started_at INTEGER, finished_at INTEGER, stats_json TEXT, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_trades_addr_ts ON trades(address, ts);
    CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_traders_status ON traders(status);
    """,
    2: """
    ALTER TABLE tokens ADD COLUMN chain TEXT DEFAULT 'solana';
    CREATE INDEX IF NOT EXISTS idx_traders_chain ON traders(chain);
    """,
    3: """
    ALTER TABLE trades ADD COLUMN chain TEXT DEFAULT 'solana';
    """,
    4: """
    ALTER TABLE traders ADD COLUMN fomo_user_id TEXT;
    ALTER TABLE traders ADD COLUMN profile_address TEXT;
    ALTER TABLE traders ADD COLUMN evm_address TEXT;
    ALTER TABLE traders ADD COLUMN pnl_24h REAL;
    ALTER TABLE traders ADD COLUMN volume_usd REAL;
    CREATE INDEX IF NOT EXISTS idx_traders_fomo_user ON traders(fomo_user_id);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS fomo_users(
      user_id TEXT PRIMARY KEY,
      handle TEXT, profile_address TEXT, evm_address TEXT,
      pnl_24h REAL, pnl_7d REAL, pnl_30d REAL, trades_cnt INTEGER, volume_usd REAL,
      source TEXT, first_seen_at INTEGER, last_seen_at INTEGER,
      resolved_at INTEGER, resolve_error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_fomo_users_resolved ON fomo_users(resolved_at);
    """,
    6: """
    ALTER TABLE traders ADD COLUMN stats_json TEXT;
    ALTER TABLE traders ADD COLUMN stats_at INTEGER;
    ALTER TABLE traders ADD COLUMN stats_source TEXT;
    """,
    7: """
    CREATE TABLE IF NOT EXISTS fomo_swaps(
      swap_id TEXT PRIMARY KEY,
      user_id TEXT, chain TEXT, token TEXT, side TEXT,
      ts INTEGER, usd REAL, amount REAL
    );
    CREATE INDEX IF NOT EXISTS idx_fomo_swaps_user ON fomo_swaps(user_id, chain, ts);
    ALTER TABLE fomo_users ADD COLUMN onchain_at INTEGER;
    ALTER TABLE fomo_users ADD COLUMN onchain_address TEXT;
    ALTER TABLE fomo_users ADD COLUMN onchain_note TEXT;
    """,
    8: """
    CREATE TABLE IF NOT EXISTS fomo_positions(
      trade_id TEXT PRIMARY KEY,
      user_id TEXT, chain TEXT, token TEXT, symbol TEXT,
      opened_at INTEGER, closed_at INTEGER,
      amount REAL, avg_entry REAL, avg_exit REAL,
      realized_pnl REAL, unrealized_pnl REAL, cost_basis REAL,
      current_price REAL, liquidity REAL, seen_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_positions_user ON fomo_positions(user_id);
    CREATE INDEX IF NOT EXISTS idx_positions_token ON fomo_positions(token);
    """,
    9: """
    -- Two sources describing the same fill number their signatures differently (a log index, a
    -- tape sequence), so the primary key alone lets one trade land twice. `fill_key` is what the
    -- chain considers the same event, and the unique index makes the duplicate impossible.
    ALTER TABLE trades ADD COLUMN fill_key TEXT;
    UPDATE trades SET fill_key = substr(sig, 1, 66) || ':' || address || ':' || mint || ':' || side;
    DELETE FROM trades WHERE rowid NOT IN (
      SELECT MIN(rowid) FROM trades GROUP BY fill_key
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_fill ON trades(fill_key);
    """,
    10: """
    -- Telegram: who wants signals pushed, and what each of them has already been told, so a
    -- token that stays hot for a day does not become a day of identical messages.
    CREATE TABLE IF NOT EXISTS bot_subscribers(
      chat_id TEXT PRIMARY KEY,
      username TEXT, min_conviction REAL, subscribed_at INTEGER, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS bot_sent(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id TEXT, mint TEXT, ts INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_bot_sent ON bot_sent(chat_id, mint, ts);
    """,
    11: """
    -- A position nobody can price is half an answer: we knew what a wallet paid and how much it
    -- still holds, but not what that is worth today. DexScreener returns the price in the same
    -- response the liquidity already came from, so the mark costs no extra request; `price_at` is
    -- what lets a refresh pass tell a stale quote from a fresh one.
    ALTER TABLE tokens ADD COLUMN price_usd REAL;
    ALTER TABLE tokens ADD COLUMN price_at INTEGER;
    -- Rebuilding a wallet's book reads its whole tape one token at a time; without this the
    -- ledger scans every fill in the table for every trader page.
    CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(address, mint);
    """,
    12: """
    -- How many base units make one token. It is a constant the chain will answer for free, but
    -- every collection pass is a fresh process, so without somewhere to keep it the tracker
    -- re-asks about the same few hundred tokens four times an hour.
    ALTER TABLE tokens ADD COLUMN decimals INTEGER;
    """,
    13: """
    -- When a price source was last *asked* about this address, whatever it answered. Without it a
    -- token nobody indexes is re-requested every pass forever, and because the queue is ordered by
    -- how stale a quote is, those permanent failures sit at the front of it and starve the tokens
    -- that do have a price.
    ALTER TABLE tokens ADD COLUMN checked_at INTEGER;
    """,
    14: """
    -- What a wallet actually holds, read off the chain rather than inferred from the fills we
    -- happened to watch. This is what makes a position's size and value true for a name entered
    -- before we started watching, and for the fills older sources recorded without a size.
    CREATE TABLE IF NOT EXISTS holdings(
      address TEXT, token TEXT, amount REAL, ts INTEGER,
      PRIMARY KEY(address, token)
    );
    CREATE INDEX IF NOT EXISTS idx_holdings_ts ON holdings(ts);
    """,
    15: """
    -- The pool a token actually trades in. It arrives free alongside the price, and it is the only
    -- handle the candle endpoint takes, so without it a token page has no chart.
    ALTER TABLE tokens ADD COLUMN pool_address TEXT;
    """,
    16: """
    -- What a trader said about a position, in their own words.
    --
    -- Everything else here is inferred: we watch the tape and conclude. A thesis is the one field
    -- where the trader tells you directly, and it has been arriving all along inside the holders
    -- response we already ask for — `comment` on each holder — and getting dropped by the parser.
    --
    -- Keyed by fomo's trade id so an edited thesis replaces itself rather than accumulating, with
    -- first_seen_at kept separately because when someone said a thing is most of what it is worth.
    CREATE TABLE IF NOT EXISTS theses(
      trade_id TEXT PRIMARY KEY,
      user_id TEXT,
      mint TEXT,
      text TEXT,
      pnl_usd REAL,
      cost_usd REAL,
      is_dev INTEGER DEFAULT 0,
      first_seen_at INTEGER,
      seen_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_theses_mint ON theses(mint);
    CREATE INDEX IF NOT EXISTS idx_theses_user ON theses(user_id);

    -- When we last asked fomo about this token's holders. Stamped on the ask, not the answer, for
    -- the same reason tokens.checked_at is: a token nobody has written about must not sit at the
    -- head of the queue forever, starving the ones that would answer.
    ALTER TABLE tokens ADD COLUMN thesis_at INTEGER;
    """,
    17: """
    -- A burst the rule fired on, written the tick it formed. The bot dedups its own pushes in
    -- bot_sent; this is the record that outlives them, and it is what turns the backtest into a
    -- live one: the digest reads back what each burst went on to do, so a month from now the
    -- answer to "does this feed work" is measured rather than remembered.
    CREATE TABLE IF NOT EXISTS bursts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      mint TEXT, chain TEXT, ts INTEGER, conviction REAL, wallets INTEGER, usd REAL, px REAL,
      window_s INTEGER, age_s INTEGER, who TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bursts_mint_ts ON bursts(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_bursts_ts ON bursts(ts);
    """,
    18: """
    -- Whose trade a fill really is. A swap delivered to a wallet by an outside key calling the
    -- router, or fifty cents pushed through fomo's own flow to a famous address, both used to
    -- read as that wallet buying. `kind` is direct / dust / seed / trade once judged, flow between the
    -- scan and the size check, and NULL for rows from before this existed, which verify-fills
    -- works through. See pipeline/provenance.py.
    ALTER TABLE trades ADD COLUMN kind TEXT;
    CREATE INDEX IF NOT EXISTS idx_trades_kind ON trades(kind);
    -- each wallet's own idea of a normal buy, which is what a dust floor is relative to
    ALTER TABLE traders ADD COLUMN median_buy_usd REAL;
    """,
    19: """
    -- Can it be sold. 1 yes, 0 no, NULL not known; asked of the chain (a simulated transfer to
    -- the pool) and of the pool (buys against sells). A 0 keeps the token out of every feed the
    -- way seeded does. See pipeline/safety.py.
    ALTER TABLE tokens ADD COLUMN sellable INTEGER;
    ALTER TABLE tokens ADD COLUMN sell_checked_at INTEGER;
    ALTER TABLE tokens ADD COLUMN sell_note TEXT;
    """,
    20: """
    -- PRO access, paid in the token and burned. A quote is an exact amount of tokens for one
    -- chat: the amount is the receipt. A payment is one burn transaction, credited to a chat
    -- once and never again; one with no chat is a burn nobody has claimed yet. See pipeline/pro.py.
    ALTER TABLE bot_subscribers ADD COLUMN paid_until INTEGER;
    ALTER TABLE bot_subscribers ADD COLUMN pro_notice TEXT;
    CREATE TABLE IF NOT EXISTS pro_quotes(
      code TEXT PRIMARY KEY,
      chat_id TEXT NOT NULL, usd REAL NOT NULL, price REAL NOT NULL,
      tokens INTEGER NOT NULL, tokens_min REAL NOT NULL,
      created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'open', tx TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pro_quotes_chat ON pro_quotes(chat_id, status);
    CREATE TABLE IF NOT EXISTS pro_payments(
      tx TEXT PRIMARY KEY,
      chat_id TEXT, frm TEXT, tokens REAL NOT NULL, usd REAL, ts INTEGER NOT NULL,
      days INTEGER, source TEXT, block INTEGER, code TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pro_payments_chat ON pro_payments(chat_id, ts);
    """,
    21: """
    -- Addresses that are not traders: routers, aggregators, bots. Found by their fill rate,
    -- dropped from the roster, and never offered to wallet resolution again. See pipeline/noise.py.
    CREATE TABLE IF NOT EXISTS noise_addresses(
      address TEXT PRIMARY KEY, reason TEXT, ts INTEGER
    );
    """,
    22: """
    -- The push ledger: one row per message event, with the price the cohort was at and what
    -- earned the push, and an hour later what the market did with it. See pipeline/pushes.py.
    CREATE TABLE IF NOT EXISTS pushes(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL, mint TEXT NOT NULL, ts INTEGER NOT NULL,
      px REAL, heat REAL, conviction REAL, wallets INTEGER, liq REAL, chats INTEGER,
      followup_due INTEGER, followup_at INTEGER, best REAL, peak_min INTEGER, now_x REAL, vol_usd REAL
    );
    CREATE INDEX IF NOT EXISTS idx_pushes_mint ON pushes(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_pushes_due ON pushes(followup_due, followup_at);
    """,
    23: """
    -- API keys: a PRO chat's key raises its allowance on the public API. Handed out by the bot,
    -- checked by the API on every request, revoked by asking for a new one. See pipeline/keys.py.
    CREATE TABLE IF NOT EXISTS api_keys(
      key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, created_at INTEGER NOT NULL,
      revoked_at INTEGER, last_used INTEGER, requests INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_api_keys_chat ON api_keys(chat_id, revoked_at);
    """,
    24: """
    -- What went to X: one row per push queued for the account, the post id once it is up, and
    -- the id of the hour-later reply. See pipeline/xpost.py.
    CREATE TABLE IF NOT EXISTS x_posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      push_id INTEGER, kind TEXT, mint TEXT, due_ts INTEGER NOT NULL, text TEXT,
      posted_at INTEGER, tweet_id TEXT, reply_id TEXT, replied_at INTEGER, attempts INTEGER DEFAULT 0, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_x_posts_due ON x_posts(posted_at, due_ts);
    """,
    25: """
    -- Wallets a chat asked to follow: their fills go to it within a tick. See pipeline/follows.py.
    CREATE TABLE IF NOT EXISTS follows(
      chat_id TEXT NOT NULL, address TEXT NOT NULL, since INTEGER NOT NULL,
      PRIMARY KEY(chat_id, address)
    );
    CREATE INDEX IF NOT EXISTS idx_follows_address ON follows(address);
    """,
    26: """
    -- A PRO key's webhook (pipeline/webhooks.py), a chat's invite code and what each chat wants
    -- to hear (pipeline/prefs.py), and who came by whose invite (pipeline/referrals.py).
    ALTER TABLE api_keys ADD COLUMN webhook_url TEXT;
    ALTER TABLE api_keys ADD COLUMN webhook_failures INTEGER DEFAULT 0;
    ALTER TABLE api_keys ADD COLUMN webhook_last INTEGER;
    ALTER TABLE bot_subscribers ADD COLUMN kinds TEXT;
    ALTER TABLE bot_subscribers ADD COLUMN quiet_from INTEGER;
    ALTER TABLE bot_subscribers ADD COLUMN quiet_to INTEGER;
    ALTER TABLE bot_subscribers ADD COLUMN ref_code TEXT;
    -- the bar used to be copied from the bot's own on subscribe and never read; now it is the
    -- chat's, NULL until the chat sets one with /minconv
    UPDATE bot_subscribers SET min_conviction = NULL;
    CREATE INDEX IF NOT EXISTS idx_subscribers_ref ON bot_subscribers(ref_code);
    CREATE TABLE IF NOT EXISTS referrals(
      chat_id TEXT PRIMARY KEY, referrer TEXT NOT NULL, at INTEGER NOT NULL, rewarded INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer, at);
    """,
}

STATUSES = ("candidate", "tracking", "active", "watch", "dropped", "needs_review")


def now() -> int:
    return int(time.time())


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else settings.db_path
    # WAL lets a long read run beside a write; the busy timeout covers the moment two writers
    # meet, which happens whenever a collection loop and a one-off command overlap.
    #
    # check_same_thread is off because the API hands each request its own connection, and a web
    # framework is free to run the handler on one worker thread and the teardown that closes it on
    # another. sqlite's guard sees that as illegal and raises mid-request. Turning it off is safe
    # here precisely because no connection is ever shared between two units of work — every caller
    # opens its own and closes it.
    conn = sqlite3.connect(p, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in sorted(MIGRATIONS):
        if v > version:
            conn.executescript(MIGRATIONS[v])
            conn.execute(f"PRAGMA user_version={v}")
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _placeholders(n: int) -> str:
    return ",".join(["?"] * n)


# ---------- traders ----------

def upsert_trader(conn: sqlite3.Connection, address: str, **fields: Any) -> bool:
    """Insert or update a trader. Returns True if the row was newly created.
    Status of an existing row is never touched here; use set_status."""
    ts = now()
    exists = conn.execute("SELECT 1 FROM traders WHERE address=?", (address,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["address", "first_seen_at", "last_seen_at", *clean]
        vals = [address, ts, ts, *clean.values()]
        conn.execute(f"INSERT INTO traders({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        return True
    clean.pop("status", None)
    clean["last_seen_at"] = ts
    sets = ",".join(f"{k}=?" for k in clean)
    conn.execute(f"UPDATE traders SET {sets} WHERE address=?", [*clean.values(), address])
    return False


def noise(conn: sqlite3.Connection) -> set[str]:
    """Every address quarantined as not-a-trader."""
    return {r["address"] for r in conn.execute("SELECT address FROM noise_addresses")}


def set_status(conn: sqlite3.Connection, address: str, status: str) -> None:
    assert status in STATUSES, status
    conn.execute("UPDATE traders SET status=? WHERE address=?", (status, address))


def get_trader(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM traders WHERE address=?", (address,)).fetchone()


def traders_by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM traders WHERE status IN ({_placeholders(len(statuses))}) "
        "ORDER BY score DESC NULLS LAST, last_seen_at DESC",
        statuses,
    ).fetchall()


# ---------- tokens ----------

def upsert_token(conn: sqlite3.Connection, mint: str, **fields: Any) -> bool:
    ts = now()
    exists = conn.execute("SELECT 1 FROM tokens WHERE mint=?", (mint,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["mint", "first_seen_at", *clean]
        vals = [mint, ts, *clean.values()]
        conn.execute(f"INSERT INTO tokens({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        return True
    if clean:
        sets = ",".join(f"{k}=?" for k in clean)
        conn.execute(f"UPDATE tokens SET {sets} WHERE mint=?", [*clean.values(), mint])
    return False


def token_decimals(conn: sqlite3.Connection) -> dict[str, int]:
    """Every token whose base unit we already know, so nobody asks the chain about it twice."""
    return {r["mint"]: r["decimals"] for r in
            conn.execute("SELECT mint, decimals FROM tokens WHERE decimals IS NOT NULL")}


def save_token_decimals(conn: sqlite3.Connection, decimals: dict[str, int]) -> int:
    """Store what a pass learned. Writes only what is new, so this is free when nothing changed."""
    known = token_decimals(conn)
    fresh = {m: d for m, d in decimals.items() if d is not None and known.get(m) != d}
    for mint, value in fresh.items():
        upsert_token(conn, mint, decimals=value)
    return len(fresh)


def save_holdings(conn: sqlite3.Connection, balances: dict[tuple[str, str], float]) -> int:
    """Store a balance read. A zero is a fact worth keeping — it says a position was closed."""
    ts = now()
    conn.executemany(
        "INSERT INTO holdings(address, token, amount, ts) VALUES(?,?,?,?) "
        "ON CONFLICT(address, token) DO UPDATE SET amount=excluded.amount, ts=excluded.ts",
        [(w, m, amount, ts) for (w, m), amount in balances.items()],
    )
    return len(balances)


def holdings_for(conn: sqlite3.Connection, address: str) -> dict[str, tuple[float, int]]:
    """token -> (amount, when it was read) for one wallet."""
    return {r["token"]: (r["amount"], r["ts"]) for r in
            conn.execute("SELECT token, amount, ts FROM holdings WHERE address = ?", (address,))}


# ---------- trades ----------

TRADE_COLS = ("sig", "address", "chain", "mint", "side", "sol_amount", "token_amount", "usd_value",
              "ts", "source", "fill_key", "kind")


def fill_key(t: dict[str, Any]) -> str:
    """What makes two rows the same trade, whichever source reported it.

    Signatures carry a source-specific suffix — a log index from the chain, a sequence number from
    a tape — so the transaction hash is truncated back out of them before comparing. Addresses are
    taken as stored: sources normalize EVM case on the way in, and Solana base58 is case-sensitive.
    """
    return ":".join((str(t.get("sig", ""))[:66], str(t.get("address", "")),
                     str(t.get("mint", "")), str(t.get("side", ""))))


def insert_trade(conn: sqlite3.Connection, **t: Any) -> bool:
    """Idempotent insert keyed by signature. Returns True if a new row was inserted.

    A fill already on file is left alone except for the fields nobody filled in. Sources differ in
    what they can say about the same trade — the tracker knew the dollar value long before it knew
    the token size — so a later pass that does know must be able to complete the row rather than
    bounce off the unique index. It only ever writes over a NULL: what one source already recorded
    is never replaced by another's opinion of it.
    """
    t = {**t, "fill_key": t.get("fill_key") or fill_key(t)}
    vals = [t.get(c) for c in TRADE_COLS]
    cur = conn.execute(
        f"INSERT OR IGNORE INTO trades({','.join(TRADE_COLS)}) VALUES({_placeholders(len(TRADE_COLS))})", vals
    )
    if cur.rowcount == 1:
        return True
    if t.get("token_amount") is not None or t.get("usd_value") is not None:
        conn.execute(
            "UPDATE trades SET token_amount = COALESCE(token_amount, ?), "
            "  usd_value = COALESCE(usd_value, ?), sol_amount = COALESCE(sol_amount, ?) "
            "WHERE fill_key = ? AND (token_amount IS NULL OR usd_value IS NULL)",
            (t.get("token_amount"), t.get("usd_value"), t.get("sol_amount"), t["fill_key"]),
        )
    return False


def last_trade_ts(conn: sqlite3.Connection, address: str) -> int | None:
    row = conn.execute("SELECT MAX(ts) FROM trades WHERE address=?", (address,)).fetchone()
    return row[0]


# ---------- runs / history ----------

def run_start(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs(kind, started_at) VALUES(?,?)", (kind, now()))
    conn.commit()
    return cur.lastrowid


def run_finish(conn: sqlite3.Connection, run_id: int, stats: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats_json=?, error=? WHERE id=?",
        (now(), json.dumps(stats or {}), error, run_id),
    )
    conn.commit()


def upsert_thesis(conn: sqlite3.Connection, **t: Any) -> bool:
    """Store what a trader wrote about a position. Returns True the first time we see it.

    A thesis can be edited, so the text is overwritten — but `first_seen_at` is not. When someone
    said a thing is most of what it is worth: a call written before the token moved is evidence,
    the same words added afterwards are commentary, and only the timestamp separates them.
    """
    text = (t.get("text") or "").strip()
    if not text or not t.get("trade_id"):
        return False
    row = conn.execute("SELECT first_seen_at FROM theses WHERE trade_id=?", (t["trade_id"],)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO theses(trade_id, user_id, mint, text, pnl_usd, cost_usd, is_dev,"
        "  first_seen_at, seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (t["trade_id"], t.get("user_id"), t.get("mint"), text, t.get("pnl_usd"), t.get("cost_usd"),
         int(bool(t.get("is_dev"))), (row["first_seen_at"] if row else None) or now(), now()),
    )
    return row is None


def theses_for_token(conn: sqlite3.Connection, mint: str, limit: int = 12) -> list[dict]:
    """What the tracked wallets said about this token, best-scored voice first.

    Only traders we have a verdict on: an unscored handle writing "wagmi" is noise, and the whole
    product is the claim that whose money it is decides whether the words are worth reading.
    """
    rows = conn.execute(
        "SELECT th.text, th.pnl_usd, th.cost_usd, th.is_dev, th.first_seen_at, "
        "       u.handle, t.address, t.score, t.status "
        "FROM theses th "
        "JOIN fomo_users u ON u.user_id = th.user_id "
        "LEFT JOIN traders t ON t.address = u.onchain_address "
        "WHERE th.mint = ? AND t.score IS NOT NULL "
        "ORDER BY t.score DESC, th.first_seen_at DESC LIMIT ?", (mint, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mints_wanting_theses(conn: sqlite3.Connection, limit: int, window_h: int,
                         max_age_s: int) -> list[dict]:
    """Which tokens to ask fomo about next, best first.

    The collector can only ask about a handful of names per pass, so the ranking is the same one
    the product uses everywhere else: whose money is in it, not how many wallets are. Tokens nobody
    has ever been asked about come first, then the ones with the most conviction behind them.
    """
    cutoff = now() - window_h * 3600
    stale = now() - max_age_s
    rows = conn.execute(
        "SELECT tk.mint, tk.chain, COALESCE(tk.symbol, '') symbol, "
        "       COUNT(DISTINCT t.address) buyers, "
        "       SUM((t.score / 100.0) * (t.score / 100.0)) conviction "
        "FROM trades tr "
        "JOIN traders t ON t.address = tr.address "
        "JOIN tokens tk ON tk.mint = tr.mint "
        "WHERE tr.side = 'buy' AND tr.ts >= ? AND t.score IS NOT NULL "
        "  AND (tk.thesis_at IS NULL OR tk.thesis_at < ?) "
        "GROUP BY tk.mint "
        "ORDER BY (tk.thesis_at IS NULL) DESC, conviction DESC LIMIT ?",
        (cutoff, stale, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_fomo_position(conn: sqlite3.Connection, **p: Any) -> bool:
    """Positions change as prices move, so this overwrites rather than ignoring duplicates."""
    cols = ("trade_id", "user_id", "chain", "token", "symbol", "opened_at", "closed_at", "amount",
            "avg_entry", "avg_exit", "realized_pnl", "unrealized_pnl", "cost_basis",
            "current_price", "liquidity", "seen_at")
    # fomo's `pnl` can exceed a holding's whole value because it also counts profit already taken
    # out. Cost basis is then unrecoverable, and a negative one would poison every multiple
    # computed from it, so it is stored as unknown. Enforced here, where no caller can skip it.
    p = dict(p)
    for k in ("cost_basis", "avg_entry"):
        if p.get(k) is not None and p[k] <= 0:
            p[k] = None
    vals = [p.get(c) for c in cols[:-1]] + [now()]
    existed = conn.execute("SELECT 1 FROM fomo_positions WHERE trade_id=?", (p.get("trade_id"),)).fetchone()
    conn.execute(
        f"INSERT OR REPLACE INTO fomo_positions({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals
    )
    return existed is None


def insert_fomo_swap(conn: sqlite3.Connection, **s: Any) -> bool:
    cols = ("swap_id", "user_id", "chain", "token", "side", "ts", "usd", "amount")
    cur = conn.execute(
        f"INSERT OR IGNORE INTO fomo_swaps({','.join(cols)}) VALUES({_placeholders(len(cols))})",
        [s.get(c) for c in cols],
    )
    return cur.rowcount == 1


FOMO_PNL_FIELDS = ("pnl_24h", "pnl_7d", "pnl_30d")


def upsert_fomo_user(conn: sqlite3.Connection, user_id: str, **fields: Any) -> bool:
    ts = now()
    exists = conn.execute("SELECT handle FROM fomo_users WHERE user_id=?", (user_id,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["user_id", "first_seen_at", "last_seen_at", *clean]
        vals = [user_id, ts, ts, *clean.values()]
        conn.execute(f"INSERT INTO fomo_users({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        created = True
    else:
        clean["last_seen_at"] = ts
        sets = ",".join(f"{k}=?" for k in clean)
        conn.execute(f"UPDATE fomo_users SET {sets} WHERE user_id=?", [*clean.values(), user_id])
        created = False
    mirror_fomo_pnl(conn, user_id, clean.get("handle") or (exists["handle"] if exists else None),
                    {k: clean[k] for k in FOMO_PNL_FIELDS if k in clean})
    return created


def mirror_fomo_pnl(conn: sqlite3.Connection, user_id: str, handle: str | None, pnl: dict[str, Any]) -> int:
    """The board's PnL, copied onto the wallet(s) that trade for this user.

    `traders.pnl_*` is a mirror of the fomo board, and it used to be written once - at resolve
    time - and then left, so the site's leaderboard and the scoring context would show a trader
    the number the board had the day their wallet was found. Now every board row that lands
    refreshes it. Only the three PnL columns: trades_cnt and volume_usd also come from trenches,
    and two sources taking turns on one column is not a refresh, it is a fight.
    """
    if not pnl:
        return 0
    sets = ",".join(f"{k}=?" for k in pnl)
    cur = conn.execute(
        f"UPDATE traders SET {sets} WHERE fomo_user_id=? OR (? IS NOT NULL AND lower(fomo_handle)=lower(?))",
        [*pnl.values(), user_id, handle, handle])
    return cur.rowcount


def unresolved_fomo_users(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Leaderboard entries whose execution wallet we have not looked up yet, best PnL first."""
    return conn.execute(
        "SELECT * FROM fomo_users WHERE resolved_at IS NULL AND resolve_error IS NULL "
        "ORDER BY COALESCE(pnl_30d, pnl_7d, pnl_24h) DESC NULLS LAST LIMIT ?",
        (limit,),
    ).fetchall()


def add_score_history(conn: sqlite3.Connection, address: str, score: int, status: str, model: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO score_history(address, score, status, model, reason, ts) VALUES(?,?,?,?,?,?)",
        (address, score, status, model, reason, now()),
    )
