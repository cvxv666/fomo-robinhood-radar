"""PRO access: the alerts and the live feeds, paid for in the token and burned.

The bot is the product and the token is the currency. A subscriber asks for a price, gets an
exact number of tokens to send to the burn address, and sends them from whatever wallet they
trade with. Nothing is connected, nothing is signed: the amount itself is the receipt. Every
open quote carries a different amount - a round base plus a small tail - so a transfer of that
exact size to the burn address can only be one chat's. The watcher, which already reads every
block, sees the burn within a tick and the chat is credited before the wallet has closed the
confirmation screen.

The price is in dollars and the token count follows the pool: twenty dollars is twenty dollars
whether the token did ten times or a tenth. A quote holds for half an hour, and the base is
rounded up, so the exact amount is always at least the dollar price at the moment of asking.

What does not match - someone rounded the number, or paid twice the amount for two months in
one go - lands in the table with no chat, and `/claim <tx>` attaches it if it covers the price.
A burn is credited once: the transaction hash is the primary key.

The gate is soft in three ways so it never eats an honest subscriber: a price of zero means no
gate at all; `PRO_GRACE_UNTIL` keeps everyone who was here before the gate on the free side until
that date; `PRO_TRIAL_DAYS` hands a newcomer a few days on /start. Free for everyone, always:
the leaderboard, the token and trader lookups, and the daily digest - the funnel.
"""
from __future__ import annotations

import logging
import math
import secrets
import sqlite3
import time

from .. import db
from ..config import settings
from ..sources.rpc import TRANSFER_TOPIC, RobinhoodRPC, RpcError, parse_transfer, topic_for

log = logging.getLogger(__name__)

DECIMALS = 18
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: it is read off a phone
CLAIM_GRACE_S = 6 * 3600     # an exact amount that arrives this long after its quote expired still counts
NOTICES = ((86400, "1d"), (3 * 86400, "3d"))   # nearest first: the tag is the first horizon the end is inside


# ---------------------------------------------------------------- the gate

def enabled() -> bool:
    return settings.pro_price_usd > 0 and bool(settings.pro_token)


def entitled_row(row, now: int | None = None) -> bool:
    """Whether this subscriber row gets the alerts and the live feeds right now."""
    if not enabled():
        return True
    now = now or db.now()
    paid = row["paid_until"] if "paid_until" in row.keys() else None
    if paid and paid > now:
        return True
    grace = settings.pro_grace_until
    if grace and now < grace and (row["subscribed_at"] or 0) < grace:
        return True
    return False


