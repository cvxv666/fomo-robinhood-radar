"""The day, as one board, posted to X every morning without anybody drawing it.

The 17 Sep board was made by hand from the ledger and posted by hand. This is the same board from
the same numbers - the alerts of the window with their hour peak and where they sit now, the tape
underneath - laid out by `assets/boards/day.html`, rendered by a headless browser, and posted with
a text that fits in 280 characters. Every figure on it is the record's: nothing is recomputed to
look better, the seeded and the dead are on it with those words.

`data` is the only function with an opinion; the rest is plumbing. A window of 168 hours is the
week's board, same template, other title.
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import time

from .. import db
from ..config import settings
from . import record

log = logging.getLogger(__name__)

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "assets" / "boards" / "day.html"
MAX_ROWS = 8


def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K" if v < 1e5 else f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _n(v: int | float | None) -> str:
    return "—" if v is None else f"{int(v):,}"


def data(conn: sqlite3.Connection, hours: int = 24, now: int | None = None) -> dict:
    now = now or db.now()
    since = now - hours * 3600
    # one row per token: the first alert on it carries the entry the chats were given first
    rows_by_mint: dict[str, dict] = {}
    for r in sorted(record.rows(conn, days=hours // 24 + 2, now=now), key=lambda r: r["ts"]):
        if r["ts"] < since:
            continue
        cur = rows_by_mint.get(r["mint"])
        if cur is None:
            rows_by_mint[r["mint"]] = {**r, "kinds": [r["kind"]]}
        elif r["kind"] not in cur["kinds"]:
            cur["kinds"].append(r["kind"])
    alerts = list(rows_by_mint.values())
    judged = [a for a in alerts if a["best"] is not None and a["verdict"] not in ("open", "unmeasured", "seeded", "honeypot", "dead")]
    above = [a for a in judged if a["best"] > 1.0]
    two = [a for a in judged if a["best"] >= 2.0]
    bests = sorted(a["best"] for a in judged)
    best = max(judged, key=lambda a: a["best"]) if judged else None
    fast = [a for a in judged if a["peak_min"] is not None and a["peak_min"] <= 15]
    with_now = [a for a in judged if a["now"] is not None]
    under = [a for a in with_now if a["now"] < 1.0]

    tape = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT address) wallets, COUNT(DISTINCT mint) mints, SUM(usd_value) usd FROM trades "
        "WHERE ts >= ? AND ts <= ? AND COALESCE(kind, 'trade') = 'trade'", (since, now)).fetchone()
    new_tokens = conn.execute("SELECT COUNT(*) FROM tokens WHERE first_seen_at >= ? AND first_seen_at <= ?", (since, now)).fetchone()[0]

    day = hours <= 24
    end = time.strftime("%H:%M", time.gmtime(now))
    date = time.strftime("%d %b %Y", time.gmtime(now)).lstrip("0").upper()
    n, of = len(above), len(judged)
    if of:
        hero_label = ["ALERTS TRADED", "ABOVE THE CALL"]
        sub1 = f"<b>{len(two)}</b> REACHED ×2  ·  MEDIAN PEAK <b>×{bests[len(bests) // 2]:.2f}</b>" + (f"  ·  BEST <b>×{best['best']:.2f}</b>" if best else "")
        sub2 = (f"<b>{len(fast)} OF {of}</b> PEAKED INSIDE 15 MIN" if of else "") + \
               (f"  ·  <i>{len(under)} OF {len(with_now)}</i> UNDER ENTRY NOW" if with_now else "")
    else:
        hero_label = ["ALERTS", "A QUIET " + ("DAY" if day else "WEEK")]
        sub1 = "NOTHING THE COHORT DID CLEARED THE BAR"
        sub2 = "THE TAPE STILL RAN, BELOW"
    skipped = [a for a in alerts if a["verdict"] in ("seeded", "dead", "honeypot")]
    if skipped:
        sub2 += f"  ·  <i>{len(skipped)}</i> SEEDED OR DEAD, LISTED"

    # the rows on the board: by peak, the best eight; the rest are counted
    shown = sorted(alerts, key=lambda a: -(a["best"] or 0))[:MAX_ROWS]
    shown.sort(key=lambda a: a["ts"])
    rows = []
    for a in shown:
        note = "SEEDED" if a["verdict"] == "seeded" else "HONEYPOT" if a["verdict"] == "honeypot" else "DEAD POOL" if a["verdict"] == "dead" else \
               ("OPEN" if a["verdict"] == "open" else "")
        # what the cohort had put in by the time of the call: trusted buys in the half hour before
        usd_in = conn.execute(
            "SELECT SUM(tr.usd_value) FROM trades tr JOIN traders t ON t.address = tr.address WHERE tr.mint = ? AND tr.side = 'buy' "
            "AND tr.ts BETWEEN ? AND ? AND t.score >= 60 AND COALESCE(tr.kind, 'trade') = 'trade'", (a["mint"], a["ts"] - 1800, a["ts"] + 60)).fetchone()[0]
        rows.append({
            "sym": a["sym"], "at": time.strftime("%H:%M", time.gmtime(a["ts"])), "min": int((a["ts"] - since) / 60),
            "kind": " · ".join(k.upper() for k in sorted(a["kinds"], key=lambda k: k != "launch")),
            "peak": a["best"], "peakMin": a["peak_min"], "now": a["now"], "buyers": a["wallets"],
            "usd": _usd(usd_in) if usd_in else None, "chats": a["chats"], "note": note,
        })
    ticks = []
    step_min = 180 if day else 1440
    first_tick = ((since // 3600) + 1) * 3600
    t = first_tick
    while t < now:
        m = int((t - since) / 60)
        if (t // 60) % step_min == 0 or (not day and (t % 86400) == 0):
            ticks.append({"min": m, "label": time.strftime("%H:%M" if day else "%d %b", time.gmtime(t))})
        t += 3600
    return {
        "hours": hours, "now": now, "since": since,
        "title": date, "subtitle": "THE DAY" if day else "THE WEEK",
        "window": f"{hours} HOURS TO {end} UTC" if day else f"7 DAYS TO {time.strftime('%d %b %H:%M', time.gmtime(now)).lstrip('0').upper()} UTC",
        "hero": {"n": n, "of": of, "label": hero_label, "sub1": sub1, "sub2": sub2, "quiet": not of},
        "timeline": {"minutes": hours * 60, "ticks": ticks,
                     "marks": [{"min": r["min"], "sym": r["sym"], "at": r["at"], "dead": (r["now"] is not None and r["now"] < 0.5) or r["note"] in ("SEEDED", "DEAD POOL", "HONEYPOT")} for r in rows]},
        "rows": rows, "more": max(0, len(alerts) - len(shown)),
        "section": (("THE " + {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT"}.get(len(shown), str(len(shown))))
                    + (f"  ·  +{len(alerts) - len(shown)} MORE IN THE WINDOW, BELOW THESE PEAKS" if len(alerts) > len(shown) else "")) if shown else "NO ALERTS",
        "stats": [
            {"v": _n(tape["n"]), "k": "FILLS ON THE TAPE"},
            {"v": _n(tape["wallets"]), "k": "TRUSTED WALLETS TRADING"},
            {"v": _n(tape["mints"]), "k": "TOKENS TOUCHED"},
            {"v": _usd(tape["usd"]), "k": "THROUGH THEIR WALLETS", "green": True},
            {"v": _n(new_tokens), "k": "NEW TOKENS SEEN"},
        ],
        "tape": {"fills": tape["n"], "wallets": tape["wallets"], "mints": tape["mints"], "usd": tape["usd"], "new_tokens": new_tokens},
        "judged": {"n": n, "of": of, "two": len(two), "best": best, "fast": len(fast), "under": len(under), "with_now": len(with_now),
                   "median": bests[len(bests) // 2] if bests else None},
        "foot_right": f"@{settings.x_handle}  ·  THE BOT CALLS IT THE SECOND IT FORMS  ·  {settings.public_site_url.replace('https://', '')}" if settings.public_site_url else f"@{settings.x_handle}",
    }


def html(d: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    return tpl.replace("/*DAY*/", "window.DAY = " + json.dumps(d, ensure_ascii=False) + ";")


def render(html_path: pathlib.Path, png_path: pathlib.Path, scale: int = 2) -> None:
    """The board as a PNG at 2x, through a headless browser (playwright, chromium)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=scale)
        page.goto(html_path.resolve().as_uri() + "?still=1")
        page.evaluate("document.fonts.ready.then(() => true)")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(png_path), full_page=False)
        b.close()


