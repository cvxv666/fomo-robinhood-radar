"""Telegram bot — the one surface that comes to the reader instead of waiting to be opened.

Everything else we build has to be visited. A signal is only worth something while it is fresh, so
this pushes: when a second wallet scoring 60+ enters a token, subscribers hear about it within a
minute. On demand it also answers the two questions the terminal answers — whose money is in this
token, and what is this trader actually doing.

Deliberately dependency-free: the Bot API is plain HTTP and we already ship httpx. It uses long
polling rather than webhooks, so it runs from a laptop behind NAT exactly as well as from a server
with a public address — which keeps the hosting decision open.

Messages are formatted as instrument readouts, per the design system: a monospace block with
aligned columns, no decoration, nothing that has to be scrolled to read on a phone.

This is a package now: `telegram` is the transport, `words` every formatter, `subs` who hears
what and what goes out on a timer, `commands` what the bot answers and the loop. Everything the
rest of the project imports is re-exported here, so `from fomo_agent.bot import X` is unchanged.
"""
from __future__ import annotations

from .. import db  # noqa: F401 - re-exported: callers and tests monkeypatch bot.db
from ..pipeline import analyze, prefs, pro, pushes  # noqa: F401 - and bot.analyze.fresh
from .commands import *  # noqa: F401,F403
from .commands import handle_text, handle_update, is_start, run, status_text
from .subs import (Burns, already_sent, due, followups, gone, mark_sent, notify,  # noqa: F401
                   subscribe, subscribers, sweep_launches, unsubscribe)
from .telegram import API, Telegram, TelegramError  # noqa: F401
from .words import *  # noqa: F401,F403
