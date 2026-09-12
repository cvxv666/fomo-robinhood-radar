"""The $FOMOBRAIN flywheel, as one picture: 1600 x 900, the site's grammar.

A flywheel is a loop where every stage pushes the next and the last pushes the first, so the
thing speeds up the longer it runs. Drawn the way flywheels are drawn - one ring, stations on it,
arrows one way round - with the brain at the hub, because the brain is what turns it: it makes
the signals that are sold, and it makes the signals the algo trades. Two roads come in from
outside the ring, and every road ends at the same station.

Run:  .venv/Scripts/python assets/brand/fomobrain/make_flywheel.py
"""
from __future__ import annotations

import math
import pathlib
import sys

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from make_brand import (AMBER, BLACK, CARBON, CRIMSON, GRAPHITE, GREEN, SS, WHITE, YELLOW,  # noqa: E402
                        canvas, draw_runs, mark, meter, mono, run_width, sans, save)
from make_fomobrain import brain  # noqa: E402

S = lambda v: v * SS  # noqa: E731  - design units to canvas pixels
VIOLET = "#6100ff"
CYAN = "#8fd9ff"  # the brain's own tint, for the three spokes it drives


# ---------------------------------------------------------------- primitives

def text(d, x, y, s, f, colour, track=0.0, anchor="l"):
    """Tracked text at design coordinates; `anchor` l / c / r on the baseline."""
    w = run_width([(s, f)], S(track))
    x0 = S(x) - (w / 2 if anchor == "c" else w if anchor == "r" else 0)
    draw_runs(d, x0, S(y), [(s, f)], colour, S(track))
    return w / SS


def line(d, x0, y0, x1, y1, colour, w=1.0, dashed=False):
    if not dashed:
        d.line([S(x0), S(y0), S(x1), S(y1)], fill=colour, width=max(1, round(S(w))))
        return
    L = math.hypot(x1 - x0, y1 - y0)
    if L == 0:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    t, on, off = 0.0, 6.0, 5.0
    while t < L:
        e = min(L, t + on)
        d.line([S(x0 + ux * t), S(y0 + uy * t), S(x0 + ux * e), S(y0 + uy * e)],
               fill=colour, width=max(1, round(S(w))))
        t += on + off


def arrowhead(d, x, y, ang, colour, size=12.0):
    """A filled triangle with its tip at (x, y) pointing along `ang` (radians, screen)."""
    bx, by = x - math.cos(ang) * size, y - math.sin(ang) * size
    nx, ny = -math.sin(ang) * size * 0.48, math.cos(ang) * size * 0.48
    d.polygon([(S(x), S(y)), (S(bx + nx), S(by + ny)), (S(bx - nx), S(by - ny))], fill=colour)


def arc_arrow(d, cx, cy, r, a0, a1, colour, w=2.0, dashed=False, dots=0):
    """An arc from angle a0 to a1 (degrees, clockwise on screen) with a head at a1."""
    if dashed:
        step = 3.0
        a = a0
        while a < a1:
            e = min(a1, a + step)
            d.arc([S(cx - r), S(cy - r), S(cx + r), S(cy + r)], a, e, fill=colour, width=max(1, round(S(w))))
            a += step * 2
    else:
        d.arc([S(cx - r), S(cy - r), S(cx + r), S(cy + r)], a0, a1, fill=colour, width=max(1, round(S(w))))
    t = math.radians(a1)
    arrowhead(d, cx + math.cos(t) * r, cy + math.sin(t) * r, t + math.pi / 2, colour)
    # a few units travelling the arc, brighter as they approach the head
    for k in range(dots):
        f = (k + 1) / (dots + 1)
        t = math.radians(a0 + (a1 - a0) * f)
        px, py = cx + math.cos(t) * r, cy + math.sin(t) * r
        rr = 2.2 + f * 1.8
        d.ellipse([S(px - rr), S(py - rr), S(px + rr), S(py + rr)], fill=colour)


def curve_arrow(d, p0, p1, p2, colour, w=2.0, dots=0, dashed=False):
    """A quadratic curve p0 -> p2 bent through p1, head at p2, optional units along it."""
    pts = []
    for i in range(61):
        t = i / 60
        u = 1 - t
        pts.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    if dashed:
        for i in range(0, 60, 4):
            a, b = pts[i], pts[min(60, i + 2)]
            d.line([S(a[0]), S(a[1]), S(b[0]), S(b[1])], fill=colour, width=max(1, round(S(w))))
    else:
        d.line([(S(x), S(y)) for x, y in pts], fill=colour, width=max(1, round(S(w))), joint="curve")
    ang = math.atan2(pts[-1][1] - pts[-3][1], pts[-1][0] - pts[-3][0])
    arrowhead(d, pts[-1][0], pts[-1][1], ang, colour)
    for k in range(dots):
        f = (k + 1) / (dots + 1)
        px, py = pts[int(f * 60)]
        rr = 2.2 + f * 1.8
        d.ellipse([S(px - rr), S(py - rr), S(px + rr), S(py + rr)], fill=colour)


