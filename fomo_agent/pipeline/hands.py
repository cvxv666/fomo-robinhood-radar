"""The "hands" board: what the radar can do, drawn from what it did - redrawn every morning.

One page in two shapes (1080x1350 for the feed, 1600x900 for X) filled from the same numbers:
the paper run and the share above entry from the ledger, the four wallets most worth following
from the traders table, the API's last day from Caddy's journal, the week's seeding waves from
the tape, the creator with a record and the largest crew. Nothing on it is typed in; the day it
stops being true it stops saying it. Rendered like the day board, posted like it, and served at
/boards/ so the page a screenshot came from is always one click away.
"""
from __future__ import annotations

import collections
import json
import logging
import pathlib
import shutil
import sqlite3
import subprocess
import time

from .. import db
from ..config import settings
from . import record

log = logging.getLogger(__name__)

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "assets" / "boards"
# the wave detector went live at 08:32 UTC on 18 Sep 2026; alerts on seeded tokens before that
# are on the record, but they are not what the board is counting
WAVES_LIVE_TS = 1789720320


def _usd(v: float | None, dash: str = "—") -> str:
    if v is None:
        return dash
    a = abs(v)
    s = f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e3 else f"${a:.0f}"
    return ("−" if v < 0 else "") + s


def qr_data_uri(url: str) -> str:
    """The bot's link as a QR, inline; empty when the qrcode package is not there."""
    try:
        import base64
        import io

        import qrcode
    except ImportError:
        return ""
    img = qrcode.make(url, border=1, box_size=6)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def api_day() -> dict:
    """The API's last 24 hours from Caddy's journal: requests, readers, the heavy ones, by hour,
    the names. Zeros when the journal is not there (a laptop)."""
    out = {"requests": 0, "readers": 0, "heavy": 0, "heavy_share": 0, "hours": [0] * 24, "uas": []}
    try:
        raw = subprocess.run(["journalctl", "-u", "caddy", "--since", "24 hours ago", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        log.debug("caddy journal: %s", e)
        return out
    by_ip: dict[str, int] = collections.Counter()
    by_ua: dict[str, int] = collections.Counter()
    hours = [0] * 24
    for ln in raw.splitlines():
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        req = j.get("request") or {}
        if not req.get("uri", "").startswith("/api"):
            continue
        ip = req.get("remote_ip") or req.get("client_ip") or ""
        by_ip[ip] += 1
        ua = ((req.get("headers") or {}).get("User-Agent") or [""])[0]
        by_ua[ua.split(" (")[0].split("/")[0][:24] or "unnamed"] += 1
        hours[time.gmtime(j.get("ts", 0)).tm_hour] += 1
    total = sum(by_ip.values())
    heavy = [v for v in by_ip.values() if v >= 1000]
    out.update({"requests": total, "readers": len(by_ip), "heavy": len(heavy),
                "heavy_share": round(100 * sum(heavy) / total) if total else 0, "hours": hours,
                "uas": [[n, v] for n, v in by_ua.most_common(6) if n not in ("Mozilla", "unnamed")][:5]})
    return out


def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def data(conn: sqlite3.Connection, now: int | None = None) -> dict:
    now = now or db.now()
    rep = record.report(conn, days=30, now=now)
    t = rep["totals"]
    curve = [round(c["total"]) for c in rep["curve"]]
    curve_trail = [round(c["total"]) for c in rep["curve_trail"]]
    q = t["paper_trail"]
    best = max((c for c in rep["curve"]), key=lambda c: c["pnl"], default=None)
    best_row = next((r for r in rep["pushes"] if best and r["mint"] == best["mint"] and r["paper"] == best["pnl"]), None)

    stars = [dict(r) for r in conn.execute(
        "SELECT fomo_handle handle, score, pnl_30d FROM traders WHERE status = 'active' AND score >= 84 AND fomo_handle IS NOT NULL "
        "AND pnl_30d IS NOT NULL ORDER BY pnl_30d DESC LIMIT 4")]

    week = now - 7 * 86400
    waves = [dict(r) for r in conn.execute(
        "SELECT COALESCE(tk.symbol, substr(t.mint, 1, 8)) sym, MIN(t.ts) t0, COUNT(*) fills, COUNT(DISTINCT t.address) wallets, SUM(t.usd_value) usd "
        "FROM trades t LEFT JOIN tokens tk ON tk.mint = t.mint WHERE t.kind = 'seed' AND t.ts >= ? GROUP BY t.mint ORDER BY t0", (week,))]
    seed_fills = sum(w["fills"] for w in waves)
    seed_usd = sum(w["usd"] or 0 for w in waves)
    seeded_pushed = conn.execute(
        "SELECT COUNT(*) FROM pushes p WHERE p.ts >= ? AND EXISTS (SELECT 1 FROM trades s WHERE s.mint = p.mint AND s.kind = 'seed' AND s.ts < p.ts)",
        (max(week, WAVES_LIVE_TS),)).fetchone()[0]

    key = conn.execute(
        "SELECT creator, GROUP_CONCAT(symbol, '|') syms, COUNT(*) n FROM tokens WHERE creator IS NOT NULL AND creator != '' AND created_via = 'deploy' "
        "AND EXISTS (SELECT 1 FROM trades s WHERE s.mint = tokens.mint AND s.kind = 'seed') GROUP BY creator ORDER BY n DESC LIMIT 1").fetchone()
    crew = [r["h"] for r in conn.execute(
        "SELECT COALESCE(t.fomo_handle, substr(c.address, 1, 8)) h FROM crews c LEFT JOIN traders t ON t.address = c.address "
        "WHERE c.crew = (SELECT crew FROM crews ORDER BY size DESC, crew LIMIT 1) ORDER BY t.score DESC LIMIT 3")]
    api = api_day()
    price = settings.pro_price_usd or 20
    subs = conn.execute("SELECT COUNT(*) FROM bot_subscribers WHERE active = 1").fetchone()[0]
    week0 = time.gmtime(week)
    return {
        "now": now, "date": time.strftime("%d %b %Y", time.gmtime(now)).lstrip("0").upper(),
        "alerts": t["pushes"], "above_pct": round(100 * t["above_entry"] / t["clean"]) if t["clean"] else 0,
        "above_n": t["above_entry"], "clean": t["clean"], "since": "11 SEP",
        "paper": {"pnl": t["paper"]["pnl"], "trades": t["paper"]["trades"], "win_pct": round(100 * (t["paper"]["win_rate"] or 0)),
                  "best": t["paper"]["best"], "worst": t["paper"]["worst"], "curve": curve,
                  "best_sym": best["sym"] if best else None, "best_x": round(best_row["hour"], 2) if best_row and best_row.get("hour") else None,
                  "best_index": curve.index(round(max(rep["curve"], key=lambda c: c["pnl"])["total"])) if rep["curve"] else 0,
                  "at_1000": round(t["paper"]["pnl"] * 10),
                  "trail": {"pnl": q["pnl"], "trades": q["trades"], "win_pct": round(100 * (q["win_rate"] or 0)), "best": q["best"], "worst": q["worst"],
                            "drop": round(100 * q["drop"]), "curve": curve_trail, "at_1000": round(q["pnl"] * 10)}},
        "reached_2x": t["reached_2x"],
        "stars": [{"handle": s["handle"], "score": s["score"], "pnl": _usd(s["pnl_30d"])} for s in stars],
        "follow": {"free": settings.follow_free_max, "pro": settings.follow_pro_max},
        "invite": {"days": settings.pro_referral_days, "max": settings.pro_referral_max_per_month},
        "api": {**api, "price": price, "burn": api["heavy"] * price},
        "seeder": {"waves": [{"sym": w["sym"], "h": round((w["t0"] - week) / 3600, 1)} for w in waves], "fills": seed_fills, "usd": round(seed_usd),
                   "caught": len(waves), "sent": seeded_pushed, "week_start": time.strftime("%d %b", week0).lstrip("0").upper(),
                   "days": [time.strftime("%d %b", time.gmtime(week + d * 86400)).lstrip("0").upper() for d in range(9)]},
        "key": {"address": key["creator"][:10] + "…" if key else None, "syms": (key["syms"] or "").split("|")[:2] if key else []},
        "crew": crew,
        "subs": subs, "handle": settings.x_handle, "site": (settings.public_site_url or "").replace("https://", ""),
        "bot": settings.telegram_bot_name.lstrip("@"),
        "qr": qr_data_uri(f"https://t.me/{settings.telegram_bot_name.lstrip('@')}"),
    }


def html(d: dict, wide: bool = False) -> str:
    tpl = (TEMPLATES / ("hands-wide.html" if wide else "hands.html")).read_text(encoding="utf-8")
    return tpl.replace("/*HANDS*/", "window.HANDS = " + json.dumps(d, ensure_ascii=False) + ";")


def video(html_path: pathlib.Path, out: pathlib.Path, seconds: int = 8, width: int = 1080, height: int = 1350) -> pathlib.Path:
    """An eight-second loop of the living board, as webm from the browser and mp4 when ffmpeg is there."""
    from playwright.sync_api import sync_playwright

    tmp = out.parent / "rec"
    tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                            record_video_dir=str(tmp), record_video_size={"width": width, "height": height})
        page = ctx.new_page()
        page.goto(html_path.resolve().as_uri())
        page.wait_for_timeout(1500)
        page.wait_for_timeout(seconds * 1000)
        path = page.video.path()
        ctx.close()
        b.close()
    webm = out.with_suffix(".webm")
    pathlib.Path(path).replace(webm)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5", "-i", str(webm), "-t", str(seconds),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", "-an", str(out.with_suffix(".mp4"))],
                       check=True, timeout=300)
        return out.with_suffix(".mp4")
    except Exception as e:  # noqa: BLE001 - no system ffmpeg: the webm is the deliverable
        log.info("hands video: mp4 not made (%s), webm kept", e)
        return webm


