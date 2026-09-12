"""A case file: one event the radar caught, as a 1600x900 card in the site's own hand.

Same grammar as the brand pieces — pitch black, 1px hairlines, no shadow, no gradient, radius 0
or a pill, Space Grotesk and JetBrains Mono from the site's tokens — drawn at 3x and downscaled
so the hairlines survive. The data is a small dict at the bottom; the drawing reads only that, so
the next case is a new dict rather than a new script.

    python assets/cases/make_case.py catgpt
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "brand"))
from make_brand import (  # noqa: E402
    AMBER, BLACK, CARBON, CRIMSON, GRAPHITE, GREEN, SS, WHITE, YELLOW, canvas, draw_runs, mark,
    mono, pill, run_width, sans, save,
)

VIOLET = "#6100ff"
HERE = pathlib.Path(__file__).parent
S = lambda v: v * SS  # noqa: E731 - design pixels to render pixels


def hairline(d, x0, y0, x1, y1, colour=CARBON, dashed=False, w=1):
    if not dashed:
        d.line([S(x0), S(y0), S(x1), S(y1)], fill=colour, width=max(1, round(S(w))))
        return
    # dashes of 4 on 4 off, in design pixels
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    n = max(1, int(length / 8))
    for i in range(n):
        t0, t1 = i / n, (i + 0.5) / n
        d.line([S(x0 + (x1 - x0) * t0), S(y0 + (y1 - y0) * t0),
                S(x0 + (x1 - x0) * t1), S(y0 + (y1 - y0) * t1)], fill=colour, width=max(1, round(S(w))))


def text(d, x, baseline, s, f, colour, track=0.0, anchor="l"):
    """Tracked text in design coordinates. `anchor` r right-aligns, c centres."""
    w = run_width([(s, f)], S(track)) / SS
    if anchor == "r":
        x -= w
    elif anchor == "c":
        x -= w / 2
    draw_runs(d, S(x), S(baseline), [(s, f)], colour, S(track))
    return w


def circle_avatar(im, path: pathlib.Path | None, cx, cy, r, ring, initial=""):
    """A profile picture in a 1px ring; a letter in the ring when there is no picture."""
    d = ImageDraw.Draw(im)
    box = [S(cx - r), S(cy - r), S(cx + r), S(cy + r)]
    if path and path.exists():
        try:
            pic = Image.open(path).convert("RGB")
            side = int(S(2 * r))
            pic = ImageOps.fit(pic, (side, side), Image.LANCZOS)
            mask = Image.new("L", (side, side), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, side - 1, side - 1], fill=255)
            im.paste(pic, (int(box[0]), int(box[1])), mask)
        except Exception:  # noqa: BLE001 - a bad file is a placeholder, not a crash
            path = None
    if not path or not path.exists():
        d.ellipse(box, fill="#101010")
        f = mono(S(r * 0.9), 700)
        d.text((S(cx), S(cy)), initial.upper(), font=f, fill=GRAPHITE, anchor="mm")
    d.ellipse(box, outline=ring, width=max(1, round(S(1.2))))


def hhmm(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%H:%M")


def hhmmss(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%H:%M:%S")


def render(case: dict, w: int = 1600, h: int = 900) -> None:
    im, d = canvas(w, h)
    pad = 56

    # ── head ───────────────────────────────────────────────────────────────────────────────
    mark(d, S(pad), S(22), S(30))
    f7, f3 = sans(S(19), 700), sans(S(19), 300)
    x = pad + 30 + 12
    x = draw_runs(d, S(x), S(43), [("FOMO ", f7), ("ROBINHOOD ", f3), ("RADAR", f7)], WHITE, S(1.0)) / SS
    text(d, w - pad, 42, f"CASE FILE  ·  ${case['symbol']}  ·  {case['date']}  ·  ROBINHOOD CHAIN 4663",
         mono(S(11)), GRAPHITE, track=1.2, anchor="r")
    hairline(d, pad, 66, w - pad, 66, CARBON)

    # ── hero ───────────────────────────────────────────────────────────────────────────────
    big = sans(S(84), 300)
    text(d, pad - 4, 170, f"${case['symbol']}", big, WHITE, track=5)
    px = pad
    base = 218
    for label, colour in case["pills"]:
        px = pill(d, S(px), S(base), label, colour, S(11)) / SS + 10

    # the outcome, right-aligned: the biggest number on the card
    peak = case["peak_multiple"]
    fpeak = sans(S(92), 700)
    text(d, w - pad, 172, f"{peak:.1f}×", fpeak, GREEN, track=-2, anchor="r")
    text(d, w - pad, 200, f"PEAK  ·  {case['peak_after']} AFTER THE ALERT", mono(S(11)), GRAPHITE, track=1.4, anchor="r")
    text(d, w - pad, 222, f"{case['now_multiple']:.1f}× NOW  ·  {case['now_after']} LATER", mono(S(11)), WHITE, track=1.4, anchor="r")

    hairline(d, pad, 250, w - pad, 250, CARBON)

    # ── the entry: the step of conviction, numbered, with the cohort in a strip below ──────
    t0, t1 = case["timeline"]["from"], case["timeline"]["to"]
    tl_x0, tl_x1 = pad, w - pad - 70
    top_y, axis_y = 334, 468
    span = t1 - t0
    X = lambda ts: tl_x0 + (ts - t0) / span * (tl_x1 - tl_x0)  # noqa: E731
    cmax = case["timeline"]["conviction_max"]
    Y = lambda c: axis_y - c / cmax * (axis_y - top_y)  # noqa: E731

    text(d, pad, 276, f"THE ENTRY  ·  {hhmm(t0)} → {hhmm(t1)} UTC  ·  CONVICTION = Σ (SCORE/100)²  ·  FIRST BUY OF EACH TRUSTED WALLET",
         mono(S(11)), GRAPHITE, track=1.4)
    hairline(d, tl_x0, axis_y, tl_x1, axis_y, WHITE)
    tick = t0 - (t0 % 60)
    while tick <= t1:
        xx = X(tick)
        if tl_x0 <= xx <= tl_x1:
            major = (tick % 300) == 0
            hairline(d, xx, axis_y, xx, axis_y + (7 if major else 3), WHITE if major else CARBON)
            if major:
                text(d, xx, axis_y + 21, hhmm(tick), mono(S(9.5)), GRAPHITE, track=1, anchor="c")
        tick += 60
    for c in (2, 4, 6):
        yy = Y(c)
        colour = YELLOW if c == case["burst_bar"] else CARBON
        hairline(d, tl_x0, yy, tl_x1, yy, colour, dashed=True)
        text(d, tl_x1 + 8, yy + 3.5, f"{c:.0f}" + ("  BURST BAR" if c == case["burst_bar"] else ""),
             mono(S(9)), colour, track=1)

    entries = case["entries"]
    conv = 0.0
    prev_x, prev_y = tl_x0, Y(0)
    for e in entries:
        xx = X(e["ts"])
        hairline(d, prev_x, prev_y, xx, prev_y, AMBER, w=1.6)
        conv += (e["score"] / 100) ** 2
        yy = Y(conv)
        hairline(d, xx, prev_y, xx, yy, AMBER, w=1.6)
        e["_x"], e["_y"], e["_conv"] = xx, yy, conv
        prev_x, prev_y = xx, yy
    hairline(d, prev_x, prev_y, tl_x1, prev_y, AMBER, w=1.6)

    # event lines, labels staggered by level so three events inside three minutes still read
    for ev in case["events"]:
        xx = X(ev["ts"])
        level = ev.get("level", 0)
        ly = top_y - 10 - level * 15
        hairline(d, xx, ly + 4, xx, axis_y, ev["colour"], dashed=ev.get("dashed", False), w=1.2)
        anchor = ev.get("anchor", "l")
        lx = xx + (6 if anchor == "l" else -6)
        lab = f"{ev['label']}" + (f"  ·  {ev['sub']}" if ev.get("sub") else "")
        text(d, lx, ly, lab, mono(S(9.5), 700), ev["colour"], track=0.8, anchor=anchor)

    # numbered markers on the step: the number is the wallet's place in the strip below
    for i, e in enumerate(entries, 1):
        ax, ay = e["_x"], e["_y"]
        ring = GREEN if e["score"] >= 80 else AMBER if e["score"] >= 70 else GRAPHITE
        d.ellipse([S(ax - 6.5), S(ay - 6.5), S(ax + 6.5), S(ay + 6.5)], fill=BLACK, outline=ring, width=max(1, round(S(1.2))))
        d.text((S(ax), S(ay + 0.5)), str(i), font=mono(S(7.5), 700), fill=ring, anchor="mm")

    # the cohort, in order of arrival
    n = len(entries)
    strip_y = 522
    cell = (w - 2 * pad) / n
    r = 17
    for i, e in enumerate(entries):
        cx = pad + cell * i
        ring = GREEN if e["score"] >= 80 else AMBER if e["score"] >= 70 else GRAPHITE
        circle_avatar(im, e.get("avatar"), cx + r + 1, strip_y, r, ring, e["handle"][:1])
        d = ImageDraw.Draw(im)
        d.ellipse([S(cx - 1), S(strip_y - r - 8), S(cx + 13), S(strip_y - r + 6)], fill=BLACK, outline=ring, width=max(1, round(S(1))))
        d.text((S(cx + 6), S(strip_y - r - 0.5)), str(i + 1), font=mono(S(7.5), 700), fill=ring, anchor="mm")
        tx = cx + 2 * r + 9
        handle = e["handle"] if len(e["handle"]) <= 11 else e["handle"][:10] + "…"
        text(d, tx, strip_y - 5, f"{handle}", mono(S(9.5), 700), WHITE if e["score"] >= 80 else GRAPHITE, track=0.4)
        text(d, tx, strip_y + 8, f"{e['score']}  ·  ${e['usd']:,.0f}", mono(S(8.5)), GRAPHITE, track=0.4)
        text(d, tx, strip_y + 20, hhmmss(e["ts"]), mono(S(8.5)), CARBON, track=0.4)

    hairline(d, pad, 566, w - pad, 566, CARBON)

    # ── what followed: hourly candles, times the alert price ───────────────────────────────
    text(d, pad, 592, f"WHAT FOLLOWED  ·  HOURLY  ·  × THE ALERT PRICE OF {case['entry_px']}",
         mono(S(11)), GRAPHITE, track=1.4)
    c_x0, c_x1, c_y0, c_y1 = pad + 30, w - pad - 170, 612, 796
    candles = case["candles"]
    ymax = case["candles_max"]
    n = len(candles)
    slot = (c_x1 - c_x0) / n
    CY = lambda m: c_y1 - (m / ymax) * (c_y1 - c_y0)  # noqa: E731
    hairline(d, c_x0, c_y1, c_x1, c_y1, WHITE)
    for m in (1, 5, 10):
        hairline(d, c_x0, CY(m), c_x1, CY(m), CARBON, dashed=True)
        text(d, c_x0 - 6, CY(m) + 4, f"{m}×", mono(S(9)), GRAPHITE, track=0.6, anchor="r")
    for i, (ts, o, hi, lo, cl) in enumerate(candles):
        cx = c_x0 + slot * (i + 0.5)
        colour = GREEN if cl >= o else CRIMSON
        hairline(d, cx, CY(hi), cx, CY(lo), colour, w=1.2)
        body_top, body_bot = CY(max(o, cl)), CY(min(o, cl))
        d.rectangle([S(cx - slot * 0.28), S(body_top), S(cx + slot * 0.28), S(max(body_bot, body_top + 1))],
                    fill=colour if cl >= o else BLACK, outline=colour, width=max(1, round(S(1))))
        if i % 2 == 0:
            text(d, cx, c_y1 + 20, hhmm(ts), mono(S(9.5)), GRAPHITE, track=1, anchor="c")
    # callouts on the path
    for co in case["callouts"]:
        i = co["index"]
        cx = c_x0 + slot * (i + 0.5)
        yy = CY(co["multiple"])
        d.ellipse([S(cx - 4), S(yy - 4), S(cx + 4), S(yy + 4)], outline=co["colour"], width=max(1, round(S(1.2))))
        dx, dy = co.get("dx", 14), co.get("dy", -14)
        hairline(d, cx, yy, cx + dx, yy + dy, co["colour"])
        anchor = "r" if dx < 0 else "l"
        lx = cx + dx + (4 if dx > 0 else -4)
        text(d, lx, yy + dy + 2, co["label"], mono(S(10), 700), co["colour"], track=0.8, anchor=anchor)
        if co.get("sub"):
            text(d, lx, yy + dy + 15, co["sub"], mono(S(9)), GRAPHITE, track=0.6, anchor=anchor)

    # the right rail: the system's own clock, three numbers
    rx = c_x1 + 34
    for j, (k, v, colour) in enumerate(case["clock"]):
        yy = 640 + j * 58
        text(d, rx, yy, k, mono(S(9.5)), GRAPHITE, track=1.2)
        text(d, rx, yy + 26, v, sans(S(24), 700), colour, track=0.4)

    # ── foot ───────────────────────────────────────────────────────────────────────────────
    hairline(d, pad, 838, w - pad, 838, CARBON)
    text(d, pad, 868, case["foot_left"], mono(S(11)), WHITE, track=1.4)
    text(d, w - pad, 868, case["foot_right"], mono(S(11)), GRAPHITE, track=1.4, anchor="r")

    save(im, f"{case['slug']}.png", w, h, HERE / case["slug"])


def load(slug: str) -> dict:
    case = json.loads((HERE / slug / "case.json").read_text(encoding="utf-8"))
    avatars = HERE / slug / "avatars"
    for e in case["entries"]:
        for ext in ("jpg", "png"):
            p = avatars / f"{e['handle']}.{ext}"
            if p.exists():
                e["avatar"] = p
    return case


if __name__ == "__main__":
    render(load(sys.argv[1] if len(sys.argv) > 1 else "catgpt"))
