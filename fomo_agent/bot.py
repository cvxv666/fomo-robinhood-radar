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
"""
from __future__ import annotations

import html
import logging
import pathlib
import time

import httpx

from . import db
from .config import settings
from .pipeline import analyze, pro, pushes

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
ADDRESS_LEN = 42  # 0x + 40 hex


class TelegramError(RuntimeError):
    pass


# ---------------------------------------------------------------- transport

class Telegram:
    """The four Bot API calls we need, and nothing else."""

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token or settings.telegram_bot_token
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set (talk to @BotFather, see .env.example)")
        # api.telegram.org is blocked by some ISPs, Russian ones included: DNS resolves, the TCP
        # connection then times out. A proxy fixes it locally; on a server outside that jurisdiction
        # none is needed, which is the real reason the bot belongs on the server.
        # The read timeout has to outlast a long poll, or every idle poll looks like a failure.
        self.http = client or httpx.Client(
            timeout=settings.telegram_poll_timeout + 15,
            proxy=settings.telegram_proxy or None,
        )
        self.requests = 0
        self._file_ids: dict[str, str] = {}

    def call(self, method: str, **params) -> object:
        self.requests += 1
        r = self.http.post(API.format(token=self.token, method=method), json=params)
        if r.status_code == 409:
            raise TelegramError("another copy of this bot is already polling — stop it first")
        if r.status_code == 401:
            raise TelegramError("TELEGRAM_BOT_TOKEN rejected — check it, or /revoke a new one")
        # Telegram puts the reason in the body - "Forbidden: bot was blocked by the user",
        # "Bad Request: chat not found" - and raise_for_status would throw it away in favour of
        # the bare status line. The reason is what the caller decides on: a chat that blocked us
        # is dropped, and without it eleven blocked chats were retried on every broadcast for a
        # day, seven thousand warnings' worth.
        if r.status_code in (400, 403):
            try:
                reason = r.json().get("description")
            except ValueError:
                reason = None
            raise TelegramError(f"{method}: {reason or r.reason_phrase}")
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result")

    def send(self, chat_id, text: str, preview: bool = False) -> object:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=not preview)

    def photo(self, chat_id, path: pathlib.Path, caption: str) -> object:
        """Send a local image with a caption, uploading it at most once.

        Telegram hands back a file_id for anything it has stored, and accepts that id in place of
        the bytes forever after — so the banner crosses the wire on the first /start of a process
        and never again.
        """
        self.requests += 1
        url = API.format(token=self.token, method="sendPhoto")
        params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        known = self._file_ids.get(str(path))
        if known:
            r = self.http.post(url, json={**params, "photo": known})
        else:
            with open(path, "rb") as fh:
                r = self.http.post(url, data=params, files={"photo": fh})
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise TelegramError(f"sendPhoto: {body.get('description')}")
        result = body.get("result") or {}
        sizes = result.get("photo") or []
        if sizes and not known:
            self._file_ids[str(path)] = sizes[-1]["file_id"]
        return result

    def updates(self, offset: int, timeout: int | None = None) -> list:
        return self.call("getUpdates", offset=offset, allowed_updates=["message"],
                         timeout=timeout if timeout is not None else settings.telegram_poll_timeout)

    def me(self) -> dict:
        return self.call("getMe")

    def set_commands(self, commands: list[tuple[str, str]]) -> object:
        """The list under the menu button. Idempotent, so it runs on every start."""
        return self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])


# ---------------------------------------------------------------- formatting

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def ago(ts: int | None, now: int | None = None) -> str:
    if not ts:
        return "—"
    d = max((now or db.now()) - ts, 0)
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def rows(pairs: list[tuple[str, str]], width: int = 17) -> str:
    """A block of label/value lines that line up — the readout look, inside <pre>."""
    body = "\n".join(f"{k:<{width}}{v:>9}" for k, v in pairs)
    return f"<pre>{esc(body)}</pre>"


def who_line(handles, scores, limit: int = 6) -> str:
    """`unipcs 92 \u00b7 rasmr 88 \u00b7 …` — names carry more than a count does.

    Takes either a list or the comma-joined string sqlite's GROUP_CONCAT produces, because one
    caller aggregates in SQL and the other in Python.
    """
    def parts(v):
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if x is not None]
        return [x for x in (v or "").split(",") if x]

    hs, ss = parts(handles), parts(scores)
    pairs = [f"{esc(h)} {esc(s)}" for h, s in zip(hs, ss)][:limit]
    tail = f" +{len(hs) - limit}" if len(hs) > limit else ""
    return " · ".join(pairs) + tail


def token_link(mint: str | None, label: str) -> str:
    """A token as a tappable name.

    It used to point at DexScreener, which does not index Robinhood Chain and answers 403 — a dead
    link on every message the bot has ever sent. Our own token page is the one that has an answer:
    it draws the chart itself, lists who holds it and at what cost, and now carries what they said
    about it. Falls back to plain text when no site address is configured, rather than to a link
    that goes nowhere.
    """
    if not mint or not settings.public_site_url:
        return f"<b>${esc(label)}</b>"
    return f'<a href="{settings.public_site_url}/token/{esc(mint)}"><b>${esc(label)}</b></a>'


def token_lines(mint: str) -> list[str]:
    """The contract, tappable to copy, and the two places worth opening it in."""
    links = [f'<a href="https://fomo.family/tokens/robinhood/{esc(mint)}">fomo</a>']
    if settings.public_site_url:
        links.insert(0, f'<a href="{settings.public_site_url}/token/{esc(mint)}">breakdown</a>')
    return [f"<code>{esc(mint)}</code>", " · ".join(links) + f" · /token_{esc(mint)}"]


def fmt_signal(s: dict, now: int | None = None) -> str:
    mint = s["mint"]
    out = [f"◤ {token_link(mint, s['sym'])} · conviction {s['conviction']:.1f}", ""]
    out.append(rows([
        ("buyers 60+", str(s["buyers"])),
        ("average score", f"{s['avg_score']:.0f}"),
        ("first entry", ago(s["first_ts"], now) + " ago"),
        ("bought", analyze.usd(s["usd"])),
        ("liquidity", analyze.usd(s["liq"])),
    ]))
    out.append(who_line(s.get("who"), s.get("scores")))
    out.append("")
    out.extend(token_lines(mint))
    return "\n".join(out)


ORDINAL = {2: "2nd", 3: "3rd"}


def clone_line(t: dict, now: int | None = None) -> str | None:
    """'3rd $musebook today · 0x786d… pushed 05:33 · 0x9da1… honeypot' - or nothing."""
    clones = t.get("clones") or []
    if not clones:
        return None
    n = len(clones) + 1
    parts = []
    for c in clones:
        what = ("honeypot" if c.get("unsellable") else
                f"pushed {time.strftime('%H:%M', time.gmtime(c['pushed_at']))}" if c.get("pushed_at") else "not pushed")
        parts.append(f"{short(c['mint'])} {what}")
    return f"\u26a0 {ORDINAL.get(n, f'{n}th')} <b>${esc(t['sym'])}</b> in 24h \u00b7 " + " \u00b7 ".join(parts)


def fmt_hot(h: dict, now: int | None = None) -> str:
    """One burst. The clock is the headline: how much conviction arrived in how few minutes."""
    mins = max(1, round((h["last_ts"] - h["first_ts"]) / 60))
    age = h.get("age_s")
    age_txt = ("\u2014" if age is None else f"{age // 60} min" if age < 5400 else f"{age / 3600:.1f}h")
    out = [f"\u25b2 {token_link(h['mint'], h['sym'])} \u00b7 burst \u00b7 +{h['conviction']:.1f} in {mins} min", ""]
    out.append(rows([
        ("wallets in", str(h["wallets"])),
        ("average score", f"{h['avg_score']:.0f}"),
        ("first of them", ago(h["first_ts"], now) + " ago"),
        ("token age", age_txt),
        ("bought", analyze.usd(h["usd"])),
        ("liquidity", analyze.usd(h.get("liq"))),
    ]))
    out.append(who_line(h.get("who"), h.get("scores")))
    if clone_line(h, now):
        out.append(clone_line(h, now))
    out.append("")
    out.extend(token_lines(h["mint"]))
    return "\n".join(out)


def fmt_hot_list(hot: list[dict], window_min: int, now: int | None = None) -> str:
    if not hot:
        return (f"Nothing is bursting: no token gained {settings.hot_delta:.0f} conviction from "
                f"{settings.hot_min_wallets}+ trusted wallets inside the last {window_min} min.")
    out = [f"<b>BURSTS \u00b7 last {window_min} min \u00b7 Robinhood Chain</b>",
           "<i>conviction that arrived all at once</i>", ""]
    for i, h in enumerate(hot, 1):
        mins = max(1, round((h["last_ts"] - h["first_ts"]) / 60))
        out.append(f"{i:>2}. {token_link(h['mint'], h['sym'])}  +{h['conviction']:.1f} in {mins} min"
                   f"  \u00b7  {h['wallets']} wallets, avg {h['avg_score']:.0f}")
        out.append(f"    <i>{who_line(h.get('who'), h.get('scores'), 4)}</i>")
    return "\n".join(out)


def fmt_signals(sigs: list[dict], hours: int, now: int | None = None) -> str:
    if not sigs:
        return (f"No token has two trusted buyers in the last {hours}h.\n\n"
                "That is information too — the cohort is sitting still.")
    out = [f"<b>SIGNALS · {hours}h · Robinhood Chain</b>",
           "<i>ranked by conviction, not by headcount</i>", ""]
    for i, s in enumerate(sigs, 1):
        out.append(f"{i:>2}. {token_link(s.get('mint'), s['sym'])}  conviction {s['conviction']:.1f}"
                   f"  ·  {s['buyers']} buyers, avg {s['avg_score']:.0f}")
        out.append(f"    <i>{who_line(s.get('who'), s.get('scores'), 4)}</i>")
    out.append("\nSend a token address for the full breakdown.")
    return "\n".join(out)


def fmt_exits(leaving: list[dict], hours: int, now: int | None = None) -> str:
    """Where the cohort is getting out. The mirror of fmt_signals, and the half nobody publishes."""
    if not leaving:
        return (f"Nobody trusted has left a position in the last {hours}h.\n\n"
                "Which is its own answer: the wallets that bought are still sitting in it.")
    out = [f"<b>LEAVING · {hours}h · Robinhood Chain</b>",
           "<i>wallets that have sold most of what we watched them buy</i>", ""]
    for i, t in enumerate(leaving, 1):
        gone = f", {t['gone']} out entirely" if t["gone"] else ""
        out.append(f"{i:>2}. {token_link(t.get('mint'), t['sym'])}  conviction {t['conviction']:.1f}"
                   f"  ·  {t['sellers']} selling{gone}")
        out.append(f"    {analyze.usd(t['usd'])} out · "
                   f"<i>{who_line(t.get('who'), t.get('scores'), 4)}</i>")
    out.append("\nA sale is not an exit — every wallet here has sold at least half its position.")
    return "\n".join(out)


def fmt_digest(d: dict) -> str:
    """The day in one message. Five things that change a decision, and nothing else."""
    from .pipeline.digest import is_quiet

    c = d["counts"]
    head = (f"<b>THE DAY · {d['hours']}h</b>\n"
            f"<i>{c.get('active', 0)} follow · {c.get('watch', 0)} watch · "
            f"{c.get('dropped', 0)} dropped</i>")
    if is_quiet(d):
        return (f"{head}\n\nNothing moved: no trusted wallet entered or left a position, no launch "
                "drew the cohort in, nobody new was scored.\n\nThat is information too.")

    out = [head, ""]
    if d["fresh"]:
        out.append("<b>Launches they entered</b>")
        for t in d["fresh"]:
            # A token whose pool-open time we never learned has no lead to report. Say so rather
            # than printing a zero, which would read as "they were first" — the opposite of unknown.
            lead = (f"first in {t['lead_minutes']:.0f}m after the pool opened"
                    if t.get("lead_minutes") is not None else "launch time unknown")
            out.append(f"  {token_link(t.get('mint'), t['sym'])}  heat {t['heat']:.1f} · {t['buyers']} in, {lead}")
    b = d.get("bursts") or {}
    if b.get("n"):
        # the feed's own scorecard: the backtest, continued live, one day at a time
        line = f"{b['n']} burst{'s' if b['n'] != 1 else ''}"
        if b.get("seeded"):
            line += f" ({b['seeded']} seeded, not counted)"
        if b["measured"]:
            line += (f" · of {b['measured']} old enough to judge, {b['reached_2x']} reached 2x, "
                     f"{b['below_half']} ended below half · median best {b['median_best']:.1f}x")
        else:
            line += " · none old enough to judge yet"
        out.append(f"\n<b>Bursts</b>\n  {line}")
        for t in b.get("top", []):
            mins = max(1, round(t["window_s"] / 60)) if t.get("window_s") else "?"
            now_txt = f", now {t['now']:.1f}x" if t.get("now") is not None else ""
            out.append(f"  {token_link(t.get('mint'), t['sym'])}  +{t['conviction']:.1f} from "
                       f"{t['wallets']} wallets \u2192 best {t['best']:.1f}x{now_txt}")
    if d["signals"]:
        out.append("\n<b>Bought</b>")
        for s in d["signals"]:
            out.append(f"  {token_link(s.get('mint'), s['sym'])}  conviction {s['conviction']:.1f} · "
                       f"{s['buyers']} wallets · {analyze.usd(s['usd'])}")
    if d["exits"]:
        out.append("\n<b>Left</b>")
        for t in d["exits"]:
            gone = f", {t['gone']} out entirely" if t["gone"] else ""
            out.append(f"  ${esc(t['sym'])}  {t['sellers']} selling{gone} · "
                       f"{analyze.usd(t['usd'])} out")
    if d["theses"]:
        out.append("\n<b>Said</b>")
        for th in d["theses"]:
            out.append(f"  <b>{esc(th['handle'])}</b> {th['score']} on ${esc(th['sym'])}\n"
                       f"  <i>{esc(th['text'][:180])}</i>")
    if d["joined_n"]:
        who = " · ".join(f"{esc(j['handle'] or short(j['address']))} {j['score']}"
                         for j in d["joined"])
        more = f" (+{d['joined_n'] - len(d['joined'])} more)" if d["joined_n"] > len(d["joined"]) else ""
        out.append(f"\n<b>Joined the roster</b>\n  {who}{more}")

    h = d["health"]
    if not h["ok"]:
        bad = ", ".join(c["name"] for c in h["checks"] if not c["ok"])
        out.append(f"\n⚠ <b>Broken:</b> {esc(bad)} — /health")
    return "\n".join(out)


def fmt_fresh(feed: dict, now: int | None = None) -> str:
    """The launches the cohort is entering, hottest first."""
    tokens = feed.get("tokens") or []
    hours = feed.get("hours", 24)
    if not tokens:
        return (f"No young token has {feed.get('min_buyers', 2)} trusted buyers opening a "
                f"position in the last {hours}h.\n\n"
                "The cohort is sitting in what it already holds.")
    out = [f"<b>FRESH \u00b7 {hours}h \u00b7 Robinhood Chain</b>",
           "<i>only what the cohort has just started buying, weighted by how early</i>", ""]
    for i, t in enumerate(tokens, 1):
        lead = t.get("lead_minutes")
        # "?" said nothing and read as a defect. Name the two cases apart: either we know when the
        # pool opened and can say how early they were, or we do not and the heat is docked for it.
        when = (("first " + (f"{lead:.0f}m" if lead < 90 else f"{lead / 60:.1f}h") + " after launch")
                if lead is not None else "launch time unknown")
        out.append(f"{i:>2}. {token_link(t.get('mint'), t['sym'])}  heat {t['heat']:.2f}"
                   f"  \u00b7  {t['buyers']} in, {when}")
        out.append(f"    <i>{who_line(t.get('who'), t.get('scores'), 4)}</i>")
    if feed.get("drained"):
        out.append(f"\n<i>{feed['drained']} more had trusted buying, but the pool is drained.</i>")
    out.append("\nSend a token address for the full breakdown.")
    return "\n".join(out)


def fmt_launch(t: dict, now: int | None = None) -> str:
    """One pushed launch. The lead time is the headline: it is what this feed knows and the other does not."""
    lead = t.get("lead_minutes")
    when = ("unknown" if lead is None
            else "the same minute" if lead < 1
            else f"{lead:.0f} min" if lead < 90
            else f"{lead / 60:.1f}h")
    out = [f"\u25c6 <b>${esc(t['sym'])}</b> \u00b7 launch \u00b7 heat {t['heat']:.2f}", ""]
    out.append(rows([
        ("wallets in", str(t["buyers"])),
        ("average score", f"{t['avg_score']:.0f}" if t.get("avg_score") else "\u2014"),
        ("first wallet in", when),
        ("token age", f"{t['age_h']:.0f}h" if t.get("age_h") else "\u2014"),
        ("bought", analyze.usd(t.get("usd"))),
        ("liquidity", analyze.usd(t.get("liq"))),
    ]))
    out.append(who_line(t.get("who"), t.get("scores")))
    if clone_line(t, now):
        out.append(clone_line(t, now))
    out.append(f"\n<code>{esc(t['mint'])}</code>")
    out.append(f"/token_{esc(t['mint'])}")
    return "\n".join(out)


def fmt_health(r: dict) -> str:
    """The state of the machine, worst first. Sent daily and on demand."""
    mark = {True: "\u00b7", False: "\u25c6"}
    head = "<b>ALL CLEAR</b>" if r["ok"] else f"<b>{r['failing']} THINGS NEED A LOOK</b>"
    out = [head, ""]
    for c in r["checks"]:
        line = f"{mark[c['ok']]} <b>{esc(c['name'])}</b> \u2014 {esc(c['detail'])}"
        out.append(line if c["ok"] else f"<i>{line}</i>")
    return "\n".join(out)


def fmt_token(a: dict) -> str:
    name = esc(a["symbol"] or short(a["mint"]))
    if a["is_quote"]:
        return (f"<b>${name}</b> is a quote asset.\n\nEvery swap on this chain passes through it, "
                "so holdings and buys here are plumbing, not conviction. Nothing to read.")
    if not a["holders"] and not a["flow"]:
        return (f"<b>${name}</b>\n<code>{esc(a['mint'])}</code>\n\n"
                "Nobody on the watchlist holds this or has traded it. That is a real answer: "
                "no smart money we track is in this name.")
    out = [f"<b>${name}</b> · conviction {a['conviction']:.1f}", ""]
    out.append(rows([
        ("holders", str(len(a["holders"]))),
        ("of them 60+", str(a["trusted_holders"])),
        ("average score", f"{a['avg_score']:.0f}" if a["avg_score"] else "—"),
        ("cohort cost", analyze.usd(a["cohort_cost"])),
        ("open PnL", analyze.usd(a["cohort_pnl"])),
        (f"bought {a['hours']}h", analyze.usd(a["bought_usd"])),
        (f"sold {a['hours']}h", analyze.usd(a["sold_usd"])),
        ("liquidity", analyze.usd(a["liquidity_usd"])),
    ]))
    if a["holders"]:
        out.append("<b>Held by</b>")
        body = "\n".join(
            f"{(h['handle'] or short(h['address'])):<18}{str(h['score'] or '--'):>3}"
            f"{analyze.usd(h['pnl']):>10}"
            for h in a["holders"][:8])
        out.append(f"<pre>{esc(body)}</pre>")
    # What they said. Everything above is inferred from the tape; this is the trader talking, so it
    # is set as a quotation rather than folded into the readout block.
    for th in (a.get("theses") or [])[:3]:
        who = esc(th["handle"] or short(th["address"] or ""))
        mark = " · dev" if th["is_dev"] else ""
        out.append(f"\n<b>{who}</b> <i>{th['score']}{mark}</i>\n"
                   f"<blockquote>{esc(th['text'][:400])}</blockquote>")
    out.append(f"\n<code>{esc(a['mint'])}</code>")
    return "\n".join(out)


def fmt_trader(a: dict) -> str:
    mark = {"active": "◤", "watch": "◈", "dropped": "◣"}.get(a["status"], "·")
    out = [f"{mark} <b>{esc(a['handle'] or short(a['address']))}</b> · "
           f"{a['score']} · {esc(a['status'])}"]
    if a["style"] or a["red_flags"]:
        tags = ", ".join(a["style"]) + ("  ⚠ " + ", ".join(a["red_flags"]) if a["red_flags"] else "")
        out.append(f"<i>{esc(tags)}</i>")
    if a["summary"]:
        out.append(f"\n{esc(a['summary'])}")
    out.append("")
    # A win rate is only stated once there are enough decided trades behind it to mean anything.
    rated = a["round_trips"] >= 5 and a["win_rate"] is not None
    out.append(rows([
        ("fomo 30d", analyze.usd(a["fomo_pnl"])),
        ("open names", str(len(a["positions"]))),
        ("open PnL", analyze.usd(a["open_pnl"])),
        ("realised", analyze.usd(a["realized_usd"]) if a["realized_usd"] is not None else "—"),
        ("round trips", f"{a['wins']}/{a['round_trips']}" if rated else str(a["round_trips"] or "—")),
        (f"bought {a['hours']}h", analyze.usd(a["bought_usd"])),
        (f"sold {a['hours']}h", analyze.usd(a["sold_usd"])),
    ]))
    if a["positions"]:
        out.append("<b>Largest positions</b>")
        lines = []
        for p in a["positions"][:6]:
            cost, pnl = p.get("cost"), p.get("pnl")
            mult = f"  {(cost + pnl) / cost:.1f}x" if cost and pnl is not None else ""
            lines.append(f"{p['sym'][:14]:<14}{analyze.usd(pnl):>10}{mult}")
        out.append(f"<pre>{esc(chr(10).join(lines))}</pre>")
    if a["closed"]:
        out.append("<b>What came back out</b>")
        lines = []
        for p in a["closed"][:5]:
            exit_at = "all" if p["state"] == "closed" else f"{(p['exit_pct'] or 0) * 100:.0f}%"
            lines.append(f"{p['sym'][:14]:<14}{analyze.usd(p['realized']):>10}  {exit_at:>4}")
        out.append(f"<pre>{esc(chr(10).join(lines))}</pre>")
    out.append(f"<code>{esc(a['address'])}</code>")
    return "\n".join(out)


def fmt_leaderboard(board: list[dict], status: str) -> str:
    if not board:
        return "Nothing scored yet."
    label = {"active": "FOLLOW", "watch": "WATCH", "dropped": "DROPPED"}.get(status, status.upper())
    out = [f"<b>LEADERBOARD · {label}</b>",
           "<i>ranked by judgement, not by headline PnL</i>", ""]
    body = "\n".join(
        f"{i:>2}. {(r['handle'] or short(r['address']))[:17]:<18}{r['score']:>3}"
        f"{analyze.usd(r['fomo_pnl']):>9}"
        for i, r in enumerate(board, 1))
    out.append(f"<pre>{esc(body)}</pre>")
    out.append("Send a handle for the full verdict.")
    return "\n".join(out)


# The /start masthead, rendered by assets/brand/make_brand.py from the same mark the site uses.
START_BANNER = pathlib.Path(__file__).resolve().parent.parent / "assets" / "brand" / "tg-start-1280x640.png"
CAPTION_LIMIT = 1024   # Telegram's, on a photo caption

HELP = """<b>FOMO ROBINHOOD RADAR</b>
<i>which fomo.family traders on Robinhood Chain actually know what they are doing</i>

