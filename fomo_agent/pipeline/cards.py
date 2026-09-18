"""The call-card: one image when an alert reaches twice its entry inside the hour.

The follow-up already says it in three lines; the card says it in one picture that travels -
the multiple large, the token, when the call went out and when the price got there, the read
since. Made only for a ×2 or better, so a card means something, and made from the same numbers
the chats were told. Posted to X on its own and sent to the chats that got the alert, with the
follow-up text as its caption.
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import time

from .. import db
from ..config import settings

log = logging.getLogger(__name__)

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "assets" / "boards" / "card.html"


def wanted(m: dict) -> bool:
    return bool(m.get("best")) and m["best"] >= settings.card_min_x


def data(row, m: dict, now: int | None = None) -> dict:
    now = now or db.now()
    return {
        "sym": row["sym"], "kind": row["kind"], "mint": row["mint"],
        "best": m["best"], "peak_min": m.get("peak_min"), "now": m.get("now"), "vol": m.get("vol"),
        "called_at": time.strftime("%H:%M", time.gmtime(row["ts"])), "date": time.strftime("%d %b %Y", time.gmtime(row["ts"])).lstrip("0").upper(),
        "wallets": row["wallets"], "conviction": row["conviction"], "heat": row["heat"],
        "followup_min": settings.telegram_followup_min,
        "handle": settings.x_handle, "site": (settings.public_site_url or "").replace("https://", ""),
    }


def html(d: dict) -> str:
    return TEMPLATE.read_text(encoding="utf-8").replace("/*CARD*/", "window.CARD = " + json.dumps(d, ensure_ascii=False) + ";")


def render(d: dict, out_dir: pathlib.Path | None = None) -> pathlib.Path:
    from .board import render as _render

    out_dir = out_dir or (pathlib.Path(settings.board_dir) / "cards" if settings.board_dir else pathlib.Path("boards") / "cards")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{d['sym']}-{d['mint'][:8]}-{int(time.time())}"
    html_path, png_path = out_dir / f"{stem}.html", out_dir / f"{stem}.png"
    html_path.write_text(html(d), encoding="utf-8")
    _render(html_path, png_path, width=1200, height=675)
    return png_path


def post_text(d: dict) -> str:
    x = lambda v: f"×{v:.2f}"  # noqa: E731
    when = f" {d['peak_min']} min later" if d.get("peak_min") is not None else " inside the hour"
    text = f"Called ${d['sym']} at ×1.00 · {x(d['best'])}{when}"
    if d.get("now") is not None:
        text += f" · now {x(d['now'])}"
    text += f"\n\n{d['wallets']} trusted wallets were in when the bot called it." if d.get("wallets") else ""
    text += "\n\nLive calls: bot in bio · not financial advice"
    return text[:280]


def make(conn: sqlite3.Connection, row, m: dict, now: int | None = None) -> pathlib.Path | None:
    """Render the card for a measured push, or None when it is not a ×2 or the render fails."""
    if not wanted(m):
        return None
    try:
        return render(data(row, m, now))
    except Exception as e:  # noqa: BLE001 - a card that will not render is not a follow-up that will not send
        log.warning("card for %s failed: %s", row["sym"], e)
        return None


def post(conn: sqlite3.Connection, png: pathlib.Path, d: dict, client=None, now: int | None = None) -> str | None:
    """The card on X, on its own. Returns the post id."""
    from . import xpost

    if not xpost.enabled():
        return None
    now = now or db.now()
    if xpost.posted_today(conn, now) >= settings.x_max_per_day:
        return None
    try:
        if client is None:
            from ..sources.x import XClient
            client = XClient()
        mid = client.upload(png)
        tid = client.post(post_text(d), media_ids=[mid])
    except Exception as e:  # noqa: BLE001
        log.warning("card post for %s failed: %s", d["sym"], e)
        return None
    with db.tx(conn):
        conn.execute("INSERT INTO x_posts(push_id, kind, mint, due_ts, text, posted_at, tweet_id) VALUES(NULL, 'card', ?, ?, ?, ?, ?)",
                     (d["mint"], now, post_text(d), now, tid))
    return tid
