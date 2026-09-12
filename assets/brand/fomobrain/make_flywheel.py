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
    t, on, off = 0.0, 5.0, 4.0
    while t < L:
        e = min(L, t + on)
        d.line([S(x0 + ux * t), S(y0 + uy * t), S(x0 + ux * e), S(y0 + uy * e)],
               fill=colour, width=max(1, round(S(w))))
        t += on + off


def arrowhead(d, x, y, ang, colour, size=9.0):
    """A filled triangle with its tip at (x, y) pointing along `ang` (radians, screen)."""
    bx, by = x - math.cos(ang) * size, y - math.sin(ang) * size
    nx, ny = -math.sin(ang) * size * 0.48, math.cos(ang) * size * 0.48
    d.polygon([(S(x), S(y)), (S(bx + nx), S(by + ny)), (S(bx - nx), S(by - ny))], fill=colour)


def arc_arrow(d, cx, cy, r, a0, a1, colour, w=1.4, dashed=False, dots=0):
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
        rr = 1.6 + f * 1.4
        d.ellipse([S(px - rr), S(py - rr), S(px + rr), S(py + rr)], fill=colour)


def curve_arrow(d, p0, p1, p2, colour, w=1.4, dots=0, dashed=False):
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
        rr = 1.6 + f * 1.4
        d.ellipse([S(px - rr), S(py - rr), S(px + rr), S(py + rr)], fill=colour)


def node(d, x, y, r, colour, w=1.4, dashed=False):
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], fill=BLACK)
    if dashed:
        for a in range(0, 360, 12):
            d.arc([S(x - r), S(y - r), S(x + r), S(y + r)], a, a + 6, fill=colour, width=max(1, round(S(w))))
    else:
        d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=colour, width=max(1, round(S(w))))


def station_label(d, x, y, name, sub, colour, anchor="c", sub_colour=GRAPHITE):
    text(d, x, y, name, mono(S(12.5), 700), colour, track=1.2, anchor=anchor)
    text(d, x, y + 15, sub, mono(S(10)), sub_colour, track=0.5, anchor=anchor)


# ---------------------------------------------------------------- the icons, all hairline

def icon_candles(d, x, y):
    for i, (h, up) in enumerate(((14, True), (20, False), (26, True))):
        cx = x - 12 + i * 12
        colour = GREEN if up else CRIMSON
        line(d, cx, y - h / 2 - 5, cx, y + h / 2 + 5, colour, 1)
        d.rectangle([S(cx - 3.5), S(y - h / 2), S(cx + 3.5), S(y + h / 2)], fill=BLACK, outline=colour, width=max(1, round(S(1.2))))


def icon_fee(d, x, y):
    # a coin with a slice taken: the fee is the wedge
    r = 15
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=WHITE, width=max(1, round(S(1.2))))
    d.pieslice([S(x - r), S(y - r), S(x + r), S(y + r)], 300, 360, fill=AMBER)
    text(d, x - 1, y + 5, "%", mono(S(15), 700), WHITE, anchor="c")


def icon_deposit(d, x, y):
    meter(d, S(x - 20), S(y - 7), S(40), S(14), 6, 10)


def icon_buyback(d, x, y):
    # the market on the left, the token on the right, the arrow is the buy
    r = 9
    d.ellipse([S(x + 6 - r), S(y - r), S(x + 6 + r), S(y + r)], outline=WHITE, width=max(1, round(S(1.2))))
    line(d, x - 22, y, x - 8, y, WHITE, 1.4)
    arrowhead(d, x - 6, y, 0, WHITE, 7)
    text(d, x + 6, y + 3.5, "50", mono(S(8.5), 700), WHITE, anchor="c")


def icon_burn(d, x, y):
    r = 16
    d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], outline=CRIMSON, width=max(1, round(S(1.6))))
    line(d, x - r * 0.72, y + r * 0.72, x + r * 0.72, y - r * 0.72, CRIMSON, 1.6)
    text(d, x, y + 4, "0", mono(S(12), 700), CRIMSON, anchor="c")


def icon_signal(d, x, y):
    # a message with a burst in it
    d.rounded_rectangle([S(x - 19), S(y - 12), S(x + 19), S(y + 9)], radius=S(4), outline=GREEN, width=max(1, round(S(1.2))))
    d.polygon([(S(x - 12), S(y + 9)), (S(x - 12), S(y + 15)), (S(x - 5), S(y + 9))], fill=BLACK, outline=GREEN)
    line(d, x - 12, y + 9, x - 12, y + 15, GREEN, 1.2)
    line(d, x - 12, y + 15, x - 5, y + 9, GREEN, 1.2)
    text(d, x, y + 3, "▲ 4.1", mono(S(8.5), 700), GREEN, anchor="c")