def post_text(d: dict) -> str:
    """The post beside the board, under 280 characters, no URL (the link is in the bio)."""
    j, t = d["judged"], d["tape"]
    day = d["hours"] <= 24
    when = time.strftime("%d %b", time.gmtime(d["now"])).lstrip("0")
    head = f"The {'day' if day else 'week'} on Robinhood Chain · {when}"
    tape = f"{_n(t['fills'])} fills · {_n(t['wallets'])} trusted wallets · {_n(t['mints'])} tokens · {_usd(t['usd'])}"
    if j["of"]:
        best = f" · best ×{j['best']['best']:.2f} (${j['best']['sym']}" + (f", {j['best']['peak_min']} min in)" if j["best"]["peak_min"] is not None else ")")
        calls = f"{j['of']} alerts · {j['n']} traded above the call · {j['two']} hit ×2{best}"
        shape = f"{j['fast']} of {j['of']} peaked inside 15 min." + (f" Held to now, {j['under']} of {j['with_now']} sit under entry." if j["with_now"] else "")
    else:
        calls = "No alert cleared the bar. A quiet one."
        shape = ""
    tail = "Live calls: bot in bio"
    for parts in ([head, "", tape, "", calls, "", shape, "", tail], [head, "", tape, "", calls, "", tail], [head, tape, calls, tail], [head, calls, tail]):
        text = "\n".join(p for p in parts if p is not None).replace("\n\n\n", "\n\n").strip()
        if len(text) <= 280:
            return text
    return (head + "\n" + calls)[:280]


