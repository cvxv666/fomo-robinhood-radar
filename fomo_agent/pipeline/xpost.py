"""The radar's posts on X: every push, a few minutes after Telegram, and the hour-later look
back as a reply under it.

Telegram is where the alert is worth something - the PRO chats hear a burst within seconds of
the fill. X hears it `X_POST_DELAY_S` later, which is the honest order: the people who pay come
first, and the post is still a record made in public before the outcome was known. The reply
an hour on carries the same three facts the chats get - peak and when, now, traded - so the
account's timeline is its own scorecard, with the misses on it.

No URL in any post: a post that carries one costs thirteen times as much, and the link lives in
the bio. A token's address is not a URL. Twenty posts a day at most, whatever the chain does.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import db
from ..config import settings

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.x_enabled and settings.x_client_id)


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def fmt_post(kind: str, t: dict) -> str:
    """The push, as a post. Plain text, the address in full, no link anywhere."""
    sym = f"${t.get('sym') or t['mint'][:8]}"
    who = t.get("who") or []
    if kind == "burst":
        mins = max(1, round(((t.get("last_ts") or 0) - (t.get("first_ts") or 0)) / 60)) if t.get("first_ts") else None
        head = f"▲ {sym} · burst on Robinhood Chain"
        line = f"{t.get('wallets') or len(who)} trusted wallets in {mins} min" if mins else f"{t.get('wallets') or len(who)} trusted wallets"
        line += f" · conviction {t['conviction']:.1f}" if t.get("conviction") else ""
    else:
        lead = t.get("lead_minutes")
        head = f"◆ {sym} · launch on Robinhood Chain"
        line = f"{t.get('buyers') or len(who)} trusted wallets"
        if lead is not None:
            line += f", first one {('the same minute' if lead < 1 else f'{lead:.0f} min')} after the pool opened"
        line += f" · heat {t['heat']:.2f}" if t.get("heat") else ""
    usd = t.get("usd")
    if usd:
        line += f" · ${usd / 1000:.1f}K in" if usd >= 1000 else f" · ${usd:.0f} in"
    delay = settings.x_post_delay_s // 60
    lines = [head, line, t["mint"], "",
             f"FOMO Robinhood Radar · posted {delay} min after the bot's alert · the read since is in the thread · not financial advice"]
    return "\n".join(lines)


def fmt_reply(row, m: dict) -> str:
    """The hour-later reply: the same three facts the chats got."""
    if m.get("best") is None:
        return f"{settings.telegram_followup_min} min after the push: no trade in the pool since."
    x = lambda v: "—" if v is None else f"×{v:.2f}"  # noqa: E731
    peak = x(m["best"]) + (f" at +{m['peak_min']} min" if m.get("peak_min") is not None else "")
    vol = m.get("vol")
    traded = f"${vol / 1000:.0f}K" if vol and vol >= 1000 else (f"${vol:.0f}" if vol else "—")
    return f"{settings.telegram_followup_min} min after the push: peak {peak} · now {x(m.get('now'))} · traded {traded}"


def enqueue(conn: sqlite3.Connection, push_id: int, kind: str, item: dict, now: int | None = None) -> None:
    """Written at push time; sent by the bot's timer once the delay has passed."""
    if not enabled():
        return
    now = now or db.now()
    with db.tx(conn):
        conn.execute("INSERT INTO x_posts(push_id, kind, mint, due_ts, text) VALUES(?,?,?,?,?)",
                     (push_id, kind, item["mint"], now + settings.x_post_delay_s, fmt_post(kind, item)))


