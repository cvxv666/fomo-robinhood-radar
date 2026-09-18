"""A Discord channel that hears what the Telegram chats hear.

One webhook URL in the settings, nothing else: no bot, no permissions, no library. The message
is the same one the bot sends, with Telegram's HTML turned into Discord's markdown, and a card
goes as a file. Half of this audience lives in Discord; this is the door.
"""
from __future__ import annotations

import logging
import pathlib
import re

import httpx

from ..config import settings

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.discord_webhook_url)


def markdown(html: str) -> str:
    """Telegram HTML -> Discord markdown, for the messages the bot already writes."""
    s = html
    s = re.sub(r"<pre>(.*?)</pre>", lambda m: "```\n" + m.group(1) + "\n```", s, flags=re.S)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.S)
    s = re.sub(r"<b>(.*?)</b>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<i>(.*?)</i>", r"*\1*", s, flags=re.S)
    # the angle brackets that keep Discord from unfurling a link go in after the tag strip
    s = re.sub(r'<a href="([^"]+)">(.*?)</a>', "[\\2](\x00\\1\x01)", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\x00", "<").replace("\x01", ">")
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return s.strip()


def send(html: str, image: pathlib.Path | None = None, client: httpx.Client | None = None) -> bool:
    """Post one message (and, if given, one image) to the channel. False when off or refused."""
    if not enabled():
        return False
    content = markdown(html)[:1900]
    try:
        c = client or httpx.Client(timeout=10)
        if image and image.exists():
            with open(image, "rb") as fh:
                r = c.post(settings.discord_webhook_url, data={"content": content}, files={"file": (image.name, fh, "image/png")})
        else:
            r = c.post(settings.discord_webhook_url, json={"content": content})
        if r.status_code >= 300:
            log.warning("discord: %s %s", r.status_code, r.text[:120])
            return False
        return True
    except Exception as e:  # noqa: BLE001 - the mirror failing must not stop the original
        log.warning("discord: %s", e)
        return False