def node(d, x, y, r, colour, w=2.0, dashed=False):
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], fill=BLACK)
    if dashed:
        for a in range(0, 360, 12):
            d.arc([S(x - r), S(y - r), S(x + r), S(y + r)], a, a + 6, fill=colour, width=max(1, round(S(w))))
    else:
        d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=colour, width=max(1, round(S(w))))


def station_label(d, x, y, name, sub, colour, anchor="c", sub_colour=GRAPHITE):
    text(d, x, y, name, mono(S(17), 700), colour, track=1.6, anchor=anchor)
    text(d, x, y + 19, sub, mono(S(12.5)), sub_colour, track=0.5, anchor=anchor)


# ---------------------------------------------------------------- the icons, all hairline
# every icon takes k, its size relative to the first drawing, so the nodes can grow as one

def icon_candles(d, x, y, k=1.0):
    for i, (h, up) in enumerate(((14, True), (20, False), (26, True))):
        h *= k
        cx = x + (-12 + i * 12) * k
        colour = GREEN if up else CRIMSON
        line(d, cx, y - h / 2 - 5 * k, cx, y + h / 2 + 5 * k, colour, 1.2)
        d.rectangle([S(cx - 3.5 * k), S(y - h / 2), S(cx + 3.5 * k), S(y + h / 2)], fill=BLACK, outline=colour, width=max(1, round(S(1.4))))


def icon_fee(d, x, y, k=1.0):
    # a coin with a slice taken: the fee is the wedge
    r = 15 * k
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=WHITE, width=max(1, round(S(1.4))))
    d.pieslice([S(x - r), S(y - r), S(x + r), S(y + r)], 300, 360, fill=AMBER)
    text(d, x - 1 * k, y + 5 * k, "%", mono(S(15 * k), 700), WHITE, anchor="c")


def icon_deposit(d, x, y, k=1.0):
    meter(d, S(x - 20 * k), S(y - 7 * k), S(40 * k), S(14 * k), 6, 10)


def icon_buyback(d, x, y, k=1.0):
    # the market on the left, the token on the right, the arrow is the buy
    r = 9 * k
    d.ellipse([S(x + 6 * k - r), S(y - r), S(x + 6 * k + r), S(y + r)], outline=WHITE, width=max(1, round(S(1.4))))
    line(d, x - 22 * k, y, x - 8 * k, y, WHITE, 1.6)
    arrowhead(d, x - 6 * k, y, 0, WHITE, 8 * k)
    text(d, x + 6 * k, y + 3.5 * k, "50", mono(S(8.5 * k), 700), WHITE, anchor="c")


def icon_burn(d, x, y, k=1.0):
    r = 16 * k
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=CRIMSON, width=max(1, round(S(2))))
    line(d, x - r * 0.72, y + r * 0.72, x + r * 0.72, y - r * 0.72, CRIMSON, 2)
    text(d, x, y + 4 * k, "0", mono(S(12 * k), 700), CRIMSON, anchor="c")


def icon_signal(d, x, y, k=1.0):
    # a message with a burst in it
    d.rounded_rectangle([S(x - 19 * k), S(y - 12 * k), S(x + 19 * k), S(y + 9 * k)], radius=S(4 * k), outline=GREEN, width=max(1, round(S(1.4))))
    d.polygon([(S(x - 12 * k), S(y + 9 * k)), (S(x - 12 * k), S(y + 15 * k)), (S(x - 5 * k), S(y + 9 * k))], fill=BLACK, outline=GREEN)
    line(d, x - 12 * k, y + 9 * k, x - 12 * k, y + 15 * k, GREEN, 1.4)
    line(d, x - 12 * k, y + 15 * k, x - 5 * k, y + 9 * k, GREEN, 1.4)
    text(d, x, y + 3 * k, "\u25b2 4.1", mono(S(8.5 * k), 700), GREEN, anchor="c")


def icon_algo(d, x, y, k=1.0):
    # a chip with pins: the finished algo, boxed and handed over
    d.rectangle([S(x - 17 * k), S(y - 13 * k), S(x + 17 * k), S(y + 13 * k)], outline=WHITE, width=max(1, round(S(1.4))))
    for i in range(4):
        px = x + (-12 + i * 8) * k
        line(d, px, y - 13 * k, px, y - 18 * k, WHITE, 1.2)
        line(d, px, y + 13 * k, px, y + 18 * k, WHITE, 1.2)
    text(d, x, y + 4 * k, "ALGO", mono(S(8.5 * k), 700), WHITE, anchor="c")


# ---------------------------------------------------------------- the picture