def run(conn: sqlite3.Connection, out_dir: pathlib.Path | None = None, post: bool = False, make_video: bool = False,
        now: int | None = None, client=None) -> dict:
    from .board import render

    now = now or db.now()
    d = data(conn, now)
    out_dir = out_dir or (pathlib.Path(settings.board_dir) if settings.board_dir else pathlib.Path("boards"))
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {"alerts": d["alerts"], "above_pct": d["above_pct"]}
    for wide in (False, True):
        stem = "hands-wide" if wide else "hands"
        hp, pp = out_dir / f"{stem}.html", out_dir / f"{stem}.png"
        hp.write_text(html(d, wide), encoding="utf-8")
        render(hp, pp, scale=2, width=1600 if wide else 1080, height=900 if wide else 1350)
        res[stem] = str(pp)
        # a dated copy beside the living one, so a post always has the frame it was made from
        shutil.copyfile(pp, out_dir / f"{stem}-{time.strftime('%Y-%m-%d', time.gmtime(now))}.png")
    if make_video:
        res["video"] = str(video(out_dir / "hands.html", out_dir / "hands"))
    (out_dir / "hands.json").write_text(json.dumps(d, ensure_ascii=False, default=str), encoding="utf-8")
    if post:
        from . import xpost
        if xpost.enabled():
            if client is None:
                from ..sources.x import XClient
                client = XClient()
            q = d["paper"]["trail"]
            text = (f"What the radar can do, from what it did.\n\n"
                    f"{d['above_pct']}% of alerts traded above the call · $100 into every one: {_money(d['paper']['pnl'])} at the hour, "
                    f"{_money(q['pnl'])} stepping off at -{q['drop']}% from the high · "
                    f"{d['api']['requests'] // 1000}K API reads a day from {d['api']['readers']} bots\n\n"
                    f"/follow · /paper · /invite · /webhook — bot in bio")[:280]
            mid = client.upload(out_dir / "hands-wide.png")
            tid = client.post(text, media_ids=[mid])
            with db.tx(conn):
                conn.execute("INSERT INTO x_posts(push_id, kind, mint, due_ts, text, posted_at, tweet_id) VALUES(NULL, 'hands', '', ?, ?, ?, ?)", (now, text, now, tid))
            res["posted"] = tid
    return res