def posted_today(conn: sqlite3.Connection, now: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM x_posts WHERE posted_at >= ?", (now - 86400,)).fetchone()[0]


def send_due(conn: sqlite3.Connection, client=None, now: int | None = None) -> int:
    """Post whatever is due. One at a time, oldest first, under the daily cap; a post that
    fails is left with its error and tried again next minute, three times."""
    if not enabled():
        return 0
    now = now or db.now()
    rows = conn.execute(
        "SELECT * FROM x_posts WHERE posted_at IS NULL AND due_ts <= ? AND COALESCE(attempts, 0) < 3 ORDER BY due_ts",
        (now,)).fetchall()
    if not rows:
        return 0
    if client is None:
        from ..sources.x import XClient
        client = XClient()
    sent = 0
    for r in rows:
        if posted_today(conn, now) >= settings.x_max_per_day:
            log.info("x: daily cap of %d reached, %s waits", settings.x_max_per_day, r["mint"][:10])
            break
        try:
            tid = client.post(r["text"])
            with db.tx(conn):
                conn.execute("UPDATE x_posts SET posted_at=?, tweet_id=?, error=NULL WHERE id=?", (now, tid, r["id"]))
            sent += 1
            log.info("x: posted %s %s as %s", r["kind"], r["mint"][:10], tid)
            # with an hour's delay the hour-later read is usually already on the books: it goes
            # up under the post in the same minute rather than waiting for a measurement that
            # has already happened
            p = conn.execute("SELECT followup_at, best, peak_min, now_x, vol_usd FROM pushes WHERE id=?", (r["push_id"],)).fetchone() if r["push_id"] else None
            if p and p["followup_at"]:
                reply_followup(conn, r["push_id"], {"best": p["best"], "peak_min": p["peak_min"], "now": p["now_x"], "vol": p["vol_usd"]}, client=client, now=now)
        except Exception as e:  # noqa: BLE001 - X being down is not the bot being down
            with db.tx(conn):
                conn.execute("UPDATE x_posts SET attempts=COALESCE(attempts,0)+1, error=? WHERE id=?", (str(e)[:200], r["id"]))
            log.warning("x: post for %s failed: %s", r["mint"][:10], e)
        time.sleep(0.5)
    return sent


def fmt_void(note: str, minutes: int) -> str:
    fact = note[0].upper() + note[1:] + ("" if note.endswith(".") else ".")
    return f"⚠ {minutes} min after the push: unsellable. {fact} Off every feed; the record counts it as a honeypot."


def void(conn: sqlite3.Connection, mint: str, note: str, now: int | None = None, client=None) -> dict:
    """The push taken back where it was posted. A post already up gets the fact as a reply in
    its thread, the same place the hour-later read goes; a post still in the queue never goes
    out - an hour-old "launch" on a token nobody can sell is not a post, it is a trap with our
    name on it. Returns {"replied": n, "cancelled": n}."""
    out = {"replied": 0, "cancelled": 0}
    if not enabled():
        return out
    now = now or db.now()
    rows = conn.execute("SELECT * FROM x_posts WHERE mint = ? AND due_ts >= ? ORDER BY due_ts",
                        (mint, now - settings.telegram_realert_hours * 3600 - settings.x_post_delay_s)).fetchall()
    for r in rows:
        if r["posted_at"] is None:
            with db.tx(conn):
                conn.execute("UPDATE x_posts SET attempts = 3, error = ? WHERE id = ? AND posted_at IS NULL",
                             (f"voided before posting: {note}"[:200], r["id"]))
            out["cancelled"] += 1
            continue
        if not r["tweet_id"]:
            continue
        pushed = conn.execute("SELECT ts FROM pushes WHERE id = ?", (r["push_id"],)).fetchone() if r["push_id"] else None
        minutes = (now - (pushed["ts"] if pushed else r["due_ts"] - settings.x_post_delay_s)) // 60
        if client is None:
            from ..sources.x import XClient
            client = XClient()
        try:
            client.post(fmt_void(note, max(0, minutes)), reply_to=r["tweet_id"])
            out["replied"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("x: void reply for %s failed: %s", mint[:10], e)
    if out["replied"] or out["cancelled"]:
        log.info("x: void %s: %s", mint[:10], out)
    return out


def reply_followup(conn: sqlite3.Connection, push_id: int, m: dict, client=None, now: int | None = None) -> bool:
    """The look-back, as a reply under the post the push made. Nothing if the push never made it to X."""
    if not enabled():
        return False
    row = conn.execute("SELECT * FROM x_posts WHERE push_id=? AND tweet_id IS NOT NULL AND reply_id IS NULL", (push_id,)).fetchone()
    if row is None:
        return False
    now = now or db.now()
    if client is None:
        from ..sources.x import XClient
        client = XClient()
    try:
        rid = client.post(fmt_reply(row, m), reply_to=row["tweet_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("x: reply for %s failed: %s", row["mint"][:10], e)
        return False
    with db.tx(conn):
        conn.execute("UPDATE x_posts SET reply_id=?, replied_at=? WHERE id=?", (rid, now, row["id"]))
    return True