<b>Send me anything:</b>
· a token address → who holds it, at what cost, and what they said
· a trader's handle → the verdict and their open book

<b>Or ask the feeds:</b>
/hot — several trusted wallets entering one token inside minutes
/signals — what trusted wallets bought today
/fresh — launches they are entering early
/exits — where they are getting out
/top — the leaderboard, ranked by judgement

<b>Alerts</b> are on for this chat: bursts the moment they form, launches while they are still
early, and one digest a day. /stop turns them off, /start turns them back on.

Research, not financial advice."""

HELP_PRO = """<b>FOMO ROBINHOOD RADAR</b>
<i>which fomo.family traders on Robinhood Chain actually know what they are doing</i>

<b>Send me anything:</b>
· a token address → who holds it, at what cost, and what they said
· a trader's handle → the verdict and their open book

<b>Free:</b> /top — the leaderboard, ranked by judgement — and one digest a day.

<b>PRO — the alerts and the live feeds:</b>
· a <b>burst</b> the moment it forms, a <b>launch</b> while it is still early
· /hot /signals /fresh /exits whenever you ask
/pro — {days} days for ${usd}, paid in ${symbol} and burned. {state}

Research, not financial advice."""


def help_text(conn, chat_id, now: int | None = None) -> str:
    """The help, with the PRO line in it once there is a gate: what is free, what is not, and
    where this chat stands."""
    if not pro.enabled():
        return HELP
    now = now or db.now()
    end = pro.paid_until(conn, chat_id)
    if end and end > now:
        state = f"This chat: PRO until {pro._date(end)}."
    elif pro.entitled(conn, chat_id, now):
        state = f"This chat: free until {pro._date(settings.pro_grace_until)}, then /pro."
    else:
        state = "This chat: free tier."
    return HELP_PRO.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                           symbol=settings.pro_token_symbol, state=state)


PRO_ONLY = "This feed is <b>PRO</b>. /pro — {days} days for ${usd} in ${symbol}, burned. Free: /top and the daily digest."


def fmt_quote(q: dict, now: int | None = None) -> str:
    """The exact amount, the address, the deadline, the page — in that order, each on its own
    line, so a phone can long-press any one of them."""
    now = now or db.now()
    end = pro.paid_until(q.get("_conn"), q["chat_id"]) if q.get("_conn") else None
    mins = max(1, (q["expires_at"] - now) // 60)
    lines = [f"<b>PRO · {settings.pro_days} DAYS · ${q['usd']:g} IN ${esc(settings.pro_token_symbol)}</b>", ""]
    if end and end > now:
        lines.append(f"PRO is on until {pro._date(end)}. Paying again adds {settings.pro_days} days.\n")
    lines += [
        "Send <b>exactly</b>",
        f"<code>{q['tokens']}</code> ${esc(settings.pro_token_symbol)}",
        "to",
        f"<code>{esc(settings.pro_burn_address)}</code>",
        f"within {mins} minutes, from any wallet on Robinhood Chain.",
        "",
        "The amount is the receipt: it is how I know it was you. It is burned on arrival, "
        "and I confirm here within a minute. Nothing is refundable.",
    ]
    if settings.public_site_url:
        lines.append(f"\nThe same on a page, with copy buttons and a live status: "
                     f"{settings.public_site_url}/pro/{q['code']}")
    lines.append(f"\nSent a different amount? <code>/claim tx-hash</code>. "
                 f"Price now: ${q['price']:.8g} per ${esc(settings.pro_token_symbol)}.")
    return "\n".join(lines)


def fmt_notice(kind: str, end: int | None) -> str:
    when = pro._date(end)
    if kind == "expired":
        return (f"PRO ended {when}. The alerts and the live feeds are off for this chat; "
                f"/top and the digest stay. /pro to come back — {settings.pro_days} days for "
                f"${settings.pro_price_usd:g} in ${settings.pro_token_symbol}.")
    left = "three days" if kind == "3d" else "one day"
    return f"PRO ends in {left} ({when}). /pro to add {settings.pro_days} days; paying early loses nothing, the days stack."

# what the menu button offers - the short list, in the order somebody new would want it
COMMANDS = [
    ("hot", "several trusted wallets entering one token right now"),
    ("signals", "what trusted wallets bought today"),
    ("fresh", "launches the cohort is entering early"),
    ("exits", "where the cohort is getting out"),
    ("top", "the leaderboard, by judgement"),
    ("pro", "the alerts and the live feeds, paid in the token"),
    ("apikey", "a PRO key for the API: 600 requests a minute"),
    ("stop", "stop the alerts"),
    ("help", "what this is and what to send"),
]


# ---------------------------------------------------------------- subscriptions

def subscribe(conn, chat_id, username: str | None, min_conviction: float | None = None,
              backlog: bool = False) -> bool:
    """Returns True when this is a new subscriber rather than a repeat.

    A new chat starts from now: whatever is due at this moment is marked as already sent to it,
    so its first push is the next launch, not the one from an hour ago. Two hundred people joined
    in an afternoon once and every one of them was handed the same three-hour-old launch as if it
    had just happened; the burst on that token had gone out to the six who were there when it
    burst. `backlog=True` keeps the old behaviour for a test that wants the queue.
    """
    existed = conn.execute("SELECT 1 FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO bot_subscribers(chat_id, username, min_conviction, subscribed_at, active) "
            "VALUES(?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET "
            "  username=excluded.username, min_conviction=excluded.min_conviction, active=1",
            (str(chat_id), username, min_conviction if min_conviction is not None
             else settings.telegram_min_conviction, db.now()),
        )
    if existed is None and not backlog:
        try:
            for _, t, _ in due(conn):
                mark_sent(conn, chat_id, t["mint"])
        except Exception:  # noqa: BLE001 - a feed that cannot answer must not block a subscription
            log.warning("could not quiet the backlog for %s", chat_id, exc_info=True)
    if existed is None and pro.enabled() and settings.pro_trial_days > 0:
        pro.grant(conn, chat_id, settings.pro_trial_days)
    return existed is None


def unsubscribe(conn, chat_id) -> None:
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))


def subscribers(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM bot_subscribers WHERE active=1")]


def gone(e: Exception) -> bool:
    """A send that will never succeed again: blocked, deleted, kicked, or a chat that is not
    there. Every one of these is a 403 or a 400 with a permanent reason; a network hiccup or a
    429 is not, and those are retried next time."""
    s = str(e)
    return ("Forbidden" in s or "bot was blocked" in s or "chat not found" in s
            or "user is deactivated" in s or "bot was kicked" in s)


def already_sent(conn, chat_id, mint: str, within_s: int) -> bool:
    row = conn.execute(
        "SELECT ts FROM bot_sent WHERE chat_id=? AND mint=? ORDER BY ts DESC LIMIT 1",
        (str(chat_id), mint),
    ).fetchone()
    return bool(row) and (db.now() - row["ts"]) < within_s


def mark_sent(conn, chat_id, mint: str) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?,?,?)",
                     (str(chat_id), mint, db.now()))


def due(conn, now: int | None = None) -> list[tuple[str, dict, str]]:
    """(kind, token, message) for everything worth pushing right now: launches, while they are.

    The signal feed is not pushed any more. Measured over a day of pushes it went 0 for 5: by the
    time four wallets scoring 80 are in, the move is done, and a message that arrives after the
    move is noise with a good name. Launches went 5 of 8 to 2x and bursts are pushed by the
    watcher within seconds, so those two are the alerts. /signals still answers when asked.

    A launch is a push only while it is one: `telegram_launch_max_age_min` after the first
    trusted wallet went in it drops out of the queue, however hot it reads, so a subscriber who
    joins at four o'clock is not told about noon. And only while the cohort is still buying:
    once the last trusted buy is `telegram_launch_max_gap_s` old, the heat is history - every
    launch pushed more than two minutes after the cohort's last buy went nowhere.
    """
    chain = settings.dex_chains[0] if settings.dex_chains else None
    hours = settings.telegram_alert_window_h
    now = now or db.now()
    cutoff = now - settings.telegram_launch_max_age_min * 60
    stale = now - settings.telegram_launch_max_gap_s
    from .pipeline.safety import check as sell_check

    out = []
    for t in analyze.fresh(conn, chain, hours=hours, limit=10)["tokens"]:
        if t["heat"] >= settings.telegram_min_heat and (t.get("first_ts") or now) >= cutoff \
                and (t.get("last_ts") or now) >= stale:
            # a launch nobody can leave is not a launch: asked of the chain before the message goes
            verdict = sell_check(conn, t["mint"], now=now)
            if verdict["sellable"] == 0:
                log.warning("launch %s not pushed: %s", t["sym"], verdict["note"])
                continue
            t["clones"] = analyze.namesakes(conn, t.get("sym"), t["mint"], now)
            out.append(("launch", t, fmt_launch(t)))
    return out


class Burns:
    """The bot's own look at the burn address, on the alert timer: the watcher sees a burn
    within a tick when the RPC lets it, and this is the second pair of eyes for when it does
    not. Lazy: no RPC client until there is a gate, none of the allowance until a quote waits."""

    def __init__(self) -> None:
        self.rpc = None
        self.cursor = pro.Cursor()

    def look(self, conn) -> int:
        if not pro.enabled() or not pro.waiting(conn):
            return 0
        if self.rpc is None:
            from .sources.rpc import RobinhoodRPC
            self.rpc = RobinhoodRPC()
            self.rpc.limiter.max = max(2, settings.watch_rpc_max_per_min // 3)
        head, _ = self.rpc.head()
        paid = self.cursor.advance(conn, self.rpc, head)
        if paid:
            from .pipeline.watch import confirm
            confirm(conn, paid)
        return len(paid)


def fmt_followup(row, m: dict) -> str:
    """An hour on. The peak and when it came, where the price is, what traded: the same three
    facts every time, so that the reader learns the shape of these things."""
    mark = "\u25b2" if row["kind"] == "burst" else "\u25c6"
    head = f"{mark} {token_link(row['mint'], row['sym'])} \u00b7 {settings.telegram_followup_min} min after the push"
    if m.get("best") is None:
        return head + "\n\nNo trade in the pool since the push."
    x = lambda v: "\u2014" if v is None else f"\u00d7{v:.2f}"  # noqa: E731
    peak = x(m["best"]) + (f"  at +{m['peak_min']} min" if m.get("peak_min") is not None else "")
    return head + "\n\n" + rows([
        ("peak", peak),
        ("now", x(m.get("now"))),
        ("traded", analyze.usd(m.get("vol")) if m.get("vol") else "\u2014"),
    ]) + ("\n(from the tape only)" if m.get("witness") == "tape" else "")


def followups(conn, tg: Telegram, now: int | None = None) -> int:
    """Every push whose hour is up: measure, tell the chats that got it, close the row."""
    now = now or db.now()
    sent = 0
    for row in pushes.due(conn, now):
        m = pushes.measure(conn, row, now=now)
        if m is None:
            continue   # the screener was busy; next minute
        text = fmt_followup(row, m)
        for chat_id in pushes.recipients(conn, row):
            try:
                tg.send(chat_id, text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("followup to %s failed: %s", chat_id, e)
                if gone(e):
                    unsubscribe(conn, chat_id)
        pushes.close(conn, row["id"], m, now)
        log.info("followup %s %s: %s", row["kind"], row["sym"], m)
        try:
            from .pipeline import xpost
            xpost.reply_followup(conn, row["id"], m, now=now)
        except Exception as e:  # noqa: BLE001
            log.warning("x reply failed: %s", e)
    return sent


def notify(conn, tg: Telegram) -> int:
    """PRO ending soon, or just ended: one line each, once per paid period."""
    sent = 0
    for chat_id, kind in pro.reminders(conn):
        try:
            tg.send(chat_id, fmt_notice(kind, pro.paid_until(conn, chat_id)))
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("pro notice to %s failed: %s", chat_id, e)
            if gone(e):
                unsubscribe(conn, chat_id)
    return sent


def broadcast(conn, tg: Telegram) -> dict:
    """Push everything each subscriber has not already been told about."""
    stats = {"subscribers": 0, "sent": 0, "launches": 0, "skipped": 0, "errors": 0}
    subs = subscribers(conn)
    if not subs:
        return stats
    stats["subscribers"] = len(subs)
    items = due(conn)
    quiet = settings.telegram_realert_hours * 3600
    now = db.now()
    told: dict[str, int] = {}
    for sub in subs:
        if not pro.entitled_row(sub, now):
            continue
        for kind, t, text in items:
            if already_sent(conn, sub["chat_id"], t["mint"], quiet) \
                    or already_sent(conn, sub["chat_id"], f"hot:{t['mint']}", settings.telegram_dedupe_s):
                stats["skipped"] += 1   # told already, or told of the burst on it minutes ago
                continue
            try:
                tg.send(sub["chat_id"], text)
                mark_sent(conn, sub["chat_id"], t["mint"])
                stats["sent"] += 1
                stats["launches"] += kind == "launch"
                told[t["mint"]] = told.get(t["mint"], 0) + 1
            except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                stats["errors"] += 1
                log.warning("send to %s failed: %s", sub["chat_id"], e)
                if gone(e):
                    unsubscribe(conn, sub["chat_id"])
    for kind, t, _ in items:
        if told.get(t["mint"]):
            pushes.record(conn, kind, t, told[t["mint"]], now)
    if stats["sent"]:
        log.info("broadcast: %s", stats)
    return stats


# ---------------------------------------------------------------- commands

def status_text(conn, chat_id=None) -> str:
    def q(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    out = "<b>DATABASE</b>\n" + rows([
        ("traders", f"{q('SELECT COUNT(*) FROM traders'):,}"),
        ("scored", f"{q('SELECT COUNT(*) FROM traders WHERE score IS NOT NULL'):,}"),
        ("follow", f"{q('SELECT COUNT(*) FROM traders WHERE status=?', 'active'):,}"),
        ("fills", f"{q('SELECT COUNT(*) FROM trades'):,}"),
        ("positions", f"{q('SELECT COUNT(*) FROM fomo_positions'):,}"),
        ("subscribers", f"{q('SELECT COUNT(*) FROM bot_subscribers WHERE active=1'):,}"),
    ], width=15)
    if chat_id is not None and pro.enabled():
        end = pro.paid_until(conn, chat_id)
        now = db.now()
        if end and end > now:
            line = f"PRO until {pro._date(end)} ({(end - now) // 86400} days left)"
        elif pro.entitled(conn, chat_id, now):
            line = f"free until {pro._date(settings.pro_grace_until)}"
        else:
            line = "free tier — /pro for the alerts and the live feeds"
        out += f"\n\n<b>THIS CHAT</b>\n{line}"
    return out


def handle_text(conn, text: str, chat_id, username: str | None) -> str:
    """Route one message to its answer. Pure enough to test without a network."""
    text = (text or "").strip()
    if not text:
        return HELP
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]

    # /token_0xabc… — the deep link the signal message offers
    if cmd.startswith("/token_"):
        cmd, args = "/token", [cmd[len("/token_"):]]

    if cmd == "/start":
        # Starting is joining. Three of four thousand visitors found /subscribe on the first
        # day; the alerts are the product, and asking people to opt in twice is how a bot with
        # a hundred readers has three subscribers.
        subscribe(conn, chat_id, username)
        if args and args[0].lower() == "pro" and pro.enabled():
            return handle_text(conn, "/pro", chat_id, username)   # the site's button: t.me/<bot>?start=pro
        return help_text(conn, chat_id)
    if cmd == "/help":
        return help_text(conn, chat_id)
    if cmd == "/status":
        return status_text(conn, chat_id)
    if cmd == "/pro":
        if not pro.enabled():
            return "There is no PRO tier yet: everything here is open. /help"
        q = pro.quote(conn, chat_id)
        if q is None:
            return "No price for the token right now, so no quote. Try again in a minute."
        q["_conn"] = conn
        return fmt_quote(q)
    if cmd == "/apikey":
        if not pro.enabled() or settings.api_key_rate_per_min <= 0:
            return "There are no API keys yet: the API is open at 120 a minute. /help"
        if not pro.entitled(conn, chat_id):
            return ("An API key is <b>PRO</b>: " + PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                    symbol=esc(settings.pro_token_symbol)).replace("This feed is <b>PRO</b>. ", ""))
        from .pipeline import keys
        had = keys.current(conn, chat_id)
        key = keys.issue(conn, chat_id)
        return (("Your previous key is revoked. " if had else "") +
                f"Your API key:\n<code>{key}</code>\n\n"
                f"Send it as the <code>X-API-Key</code> header for {settings.api_key_rate_per_min} requests a minute "
                f"(120 without). It works while this chat is PRO. Keep it to yourself; /apikey again replaces it. "
                f"Docs: {settings.public_site_url or ''}/docs")
    if cmd == "/claim":
        if not pro.enabled():
            return "There is nothing to claim: everything here is open. /help"
        if not args:
            return "Send it as <code>/claim 0x…</code> with the transaction hash of your burn."
        ok, why = pro.claim(conn, chat_id, args[0])
        return ("✓ " if ok else "") + esc(why)
    if cmd in ("/signals", "/hot", "/fresh", "/exits") and not pro.entitled(conn, chat_id):
        return PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                               symbol=esc(settings.pro_token_symbol))
    if cmd == "/health":
        from .pipeline.health import report

        return fmt_health(report(conn))
    if cmd == "/signals":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_signals(analyze.signals(conn, chain, hours=hours, limit=10), hours)
    if cmd == "/hot":
        from .pipeline.hot import hot_now

        mins = int(args[0]) if args and args[0].isdigit() else settings.hot_window_min
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_hot_list(hot_now(conn, chain, delta=settings.hot_delta, window_s=mins * 60,
                                    min_wallets=settings.hot_min_wallets,
                                    max_age_s=settings.hot_max_age_h * 3600 or None), mins)
    if cmd == "/fresh":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_fresh(analyze.fresh(conn, chain, hours=hours, limit=10))
    if cmd == "/digest":
        from .pipeline.digest import daily
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_digest(daily(conn, hours=24, chain=chain))
    if cmd == "/exits":
        hours = int(args[0]) if args and args[0].isdigit() else 6
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_exits(analyze.exits(conn, chain, hours=hours, limit=10), hours)
    if cmd in ("/top", "/watch", "/dropped"):
        status = {"/top": "active", "/watch": "watch", "/dropped": "dropped"}[cmd]
        n = int(args[0]) if args and args[0].isdigit() else 15
        return fmt_leaderboard(analyze.leaderboard(conn, min(n, 40), status), status)
    if cmd == "/subscribe":
        # a number after it used to set a conviction floor for signal pushes; signals are not
        # pushed any more, so it is accepted and means nothing
        floor = float(args[0]) if args and args[0].replace(".", "", 1).isdigit() else None
        fresh = subscribe(conn, chat_id, username, floor)
        if pro.enabled() and not pro.entitled(conn, chat_id):
            return ("You are in. The alerts and the live feeds are <b>PRO</b>: " +
                    PRO_ONLY.format(days=settings.pro_days, usd=f"{settings.pro_price_usd:g}", symbol=esc(settings.pro_token_symbol))
                    .replace("This feed is <b>PRO</b>. ", ""))
        return (("Alerts are on." if fresh else "Alerts were already on.") +
                " You will hear about:\n"
                "· a <b>burst</b> — several trusted wallets entering one token inside minutes, "
                "sent within seconds of the fill\n"
                "· a <b>launch</b> the cohort is entering in its first minutes, while it still is\n"
                "· one digest a day\n\n"
                "A handful a day, none of them old. /stop for none; "
                "/signals, /hot, /fresh and /exits answer whenever you ask.")
    if cmd in ("/stop", "/unsubscribe", "/mute"):
        unsubscribe(conn, chat_id)
        return "Alerts are off for this chat. /start turns them back on; the feeds still answer."
    if cmd == "/token":
        if not args:
            return "Send it as <code>/token 0x…</code>, or just paste the address."
        return fmt_token(analyze.analyze_token(conn, args[0]))
    if cmd == "/trader":
        if not args:
            return "Send it as <code>/trader unipcs</code>, or just send the handle."
        a = analyze.analyze_trader(conn, args[0])
        return fmt_trader(a) if a else f"No trader matches <b>{esc(args[0])}</b>."
    if cmd.startswith("/"):
        return f"Unknown command {esc(cmd)}.\n\n" + HELP

    # bare text: an address is a token, anything else is a handle
    if text.startswith("0x") and len(text) == ADDRESS_LEN:
        a = analyze.analyze_trader(conn, text)
        return fmt_trader(a) if a else fmt_token(analyze.analyze_token(conn, text))
    a = analyze.analyze_trader(conn, text)
    if a:
        return fmt_trader(a)
    return (f"No trader called <b>{esc(text)}</b>, and that is not a token address.\n\n"
            "Send a 0x… address for a token, a handle for a trader, or /signals.")


def is_start(text: str) -> bool:
    """`/start`, `/start@thebot`, and the deep-linked `/start ref=x` Telegram sends from a link."""
    return text.strip().split(" ")[0].split("@")[0] == "/start"


def handle_update(conn, tg: Telegram, update: dict) -> bool:
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat is None or not msg.get("text"):
        return False
    user = (msg.get("from") or {}).get("username")
    try:
        answer = handle_text(conn, msg["text"], chat, user)
    except Exception as e:  # noqa: BLE001 - a bad question must not kill the bot
        log.exception("handling %r failed", msg.get("text"))
        answer = f"That broke something: <code>{esc(type(e).__name__)}</code>. Try /help."

    # The first thing a new chat sees is the masthead, with the help as its caption — one message,
    # not two. Anything at all going wrong here falls back to the text: a missing file or a
    # rejected upload must not be the reason someone's first /start answers nothing.
    if is_start(msg["text"]) and START_BANNER.exists() and len(answer) <= CAPTION_LIMIT:
        try:
            tg.photo(chat, START_BANNER, answer)
            return True
        except Exception:  # noqa: BLE001
            log.warning("start banner failed, sending the text alone", exc_info=True)

    tg.send(chat, answer)
    return True


def run(conn, tg: Telegram | None = None, once: bool = False) -> dict:
    """Long-poll for questions and push signals on a timer, in one loop."""
    tg = tg or Telegram()
    who = tg.me()
    log.info("bot @%s online", who.get("username"))
    try:
        tg.set_commands(COMMANDS)
    except Exception as e:  # noqa: BLE001 - a menu that failed to register is not a bot that is down
        log.warning("could not register the command menu: %s", e)
    stats = {"handled": 0, "broadcasts": 0, "sent": 0}
    # -inf rather than 0: monotonic() counts from boot, and on a machine up for less than the
    # alert interval - a fresh CI runner, a just-rebooted server - zero would mean waiting
    # out the interval before the first broadcast instead of sending it at once
    offset, last_alert = 0, float("-inf")
    burns = Burns()
    while True:
        try:
            for u in tg.updates(offset) or []:
                offset = max(offset, u["update_id"] + 1)
                stats["handled"] += handle_update(conn, tg, u)
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001 - a dropped poll is normal, keep going
            log.warning("poll failed: %s", e)
            time.sleep(5)

        if time.monotonic() - last_alert >= settings.telegram_alert_interval_s:
            last_alert = time.monotonic()
            try:
                stats["sent"] += broadcast(conn, tg)["sent"]
                stats["broadcasts"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("broadcast failed: %s", e)
            try:
                notify(conn, tg)
            except Exception as e:  # noqa: BLE001
                log.warning("pro notices failed: %s", e)
            try:
                burns.look(conn)
            except Exception as e:  # noqa: BLE001 - the watcher is the first pair of eyes
                log.warning("pro burn check failed: %s", e)
            try:
                followups(conn, tg)
            except Exception as e:  # noqa: BLE001
                log.warning("followups failed: %s", e)
            try:
                from .pipeline import xpost
                xpost.send_due(conn)
            except Exception as e:  # noqa: BLE001 - X being down is not the bot being down
                log.warning("x posting failed: %s", e)
        if once:
            return stats
