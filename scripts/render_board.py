"""Render one of the 1600x900 boards in assets/ to a PNG (and a JPG) at 2x, for a post.

    .venv/Scripts/python scripts/render_board.py assets/cases/day-2026-09-17/day.html out.png

Waits for the web fonts, so the render is what the browser would show; ?still=1 is passed to any
board that animates.
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright


def render(src: pathlib.Path, out: pathlib.Path, scale: int = 2) -> None:
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=scale)
        page.goto(src.resolve().as_uri() + "?still=1")
        page.evaluate("document.fonts.ready.then(() => true)")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(out), full_page=False)
        b.close()
    try:
        from PIL import Image
        Image.open(out).convert("RGB").save(out.with_suffix(".jpg"), quality=92)
    except ImportError:
        pass


if __name__ == "__main__":
    render(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
    print("ok", sys.argv[2])