def icon_algo(d, x, y):
    # a chip with a lock: the finished algo, and the closed group it goes to
    d.rectangle([S(x - 17), S(y - 13), S(x + 17), S(y + 13)], outline=WHITE, width=max(1, round(S(1.2))))
    for i in range(4):
        px = x - 12 + i * 8
        line(d, px, y - 13, px, y - 18, WHITE, 1)
        line(d, px, y + 13, px, y + 18, WHITE, 1)
    text(d, x, y + 4, "ALGO", mono(S(8.5), 700), WHITE, anchor="c")


# ---------------------------------------------------------------- the picture

def flywheel(name: str = "flywheel-1600x900.png", w: int = 1600, h: int = 900) -> None:
    im, d = canvas(w, h)
    pad = 60

    # ── header: what this is, in the words of the card
    d.ellipse([S(pad), S(56), S(pad + 7), S(63)], fill=GREEN)
    text(d, pad + 16, 63, "$FOMOBRAIN  ·  THE FLYWHEEL  ·  ROBINHOOD CHAIN", mono(S(11)), GRAPHITE, track=1.5)
    text(d, pad, 122, "EVERY ROAD ENDS AT BURN", sans(S(54), 300), WHITE, track=3.8)
    text(d, pad, 150, "signals, fees and the algo itself are paid in $FOMOBRAIN; what is paid is destroyed",
         mono(S(11.5)), GRAPHITE, track=0.6)
    mark(d, S(w - pad - 44), S(46), S(44))
    text(d, w - pad - 56, 66, "FOMO", sans(S(17), 700), WHITE, track=0.9, anchor="r")
    text(d, w - pad - 56, 86, "BRAIN", sans(S(17), 300), WHITE, track=0.9, anchor="r")
    line(d, pad, 172, w - pad, 172, CARBON, 1)

    # ── the wheel
    cx, cy, R = 800, 515, 232
    ST = {  # angle on the ring, clockwise from three o'clock, screen-wise
        "trades": 162, "fees": 234, "deposit": 306, "buyback": 18, "burn": 90,
    }
    P = {k: (cx + math.cos(math.radians(a)) * R, cy + math.sin(math.radians(a)) * R) for k, a in ST.items()}
    NR = 32      # node radius
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
    def arc_label(a0, a1, s, colour, out=26):
        a = math.radians((a0 + a1) / 2)
        x, y = cx + math.cos(a) * (R + out), cy + math.sin(a) * (R + out)
        anchor = "r" if math.cos(a) < -0.3 else "l" if math.cos(a) > 0.3 else "c"
        text(d, x, y + 3.5, s, mono(S(10)), colour, track=0.8, anchor=anchor)
    arc_label(ST["trades"], ST["fees"], "EVERY TRADE PAYS", WHITE)
    arc_label(ST["fees"], ST["deposit"], "CREATOR FEES FUND THE STAKE", WHITE, out=30)
    arc_label(ST["deposit"], ST["buyback"] + 360, "50% OF PROFIT", AMBER)
    arc_label(ST["buyback"], ST["burn"], "BOUGHT OFF THE MARKET", CRIMSON)
    arc_label(ST["burn"], ST["trades"], "LESS SUPPLY · SAME DEMAND", GRAPHITE)

    # the deposit compounds: a small loop on its own station
    dx, dy = P["deposit"]
    lr = 22
    lx, ly = dx + 30, dy - 30
    arc_arrow(d, lx, ly, lr, 150, 460, AMBER, w=1.2)
    text(d, lx + 30, ly - 6, "50% BACK IN", mono(S(9.5), 700), AMBER, track=0.8)
    text(d, lx + 30, ly + 7, "the stake compounds", mono(S(9)), GRAPHITE, track=0.4)

    # ── the hub: the brain, and the three things it drives
    hub = brain(340, 340, scale=0.44, cy=0.47, d=2.6, dot=280)
    im.paste(hub.convert("RGB"), (S(cx - 170), S(cy - 182)), hub)
    d = ImageDraw.Draw(im)
    text(d, cx, cy + 92, "THE BRAIN", mono(S(12.5), 700), WHITE, track=1.4, anchor="c")
    text(d, cx, cy + 107, "169 traders · one mind · reads the chain every 20 s", mono(S(9.5)), GRAPHITE, track=0.4, anchor="c")

    # the two roads from outside the ring, both ending at the same station
    sig = (300, 700)
    alg = (1300, 700)
    node(d, *sig, 30, GREEN)
    icon_signal(d, *sig)
    station_label(d, sig[0], sig[1] + 56, "SIGNALS", "in the bot · paid in $FOMOBRAIN", GREEN)
    node(d, *alg, 30, WHITE)
    icon_algo(d, *alg)
    station_label(d, alg[0], alg[1] + 56, "THE ALGO", "sold or rented · paid in $FOMOBRAIN", WHITE)

    bx, by = P["burn"]
    curve_arrow(d, (sig[0] + 32, sig[1] + 6), (560, 790), (bx - 40, by + 12), GREEN, dots=4)
    curve_arrow(d, (alg[0] - 32, alg[1] + 6), (1040, 790), (bx + 40, by + 12), WHITE, dots=4)
    text(d, 548, 806, "100% BURNED", mono(S(10), 700), GREEN, track=1, anchor="c")
    text(d, 1052, 806, "100% BURNED", mono(S(10), 700), WHITE, track=1, anchor="c")

    # spokes: the brain makes the signals, and it makes the signals the algo trades
    spoke = lambda x0, y0, x1, y1, s, lx, ly, anchor: (  # noqa: E731
        line(d, x0, y0, x1, y1, CYAN, 1, dashed=True),
        text(d, lx, ly, s, mono(S(9)), CYAN, track=0.6, anchor=anchor))
    spoke(cx - 78, cy + 62, sig[0] + 26, sig[1] - 22, "MAKES THE SIGNALS", 470, 692, "r")
    spoke(cx + 78, cy + 62, alg[0] - 26, alg[1] - 22, "MAKES THE SIGNALS IT TRADES", 1130, 627, "l")
    spoke(cx + 62, cy - 78, P["deposit"][0] - 22, P["deposit"][1] + 24, "", 0, 0, "l")

    # ── the stations, drawn last so they sit over the arcs
    node(d, *P["trades"], NR, GREEN)
    icon_candles(d, *P["trades"])
    station_label(d, P["trades"][0] - 46, P["trades"][1] + 4, "TRADES", "of $FOMOBRAIN", GREEN, anchor="r")

    node(d, *P["fees"], NR, WHITE)
    icon_fee(d, *P["fees"])
    station_label(d, P["fees"][0] - 44, P["fees"][1] - 6, "CREATOR FEES", "on every trade", WHITE, anchor="r")

    node(d, *P["deposit"], NR, AMBER)
    icon_deposit(d, *P["deposit"])
    station_label(d, P["deposit"][0] + 46, P["deposit"][1] + 28, "THE DEPOSIT", "the algo trades it on the brain's signals", AMBER, anchor="l")

    node(d, *P["buyback"], NR, WHITE)
    icon_buyback(d, *P["buyback"])
    station_label(d, P["buyback"][0] + 46, P["buyback"][1] + 4, "BUYBACK", "50% of what the algo makes", WHITE, anchor="l")

    node(d, *P["burn"], NR + 8, CRIMSON, w=1.8, dashed=True)
    icon_burn(d, *P["burn"])
    text(d, bx, by + 62, "BURN", mono(S(14), 700), CRIMSON, track=2, anchor="c")
    text(d, bx, by + 77, "gone. every road ends here.", mono(S(10)), GRAPHITE, track=0.5, anchor="c")

    # ── the rail: the two sentences that are the whole argument
    line(d, pad, 838, w - pad, 838, CARBON, 1)
    y = 866
    x = pad
    for s, colour in (("MORE SUBSCRIBERS", WHITE), ("→", AMBER), ("MORE BURN", CRIMSON)):
        x += text(d, x, y, s, mono(S(11.5), 700 if colour != AMBER else 400), colour, track=1.2) + 12
    x = w - pad
    parts = [("MORE VOLUME", WHITE), ("→", AMBER), ("BIGGER DEPOSIT", AMBER), ("→", AMBER), ("MORE PROFIT", WHITE),
             ("→", AMBER), ("BIGGER BUYBACK", WHITE), ("→", AMBER), ("MORE BURN", CRIMSON)]
    total = sum(run_width([(s, mono(S(11.5), 700 if c != AMBER else 400))], S(1.2)) / SS + 12 for s, c in parts) - 12
    x = w - pad - total
    for s, colour in parts:
        x += text(d, x, y, s, mono(S(11.5), 700 if colour != AMBER else 400), colour, track=1.2) + 12

    save(im, name, w, h, HERE)


if __name__ == "__main__":
    flywheel()
