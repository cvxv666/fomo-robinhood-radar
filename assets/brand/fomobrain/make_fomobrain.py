"""FOMOBRAIN: the token's Dexscreener header and icon, with the site's brain as the character.

The brain is the same point cloud the home page runs (site/src/components/Brain.astro), rendered
to one still by headless Chrome from brain_frame.html - the cloud, the section plane, one lobe
firing, the eyes; no stations, no nerves. The composition around it is the link-preview card from
make_brand.py: the mark, the wordmark, the hairline and the readout strip, with the brain where
the centre of the card used to be empty.

Run:  .venv/Scripts/python assets/brand/fomobrain/make_fomobrain.py
Needs Chrome on the machine (CHROME env var, or the usual Windows / Linux locations).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from make_brand import (BLACK, CARBON, CRIMSON, GRAPHITE, GREEN, SS, WHITE, YELLOW,  # noqa: E402
                        canvas, draw_runs, mark, meter, mono, mono_fit, pill, run_width, sans, save)

FRAME = HERE / "brain_frame.html"

# the one view of the brain both pictures share: three-quarter from the front, a little roll, the
# cerebellum and the stem low and to the left, a small lobe lit near the eyes
# denser and finer than the page draws it: print has no motion to fill the gaps between dots
VIEW = {"yaw": 1.3, "roll": 0.14, "seed": 5, "frames": 7, "front": 25, "d": 2.2, "dot": 300, "t": 2.4,
        "lobe": GREEN, "eyes": 1}


def chrome() -> str:
    cands = [os.environ.get("CHROME", ""),
             r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
             "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    for c in cands:
        if c and pathlib.Path(c).exists():
            return c
    found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if not found:
        raise SystemExit("no Chrome found; set CHROME=/path/to/chrome")
    return found


def brain(w: int, h: int, scale: float, cx: float = 0.5, cy: float = 0.5, ss: int = SS, **view) -> Image.Image:
    """The brain alone on a transparent ground, `w` x `h` CSS pixels drawn at `ss` x."""
    q = dict(VIEW, w=w, h=h, scale=scale, cx=cx, cy=cy, **view)
    url = FRAME.resolve().as_uri() + "?" + urllib.parse.urlencode(q)
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "brain.png"
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--user-data-dir={tmp}/profile", "--default-background-color=00000000",
                        f"--force-device-scale-factor={ss}", f"--window-size={w},{h}",
                        f"--screenshot={out}", url], check=True, capture_output=True, timeout=120)
        return Image.open(out).convert("RGBA")


def icon(name: str, side: int, transparent: bool = False) -> None:
    """The token icon: the brain and nothing else. Dexscreener shows it in a circle at 40px, so the
    cloud fills the square to the edge - the circle may clip the stem and the halo, which is fine -
    the dots are heavier and there are more of them, and the eyes sit near the middle."""
    layer = brain(side, side, scale=0.70, cy=0.46, d=3.2, dot=250)
    if transparent:
        im = layer
    else:
        im = Image.new("RGBA", layer.size, BLACK)
        im.alpha_composite(layer)
    out = im.resize((side, side), Image.LANCZOS)
    path = HERE / name
    out.save(path, "PNG", optimize=True)
    print(f"{str(path.relative_to(HERE.parent.parent.parent)):44} {side}x{side}  {path.stat().st_size // 1024} KB")


def header(name: str, w: int, h: int) -> None:
    """The Dexscreener header, 3:1. The link-preview card's grammar with the brain in the middle:
    lockup and readout strip on the left, the numbers the product is made of on the right."""
    im, d = canvas(w, h)
    W, H = w * SS, h * SS
    pad = W * 0.05
    dy = H * 0.02

    # ── the brain, centre stage, the whole height of the card
    layer = brain(w, h, scale=0.63, cx=0.5, cy=0.45)
    im.paste(layer.convert("RGB"), (0, 0), layer)
    d = ImageDraw.Draw(im)

    # ── left: mark, wordmark, tagline, hairline, readout strip - the card's left column
    col_w = W * 0.31
    m = H * 0.30
    mark(d, pad, H * 0.15 + dy, m)
    tx = pad + m + W * 0.022
    big = H * 0.145
    f7, f3 = sans(big, 700), sans(big, 300)
    track = big * 0.055
    base = H * 0.15 + dy + big * 0.92
    draw_runs(d, tx, base, [("FOMO", f7)], WHITE, track)
    draw_runs(d, tx, base + big * 1.18, [("BRAIN", f3)], WHITE, track)

    tag = "169 TRADERS · ONE MIND"
    fm, tm = mono_fit(tag, col_w, H * 0.046)
    draw_runs(d, pad, H * 0.635 + dy, [(tag, fm)], WHITE, tm)
    d.line([pad, H * 0.70 + dy, pad + col_w, H * 0.70 + dy], fill=CARBON, width=max(1, round(H / 320)))

    row = H * 0.815 + dy
    x = pad
    for text, colour in (("FOLLOW", GREEN), ("WATCH", YELLOW), ("DROP", CRIMSON)):
        x = pill(d, x, row, text, colour, H * 0.040) + W * 0.012
    meter(d, x, row - H * 0.034, W * 0.075, H * 0.032, 7, 10)

    # ── right: the readouts, right-aligned to the same margin; the numbers are the product's own
    rx = W - pad
    lines = [("ROBINHOOD CHAIN", "4663"), ("READS THE TAPE EVERY", "20 S"), ("BURSTS THAT GO 2×", "39%"),
             ("FILLS THAT WERE NOT THEIRS", "1 IN 11")]
    fl, fv = mono(H * 0.036), mono(H * 0.060, 700)
    tl, tv = H * 0.036 * 0.1, H * 0.060 * 0.04
    y = H * 0.16 + dy + H * 0.06
    for label, value in lines:
        vw = run_width([(value, fv)], tv)
        draw_runs(d, rx - vw, y, [(value, fv)], WHITE, tv)
        lw = run_width([(label, fl)], tl)
        draw_runs(d, rx - vw - W * 0.014 - lw, y, [(label, fl)], GRAPHITE, tl)
        y += H * 0.128
    d.line([W - pad - col_w, H * 0.70 + dy, W - pad, H * 0.70 + dy], fill=CARBON, width=max(1, round(H / 320)))
    site = "fomoradar.app"
    fs = mono(H * 0.044, 700)
    ts = H * 0.044 * 0.06
    draw_runs(d, rx - run_width([(site, fs)], ts), row, [(site, fs)], GREEN, ts)
    note = "OPEN SOURCE · MIT"
    fn = mono(H * 0.034)
    tn = H * 0.034 * 0.1
    draw_runs(d, W - pad - col_w, row, [(note, fn)], GRAPHITE, tn)

    save(im, name, w, h, HERE)


if __name__ == "__main__":
    header("dex-header-1500x500.png", 1500, 500)
    icon("dex-icon-512.png", 512)
    icon("dex-icon-1000.png", 1000)
    icon("dex-icon-1000-transparent.png", 1000, transparent=True)
