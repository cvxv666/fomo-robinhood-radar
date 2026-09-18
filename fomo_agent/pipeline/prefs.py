"""What each chat wants to hear: which alerts, how sure, and when to keep quiet.

Three knobs on the subscriber row. `kinds` is all, bursts or launches; `min_conviction` is the
bar an alert has to clear for this chat (the default is the bot's own); `quiet_from`/`quiet_to`
are UTC hours between which nothing is pushed, wrapping midnight. A chat that turned an alert
off did not unsubscribe, and the feeds still answer when asked.
"""
from __future__ import annotations

import sqlite3

from .. import db
from ..config import settings

KINDS = ("all", "bursts", "launches")


def wants(sub, kind: str, conviction: float | None, now: int) -> bool:
    """Should this alert go to this chat. `sub` is a bot_subscribers row or dict."""
    kinds = (sub["kinds"] if "kinds" in sub.keys() else None) or "all"
    if kinds == "bursts" and kind != "burst":
        return False
    if kinds == "launches" and kind != "launch":
        return False
    bar = sub["min_conviction"] if "min_conviction" in sub.keys() else None
    if bar and conviction is not None and conviction < bar:
        return False
    return not quiet_now(sub, now)


def quiet_now(sub, now: int) -> bool:
    q0 = sub["quiet_from"] if "quiet_from" in sub.keys() else None
    q1 = sub["quiet_to"] if "quiet_to" in sub.keys() else None
    if q0 is None or q1 is None or q0 == q1:
        return False
    h = (now // 3600) % 24
    return (q0 <= h < q1) if q0 < q1 else (h >= q0 or h < q1)


def set_kinds(conn: sqlite3.Connection, chat_id, kinds: str) -> tuple[bool, str]:
    k = kinds.strip().lower()
    k = {"burst": "bursts", "launch": "launches", "both": "all", "everything": "all"}.get(k, k)
    if k not in KINDS:
        return False, "/alerts all, /alerts bursts or /alerts launches. /stop turns everything off."
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET kinds=? WHERE chat_id=?", (k, str(chat_id)))
    return True, {"all": "Bursts and launches, both.", "bursts": "Bursts only.", "launches": "Launches only."}[k]


def set_min_conviction(conn: sqlite3.Connection, chat_id, value: str) -> tuple[bool, str]:
    try:
        v = float(value.replace(",", "."))
    except ValueError:
        return False, f"/minconv <number>, the bot's own bar is {settings.telegram_min_conviction:g}. Four wallets scoring 80 are about 2.6."
    if not 0 <= v <= 30:
        return False, "Between 0 and 30."
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET min_conviction=? WHERE chat_id=?", (v, str(chat_id)))
    return True, f"Alerts under conviction {v:g} are not sent to this chat." if v else "Every alert, whatever its conviction."


def set_quiet(conn: sqlite3.Connection, chat_id, spec: str) -> tuple[bool, str]:
    s = spec.strip().lower()
    if s in ("off", "none", "0"):
        with db.tx(conn):
            conn.execute("UPDATE bot_subscribers SET quiet_from=NULL, quiet_to=NULL WHERE chat_id=?", (str(chat_id),))
        return True, "No quiet hours."
    try:
        a, b = s.replace("–", "-").split("-")
        q0, q1 = int(a), int(b)
        assert 0 <= q0 <= 24 and 0 <= q1 <= 24
    except (ValueError, AssertionError):
        return False, "/quiet 23-07 (hours, UTC) or /quiet off."
    q0, q1 = q0 % 24, q1 % 24
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET quiet_from=?, quiet_to=? WHERE chat_id=?", (q0, q1, str(chat_id)))
    return True, f"Quiet from {q0:02d}:00 to {q1:02d}:00 UTC: nothing is pushed then. The feeds still answer."


def describe(conn: sqlite3.Connection, chat_id) -> str:
    sub = conn.execute("SELECT * FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    if sub is None:
        return "This chat is not subscribed. /start"
    kinds = sub["kinds"] or "all"
    bar = sub["min_conviction"]
    quiet = f"{sub['quiet_from']:02d}:00-{sub['quiet_to']:02d}:00 UTC" if sub["quiet_from"] is not None and sub["quiet_to"] is not None else "none"
    lines = ["<b>THIS CHAT</b>", "",
             f"alerts       {'off' if not sub['active'] else {'all': 'bursts + launches', 'bursts': 'bursts only', 'launches': 'launches only'}[kinds]}",
             f"conviction   {bar:g} and up" if bar else "conviction   any",
             f"quiet hours  {quiet}",
             "", "/alerts all|bursts|launches · /minconv 4.5 · /quiet 23-07 · /stop"]
    return "\n".join(lines)