def run(conn: sqlite3.Connection, hours: int = 24, out_dir: pathlib.Path | None = None, post: bool = False,
        now: int | None = None, client=None) -> dict:
    """Make the board for the window, and post it if asked. Returns where things went."""
    now = now or db.now()
    d = data(conn, hours, now)
    out_dir = out_dir or (pathlib.Path(settings.board_dir) if settings.board_dir else pathlib.Path("boards"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{'day' if hours <= 24 else 'week'}-{time.strftime('%Y-%m-%d', time.gmtime(now))}"
    html_path, png_path = out_dir / f"{stem}.html", out_dir / f"{stem}.png"
    html_path.write_text(html(d), encoding="utf-8")
    render(html_path, png_path)
    text = post_text(d)
    (out_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    result = {"html": str(html_path), "png": str(png_path), "text": text, "alerts": len(d["rows"]) + d["more"], "posted": None}
    if post:
        from . import xpost
        if not xpost.enabled():
            result["posted"] = "x is off"
            return result
        if client is None:
            from ..sources.x import XClient
            client = XClient()
        mid = client.upload(png_path)
        tid = client.post(text, media_ids=[mid])
        with db.tx(conn):
            conn.execute("INSERT INTO x_posts(push_id, kind, mint, due_ts, text, posted_at, tweet_id) VALUES(NULL, ?, '', ?, ?, ?, ?)",
                         ("board" if hours <= 24 else "board-week", now, text, now, tid))
        result["posted"] = tid
        log.info("board: posted %s as %s", stem, tid)
    return result