def flywheel(name: str = "flywheel-1600x900.png", w: int = 1600, h: int = 900) -> None:
    im, d = canvas(w, h)
    pad = 60
    K = 1.4  # the icons, relative to the first drawing

    # ── header: what this is, in the words of the card
    d.ellipse([S(pad), S(54), S(pad + 8), S(62)], fill=GREEN)
    text(d, pad + 18, 63, "$FOMOBRAIN  ·  THE FLYWHEEL  ·  ROBINHOOD CHAIN", mono(S(12.5)), GRAPHITE, track=1.6)
    text(d, pad, 122, "EVERY ROAD ENDS AT BURN", sans(S(54), 300), WHITE, track=3.8)
    text(d, pad, 151, "signals, fees and the algo itself are paid in $FOMOBRAIN; what is paid is destroyed",
         mono(S(13)), GRAPHITE, track=0.6)
    mark(d, S(w - pad - 48), S(44), S(48))
    text(d, w - pad - 62, 66, "FOMO", sans(S(19), 700), WHITE, track=0.9, anchor="r")
    text(d, w - pad - 62, 88, "BRAIN", sans(S(19), 300), WHITE, track=0.9, anchor="r")
    line(d, pad, 172, w - pad, 172, CARBON, 1)

    # ── the wheel
    cx, cy, R = 800, 505, 230
    ST = {  # angle on the ring, clockwise from three o'clock, screen-wise
        "trades": 162, "fees": 234, "deposit": 306, "buyback": 18, "burn": 90,
    }
    P = {k: (cx + math.cos(math.radians(a)) * R, cy + math.sin(math.radians(a)) * R) for k, a in ST.items()}
    NR = 44      # node radius
    gap = math.degrees(math.asin(NR / R)) + 2.5

    # the ring itself, faint, so the arcs read as the mechanism and the ring as the wheel
    d.ellipse([S(cx - R), S(cy - R), S(cx + R), S(cy + R)], outline=CARBON, width=max(1, round(S(1))))

    # the arcs: each station pushes the next
    arc_arrow(d, cx, cy, R, ST["trades"] + gap, ST["fees"] - gap, WHITE, dots=3)
    arc_arrow(d, cx, cy, R, ST["fees"] + gap, ST["deposit"] - gap, WHITE, dots=3)
    arc_arrow(d, cx, cy, R, ST["deposit"] + gap, ST["buyback"] + 360 - gap, AMBER, dots=3)
    arc_arrow(d, cx, cy, R, ST["buyback"] + gap, ST["burn"] - gap, CRIMSON, dots=4)
    # the wheel closing: less supply against the same demand is the market's reply, so dashed
    arc_arrow(d, cx, cy, R, ST["burn"] + gap, ST["trades"] - gap, GRAPHITE, dashed=True)

    # what each arc carries, set just outside the ring at its middle
    def arc_label(a0, a1, s, colour, out=30):
        a = math.radians((a0 + a1) / 2)
        x, y = cx + math.cos(a) * (R + out), cy + math.sin(a) * (R + out)
        anchor = "r" if math.cos(a) < -0.3 else "l" if math.cos(a) > 0.3 else "c"
        text(d, x, y + 4.5, s, mono(S(12.5), 700), colour, track=1, anchor=anchor)
    arc_label(ST["trades"], ST["fees"], "EVERY TRADE PAYS", WHITE)
    arc_label(ST["fees"], ST["deposit"], "CREATOR FEES FUND THE STAKE", WHITE, out=34)
    arc_label(ST["deposit"], ST["buyback"] + 360, "50% OF PROFIT", AMBER)
    arc_label(ST["buyback"], ST["burn"], "BOUGHT OFF THE MARKET", CRIMSON)
    arc_label(ST["burn"], ST["trades"], "LESS SUPPLY \u00b7 SAME DEMAND", GRAPHITE)

    # the deposit compounds: a small loop on its own station
    dx, dy = P["deposit"]
    lr = 28
    lx, ly = dx + 40, dy - 40
    arc_arrow(d, lx, ly, lr, 150, 460, AMBER, w=1.6)
    text(d, lx + 38, ly - 6, "50% BACK IN", mono(S(12.5), 700), AMBER, track=1)
    text(d, lx + 38, ly + 11, "the stake compounds", mono(S(11.5)), GRAPHITE, track=0.4)

    # ── the hub: the brain, and the three things it drives
    hub = brain(380, 380, scale=0.44, cy=0.47, d=2.6, dot=280)
    im.paste(hub.convert("RGB"), (S(cx - 190), S(cy - 206)), hub)
    d = ImageDraw.Draw(im)
    text(d, cx, cy + 104, "THE BRAIN", mono(S(17), 700), WHITE, track=1.8, anchor="c")
    text(d, cx, cy + 123, "169 traders \u00b7 one mind \u00b7 reads the chain every 20 s", mono(S(11.5)), GRAPHITE, track=0.4, anchor="c")

    # the two roads from outside the ring, both ending at the same station
    sig = (272, 690)
    alg = (1328, 690)
    node(d, *sig, 42, GREEN)
    icon_signal(d, *sig, K)
    station_label(d, sig[0], sig[1] + 72, "SIGNALS", "in the bot \u00b7 paid in $FOMOBRAIN", GREEN)
    node(d, *alg, 42, WHITE)
    icon_algo(d, *alg, K)
    station_label(d, alg[0], alg[1] + 72, "THE ALGO", "sold or rented \u00b7 paid in $FOMOBRAIN", WHITE)

    bx, by = P["burn"]
    curve_arrow(d, (sig[0] + 44, sig[1] + 8), (560, 800), (bx - 54, by + 14), GREEN, dots=4)
    curve_arrow(d, (alg[0] - 44, alg[1] + 8), (1040, 800), (bx + 54, by + 14), WHITE, dots=4)
    text(d, 545, 812, "100% BURNED", mono(S(12.5), 700), GREEN, track=1.2, anchor="c")
    text(d, 1055, 812, "100% BURNED", mono(S(12.5), 700), WHITE, track=1.2, anchor="c")

    # spokes: the brain makes the signals, and it makes the signals the algo trades
    spoke = lambda x0, y0, x1, y1, s, lx, ly, anchor: (  # noqa: E731
        line(d, x0, y0, x1, y1, CYAN, 1.2, dashed=True),
        text(d, lx, ly, s, mono(S(11), 700), CYAN, track=0.8, anchor=anchor))
    spoke(cx - 84, cy + 68, sig[0] + 36, sig[1] - 28, "MAKES THE SIGNALS", 600, 668, "r")
    spoke(cx + 84, cy + 68, alg[0] - 36, alg[1] - 28, "MAKES THE SIGNALS IT TRADES", 1010, 668, "l")
    spoke(cx + 66, cy - 84, P["deposit"][0] - 30, P["deposit"][1] + 34, "", 0, 0, "l")

    # ── the stations, drawn last so they sit over the arcs
    node(d, *P["trades"], NR, GREEN)
    icon_candles(d, *P["trades"], K)
    station_label(d, P["trades"][0] - 60, P["trades"][1] + 4, "TRADES", "of $FOMOBRAIN", GREEN, anchor="r")

    node(d, *P["fees"], NR, WHITE)
    icon_fee(d, *P["fees"], K)
    station_label(d, P["fees"][0] - 58, P["fees"][1] - 8, "CREATOR FEES", "on every trade", WHITE, anchor="r")

    node(d, *P["deposit"], NR, AMBER)
    icon_deposit(d, *P["deposit"], K)
    station_label(d, P["deposit"][0] + 60, P["deposit"][1] + 34, "THE DEPOSIT", "the algo trades it on the brain's signals", AMBER, anchor="l")

    node(d, *P["buyback"], NR, WHITE)
    icon_buyback(d, *P["buyback"], K)
    station_label(d, P["buyback"][0] + 60, P["buyback"][1] + 4, "BUYBACK", "50% of what the algo makes", WHITE, anchor="l")

    node(d, *P["burn"], NR + 10, CRIMSON, w=2.4, dashed=True)
    icon_burn(d, *P["burn"], K)
    text(d, bx, by + 76, "BURN", mono(S(20), 700), CRIMSON, track=2.6, anchor="c")
    text(d, bx, by + 94, "gone. every road ends here.", mono(S(12)), GRAPHITE, track=0.5, anchor="c")

    # ── the rail: the two sentences that are the whole argument
    line(d, pad, 848, w - pad, 848, CARBON, 1)
    y = 880
    fs = 14.5
    x = pad
    for s, colour in (("MORE SUBSCRIBERS", WHITE), ("\u2192", AMBER), ("MORE BURN", CRIMSON)):
        x += text(d, x, y, s, mono(S(fs), 700 if colour != AMBER else 400), colour, track=1.4) + 14
    parts = [("MORE VOLUME", WHITE), ("\u2192", AMBER), ("BIGGER DEPOSIT", AMBER), ("\u2192", AMBER), ("MORE PROFIT", WHITE),
             ("\u2192", AMBER), ("BIGGER BUYBACK", WHITE), ("\u2192", AMBER), ("MORE BURN", CRIMSON)]
    total = sum(run_width([(s, mono(S(fs), 700 if c != AMBER else 400))], S(1.4)) / SS + 14 for s, c in parts) - 14
    x = w - pad - total
    for s, colour in parts:
        x += text(d, x, y, s, mono(S(fs), 700 if colour != AMBER else 400), colour, track=1.4) + 14

    save(im, name, w, h, HERE)


if __name__ == "__main__":
    flywheel()