def entitled(conn: sqlite3.Connection, chat_id, now: int | None = None) -> bool:
    row = conn.execute("SELECT * FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    if row is None:
        return not enabled()
    return entitled_row(row, now)


def paid_until(conn: sqlite3.Connection, chat_id) -> int | None:
    row = conn.execute("SELECT paid_until FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    return row["paid_until"] if row else None


def grant(conn: sqlite3.Connection, chat_id, days: int, now: int | None = None) -> int:
    """Extend a chat's PRO by `days` from whichever is later, now or its current end. Returns the
    new end. The row is created if the chat never subscribed (a payment before a /start)."""
    now = now or db.now()
    current = paid_until(conn, chat_id) or 0
    end = max(now, current) + days * 86400
    with db.tx(conn):
        conn.execute(
            "INSERT INTO bot_subscribers(chat_id, subscribed_at, active, paid_until, pro_notice) VALUES(?,?,1,?,NULL) "
            "ON CONFLICT(chat_id) DO UPDATE SET paid_until=excluded.paid_until, pro_notice=NULL, active=1",
            (str(chat_id), now, end))
    return end


# ---------------------------------------------------------------- price and quotes

def price_now(conn: sqlite3.Connection, now: int | None = None) -> float | None:
    """The token's dollar price: the stored mark if it is fresh, else one live lookup."""
    now = now or db.now()
    mint = settings.pro_token
    row = conn.execute("SELECT price_usd, price_at FROM tokens WHERE mint=?", (mint,)).fetchone()
    if row and row["price_usd"] and row["price_at"] and now - row["price_at"] <= settings.pro_price_max_age_s:
        return float(row["price_usd"])
    try:
        from .new_tokens import lookup_tokens
        found, _ = lookup_tokens(settings.dex_chains[0] if settings.dex_chains else "robinhood", [mint])
    except Exception as e:  # noqa: BLE001 - the stored mark is the fallback
        log.warning("pro: price lookup failed: %s", e)
        found = []
    for t in found:
        if t.mint.lower() == mint and t.price_usd:
            with db.tx(conn):
                db.upsert_token(conn, mint, chain=t.chain, symbol=t.symbol, price_usd=t.price_usd,
                                price_at=now, decimals=t.decimals, pool_address=t.pool_address,
                                liquidity_usd=t.liquidity_usd, mcap_usd=t.mcap_usd)
            return float(t.price_usd)
    return float(row["price_usd"]) if row and row["price_usd"] else None


def exact_amount(need: float, taken: set[int]) -> int:
    """A whole number of tokens at least `need`, unique among `taken`: a base rounded up to a
    round step, plus a tail. The step is a hundredth of the size, so the rounding is at most a
    couple of percent over, and the tail is the part that names the chat."""
    step = 10 ** max(2, int(math.floor(math.log10(max(need, 1)))) - 2)
    base = int(math.ceil(need / step)) * step
    tails = list(range(1, min(step, 1000)))
    while True:
        free = [t for t in tails if base + t not in taken]
        if free:
            return base + secrets.choice(free)
        base += step   # every tail at this base is out: the next round number up


def open_quote(conn: sqlite3.Connection, chat_id, now: int | None = None) -> sqlite3.Row | None:
    now = now or db.now()
    return conn.execute(
        "SELECT * FROM pro_quotes WHERE chat_id=? AND status='open' AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (str(chat_id), now + 300)).fetchone()   # one with under five minutes left is not worth handing out again


def quote(conn: sqlite3.Connection, chat_id, now: int | None = None) -> dict | None:
    """The exact amount this chat should send, reusing an open quote if there is one. None when
    there is no price to quote from."""
    now = now or db.now()
    existing = open_quote(conn, chat_id, now)
    if existing:
        return dict(existing)
    price = price_now(conn, now)
    if not price or price <= 0:
        return None
    need = settings.pro_price_usd / price
    with db.tx(conn):
        conn.execute("UPDATE pro_quotes SET status='expired' WHERE status='open' AND expires_at <= ?", (now,))
        # an amount stays reserved for a while after its quote expired: the transfer may be in flight
        taken = {r["tokens"] for r in conn.execute(
            "SELECT tokens FROM pro_quotes WHERE status IN ('open', 'expired') AND expires_at > ?", (now - CLAIM_GRACE_S,))}
        tokens = exact_amount(need, taken)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        while conn.execute("SELECT 1 FROM pro_quotes WHERE code=?", (code,)).fetchone():
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        row = {"code": code, "chat_id": str(chat_id), "usd": settings.pro_price_usd, "price": price,
               "tokens": tokens, "tokens_min": need * (1 - settings.pro_price_tolerance),
               "created_at": now, "expires_at": now + settings.pro_quote_ttl_min * 60, "status": "open", "tx": None}
        conn.execute(
            "INSERT INTO pro_quotes(code, chat_id, usd, price, tokens, tokens_min, created_at, expires_at, status) "
            "VALUES(:code, :chat_id, :usd, :price, :tokens, :tokens_min, :created_at, :expires_at, :status)", row)
    return row


def quote_by_code(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute("SELECT * FROM pro_quotes WHERE code=?", (code.upper().strip(),)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["paid_until"] = paid_until(conn, d["chat_id"])
    return d


# ---------------------------------------------------------------- payments

def tokens_of(raw: int) -> float:
    return raw / 10 ** DECIMALS


def burns(rpc: RobinhoodRPC, from_block: int, to_block: int) -> list[dict]:
    """Every transfer of the token to the burn address in the range: one eth_getLogs."""
    raw = rpc.call("eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "address": settings.pro_token, "topics": [TRANSFER_TOPIC, None, topic_for(settings.pro_burn_address)],
    }])
    return [t for t in (parse_transfer(e) for e in raw) if t]


def record(conn: sqlite3.Connection, t: dict, ts: int, now: int | None = None) -> dict | None:
    """One burn transfer into the ledger. Returns the payment as credited (with `chat_id` when a
    quote matched it), or None when this transaction was already on the books."""
    now = now or db.now()
    tx = t["tx"].lower()
    if conn.execute("SELECT 1 FROM pro_payments WHERE tx=?", (tx,)).fetchone():
        return None
    tokens = tokens_of(t["raw"])
    whole = int(round(tokens))
    match = None
    if abs(tokens - whole) < 1e-6:
        match = conn.execute(
            "SELECT * FROM pro_quotes WHERE tokens=? AND status IN ('open', 'expired') AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 1", (whole, ts - CLAIM_GRACE_S)).fetchone()
    payment = {"tx": tx, "chat_id": match["chat_id"] if match else None, "frm": t["frm"], "tokens": tokens,
               "usd": match["usd"] if match else None, "ts": ts, "days": settings.pro_days if match else None,
               "source": "watch", "block": t.get("block"), "code": match["code"] if match else None}
    with db.tx(conn):
        conn.execute(
            "INSERT INTO pro_payments(tx, chat_id, frm, tokens, usd, ts, days, source, block, code) "
            "VALUES(:tx, :chat_id, :frm, :tokens, :usd, :ts, :days, :source, :block, :code)", payment)
        if match:
            conn.execute("UPDATE pro_quotes SET status='paid', tx=? WHERE code=?", (tx, match["code"]))
    if match:
        payment["paid_until"] = grant(conn, match["chat_id"], settings.pro_days, now)
    return payment


def settle(conn: sqlite3.Connection, rpc: RobinhoodRPC, from_block: int, to_block: int,
           now: int | None = None) -> list[dict]:
    """Read the burns in the range and credit what matches. Returns the payments credited to a
    chat - the caller tells them. Never raises: a failed read is retried by the next tick, and
    `/claim` catches anything a gap swallowed."""
    if not enabled():
        return []
    now = now or db.now()
    try:
        found = burns(rpc, from_block, to_block)
    except (RpcError, Exception) as e:  # noqa: BLE001
        log.warning("pro: could not read burns %d-%d: %s", from_block, to_block, e)
        return []
    credited = []
    for t in found:
        p = record(conn, t, now, now)
        if p and p["chat_id"]:
            credited.append(p)
            log.info("pro: %s paid %s tokens, chat %s until %s", p["frm"], p["tokens"], p["chat_id"], p["paid_until"])
        elif p:
            log.info("pro: unclaimed burn of %s tokens from %s (%s)", p["tokens"], p["frm"], p["tx"])
    return credited


def claim(conn: sqlite3.Connection, chat_id, tx: str, rpc: RobinhoodRPC | None = None,
          now: int | None = None) -> tuple[bool, str]:
    """Attach a burn to this chat by its transaction hash. (ok, why)."""
    now = now or db.now()
    tx = (tx or "").strip().lower()
    if not (tx.startswith("0x") and len(tx) == 66):
        return False, "That is not a transaction hash. It is 0x followed by 64 hex characters, from the wallet or the explorer."
    row = conn.execute("SELECT * FROM pro_payments WHERE tx=?", (tx,)).fetchone()
    if row is None:
        # not seen by the watcher: ask the chain for the receipt
        try:
            rpc = rpc or RobinhoodRPC()
            receipt = rpc.call("eth_getTransactionReceipt", [tx])
        except Exception as e:  # noqa: BLE001
            return False, f"Could not read that transaction right now ({type(e).__name__}). Try again in a minute."
        if not receipt:
            return False, "No such transaction on Robinhood Chain yet. If it was just sent, give it a minute."
        burn = None
        for e in receipt.get("logs") or []:
            t = parse_transfer(e)
            if t and t["token"] == settings.pro_token and t["to"] == settings.pro_burn_address.lower():
                burn = t
                break
        if burn is None:
            return False, f"That transaction does not send ${settings.pro_token_symbol} to the burn address."
        ts = now
        try:
            ts = rpc.block_timestamp(int(receipt["blockNumber"], 16))
        except Exception:  # noqa: BLE001
            pass
        p = record(conn, burn, ts, now)
        if p and p["chat_id"]:
            if p["chat_id"] == str(chat_id):
                return True, f"Matched your quote. PRO until {_date(p['paid_until'])}."
            return False, "That burn matched somebody else's quote."
        row = conn.execute("SELECT * FROM pro_payments WHERE tx=?", (tx,)).fetchone()
    if row["chat_id"]:
        if row["chat_id"] == str(chat_id):
            return False, f"Already credited to this chat: PRO until {_date(paid_until(conn, chat_id))}."
        return False, "That transaction is already credited to another chat."
    # unclaimed: does it cover the price? against the chat's latest quote, else against the price now
    q = conn.execute("SELECT * FROM pro_quotes WHERE chat_id=? ORDER BY created_at DESC LIMIT 1", (str(chat_id),)).fetchone()
    need_min = q["tokens_min"] if q and now - q["created_at"] < 86400 else None
    if need_min is None:
        price = price_now(conn, now)
        if not price:
            return False, "No price to check it against right now. Try again in a minute."
        need_min = settings.pro_price_usd / price * (1 - settings.pro_price_tolerance)
    months = int(row["tokens"] // need_min)
    if months < 1:
        return False, (f"That burn is {row['tokens']:,.0f} ${settings.pro_token_symbol}, under the "
                       f"{need_min:,.0f} a month costs. Send the difference to the same address and /claim that one too.")
    months = min(months, 12)
    days = months * settings.pro_days
    with db.tx(conn):
        conn.execute("UPDATE pro_payments SET chat_id=?, days=?, usd=?, source='claim' WHERE tx=?",
                     (str(chat_id), days, settings.pro_price_usd * months, tx))
        if q and q["status"] == "open":
            conn.execute("UPDATE pro_quotes SET status='paid', tx=? WHERE code=?", (tx, q["code"]))
    end = grant(conn, chat_id, days, now)
    return True, f"Credited: {months} month{'s' if months > 1 else ''}. PRO until {_date(end)}."


# ---------------------------------------------------------------- reminders

def reminders(conn: sqlite3.Connection, now: int | None = None) -> list[tuple[str, str]]:
    """(chat_id, notice) for every paid chat whose end is near or just passed, each notice once
    per paid period. The notice tag is stamped with the end it was about, so a renewal - which
    moves the end - starts the sequence over."""
    if not enabled():
        return []
    now = now or db.now()
    out = []
    rows = conn.execute("SELECT chat_id, paid_until, pro_notice FROM bot_subscribers "
                        "WHERE active=1 AND paid_until IS NOT NULL AND paid_until > ?", (now - 86400,)).fetchall()
    for r in rows:
        left = r["paid_until"] - now
        due_tag = None
        if left <= 0:
            due_tag = "expired"
        else:
            for horizon, tag in NOTICES:
                if left <= horizon:
                    due_tag = tag
                    break
        if due_tag is None:
            continue
        stamp = f"{due_tag}:{r['paid_until']}"
        if r["pro_notice"] == stamp:
            continue
        # a later notice supersedes an earlier one that was never sent (bot was down): send only the latest
        with db.tx(conn):
            conn.execute("UPDATE bot_subscribers SET pro_notice=? WHERE chat_id=?", (stamp, r["chat_id"]))
        out.append((r["chat_id"], due_tag))
    return out


def _date(ts: int | None) -> str:
    return time.strftime("%d %b %Y, %H:%M UTC", time.gmtime(ts)) if ts else "—"
