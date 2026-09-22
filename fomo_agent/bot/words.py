"""What a message looks like: every formatter the bot has, and the small helpers they share.

Messages are formatted as instrument readouts, per the design system: a monospace block with
aligned columns, no decoration, nothing that has to be scrolled to read on a phone. Nothing here
touches the network - a formatter takes what was measured and returns text.
"""
from __future__ import annotations

import html
import pathlib
import time

from .. import db
from .. import links as links_
from ..config import settings
from ..pipeline import analyze, pro, pushes

ADDRESS_LEN = 42  # 0x + 40 hex


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
    hs, ss = who_line.parts(handles), who_line.parts(scores)
    pairs = [f"{esc(h)} {esc(s)}" for h, s in zip(hs, ss)][:limit]
    tail = f" +{len(hs) - limit}" if len(hs) > limit else ""
    return " · ".join(pairs) + tail


def _parts(v):
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None]
    return [x for x in (v or "").split(",") if x]


who_line.parts = _parts


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
    """The contract, tappable to copy, and the two places worth opening it in - the buy first."""
    links = [f'<a href="{esc(links_.fomo_token(mint))}">buy on fomo</a>']
    if settings.public_site_url:
        links.append(f'<a href="{esc(links_.site_token(mint))}">breakdown</a>')
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


def name_line(t: dict, now: int | None = None) -> str | None:
    """'⚠ $ZEC is a listed ticker; this is a Robinhood Chain token wearing its name; the 4th $ZEC
    on this chain in 14 days · 0x824c… 3d ago · 0xd113… 11d ago' - or nothing. Beside the 24-hour
    clone line, not instead of it: that one names the pushes and the honeypots of the day."""
    b = t.get("borrowed")
    if not b:
        return None
    now = now or db.now()
    earlier = [c for c in b.get("earlier") or [] if now - (c.get("t0") or now) >= 86400]
    tail = " \u00b7 ".join(f"{short(c['mint'])} {ago(c['t0'], now)} ago" + (" honeypot" if c.get("unsellable") else "") for c in earlier[-3:])
    return f"\u26a0 {esc(b['why'])}" + (f" \u00b7 {tail}" if tail else "")


def share_row(t: dict) -> list[tuple[str, str]]:
    """'of the pool's hour   4%' - how much of the last hour's volume the cohort is. Near all of
    it, the cohort made the market; a sliver, it walked into one the crowd made."""
    s = t.get("cohort_share")
    if s is None:
        return []
    return [("of the pool's hour", f"{s:.0%}")]


def follow_line(t: dict) -> str | None:
    """`follow the best of them: /follow_unipcs` - the highest-scored wallet in, one tap.

    Two chats in four hundred used /follow in its first three days; the names were in every push
    all along, and the command was a screen away. A handle that is not a command's alphabet gets
    the two-word form instead."""
    hs, ss = who_line.parts(t.get("who")), who_line.parts(t.get("scores"))
    best = None
    for h, sc in zip(hs, ss):
        try:
            v = float(sc)
        except (TypeError, ValueError):
            continue
        if h and not h.startswith("0x") and (best is None or v > best[1]):
            best = (h, v)
    if best is None:
        return None
    h = best[0]
    cmd = f"/follow_{h}" if h.replace("_", "").isalnum() and h.isascii() else f"/follow {esc(h)}"
    return f"follow the best of them: {cmd}"


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
        *share_row(h),
    ]))
    out.append(who_line(h.get("who"), h.get("scores")))
    if follow_line(h):
        out.append(follow_line(h))
    for line in (clone_line(h, now), name_line(h, now)):
        if line:
            out.append(line)
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
    from ..pipeline.digest import is_quiet

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


def launch_record(conn, days: int = 30) -> str | None:
    """One line under the launch feed: what a hundred dollars into every launch has done lately.

    These are not pushed to anybody, which is a claim about them; the claim should carry its
    evidence in the same message rather than on a page nobody opens.
    """
    try:
        from ..pipeline import record

        t = record.report(conn, days=days, kind="launch", sent_only=False)["totals"]
        p = t["paper"]
        if not p["trades"]:
            return None
        money = f"{'+' if p['pnl'] >= 0 else '-'}${abs(p['pnl']):,.0f}"
        return (f"<i>These are not alerts: over {days} days, $100 into every launch was {money} at the "
                f"{settings.telegram_followup_min}-minute read, {round(p['win_rate'] * 100)}% of them above the call. "
                f"The alerts are the bursts. /record</i>")
    except Exception:  # noqa: BLE001 - a feed without its footnote is still the feed
        return None


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
        *share_row(t),
    ]))
    out.append(who_line(t.get("who"), t.get("scores")))
    if follow_line(t):
        out.append(follow_line(t))
    for line in (clone_line(t, now), name_line(t, now)):
        if line:
            out.append(line)
    out.append("")
    out.extend(token_lines(t["mint"]))
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
    if a.get("distributing") and "distributing" not in (a["summary"] or ""):
        out.append(f"\u26a0 {esc(a['distributing'])}")
    if a.get("pnl_gap"):
        g = a["pnl_gap"]
        out.append(f"fomo says {analyze.usd(g['fomo'])}, the indexer's realised {analyze.usd(g['indexer'])}: "
                   f"{analyze.usd(abs(g['gap']))} apart. fomo counts open positions; the indexer what was closed.")
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
# the repo root is two parents up from this file now that the bot is a package
START_BANNER = pathlib.Path(__file__).resolve().parents[2] / "assets" / "brand" / "tg-start-1280x640.png"
CAPTION_LIMIT = 1024   # Telegram's, on a photo caption

HELP = """<b>FOMO ROBINHOOD RADAR</b>
<i>which fomo.family traders on Robinhood Chain actually know what they are doing</i>

<b>Send me anything:</b>
· a token address → who holds it, at what cost, and what they said
· a trader's handle → the verdict and their open book

<b>Or ask the feeds:</b>
/hot — several trusted wallets entering one token inside minutes
/launches — what the cohort is entering early (not alerts — see their record)
/signals — bought today · /exits — where they are getting out
/top — the leaderboard · /record — every alert and what came of it · /paper
/follow &lt;handle&gt; — that wallet's every fill, within a tick of the chain
/invite — your link; /settings — what this chat hears

<b>Alerts</b> are on and free: a burst the moment it forms — several trusted wallets into one
token inside minutes — and one digest a day. /stop turns them off.

Every alert carries a <b>buy on fomo</b> link. Not on fomo.family yet? {fomo}

Research, not financial advice."""

HELP_PRO = """<b>FOMO ROBINHOOD RADAR</b>
<i>which fomo.family traders on Robinhood Chain actually know what they are doing</i>

<b>Send me anything:</b>
· a token address → who holds it, at what cost, and what they said
· a trader's handle → the verdict and their open book

<b>Free, for everybody:</b>
· a <b>burst</b> alert the moment it forms — the half of the feed that pays for itself
· /hot and /launches whenever you ask · /top · /record · /paper · one digest a day

<b>PRO:</b>
· /follow up to {follows} wallets, every fill within a tick of the chain (free: {follows_free})
· /signals and /exits — the cohort's whole book, live
· /apikey and /webhook — every alert POSTed to your own URL as it goes out, 600 reads a minute
/invite — your link, a week of PRO for both · /settings — what this chat hears
/pro — {days} days for ${usd}, paid in ${symbol} and burned. {state}

Every alert carries a <b>buy on fomo</b> link. Not on fomo.family yet? {fomo}

Research, not financial advice."""


def help_text(conn, chat_id, now: int | None = None) -> str:
    """The help, with the PRO line in it once there is a gate: what is free, what is not, and
    where this chat stands."""
    if not pro.enabled():
        return HELP.format(fomo=links_.fomo_home())
    now = now or db.now()
    end = pro.paid_until(conn, chat_id)
    if end and end > now:
        state = f"This chat: PRO until {pro._date(end)}."
    elif pro.entitled(conn, chat_id, now):
        state = f"This chat: free until {pro._date(settings.pro_grace_until)}, then /pro."
    else:
        state = "This chat: free tier."
    return HELP_PRO.format(fomo=links_.fomo_home(), follows=settings.follow_pro_max, follows_free=settings.follow_free_max, days=settings.pro_days, usd=f"{settings.pro_price_usd:g}",
                           symbol=settings.pro_token_symbol, state=state)


PRO_ONLY = ("This feed is <b>PRO</b>. /pro — {days} days for ${usd} in ${symbol}, burned. "
            "Free, always: the burst alerts, /hot, /launches, /top, /record and the daily digest.")


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
    ("launches", "tokens the cohort is entering early - not alerts"),
    ("signals", "what trusted wallets bought today"),
    ("exits", "where the cohort is getting out"),
    ("top", "the leaderboard, by judgement"),
    ("follow", "a wallet's every fill, within a tick: /follow <handle>"),
    ("invite", "your invite link: a week of PRO for both"),
    ("settings", "what this chat hears: /alerts /minconv /quiet"),
    ("record", "every alert sent and what came of it"),
    ("paper", "$100 into every alert, out at the hour read"),
    ("pro", "/follow, /signals, /exits and the API, paid in the token"),
    ("apikey", "a PRO key for the API: 600 requests a minute"),
    ("stop", "stop the alerts"),
    ("help", "what this is and what to send"),
]


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


def fmt_void(row, note: str, now: int | None = None) -> str:
    """The push taken back: what settled it and what it means for the record. The same shape as
    the follow-up, so the reader knows the message is about a call they were sent."""
    now = now or db.now()
    sym = row["sym"] if "sym" in row.keys() and row["sym"] else None
    if sym is None:
        sym = row["mint"][:8]
    mark = "▲" if row["kind"] == "burst" else "◆"
    head = f"⚠ {mark} {token_link(row['mint'], sym)} · unsellable, {ago(row['ts'], now)} after the push"
    fact = note[0].upper() + note[1:] + ("" if note.endswith(".") else ".")
    return "\n".join([head, "", esc(fact), "Off every feed from now; the record counts it as a honeypot."])


def fmt_fill(f: dict, now: int | None = None) -> str:
    """One fill of a followed wallet: who, did what, how much, of what - and the buy link."""
    name = f.get("handle") or f["address"][:10]
    verb = "bought" if f["side"] == "buy" else "sold"
    size = analyze.usd(f.get("usd_value")) if f.get("usd_value") else "some"
    mark = "\u25b2" if f["side"] == "buy" else "\u25bd"
    head = f"{mark} <b>{esc(name)}</b>" + (f" {f['score']}" if f.get("score") is not None else "") + f" {verb} <b>{esc(size)}</b> of {token_link(f['mint'], f['sym'])} \u00b7 {ago(f['ts'], now)} ago"
    return "\n".join([head, "", *token_lines(f["mint"])])


def fmt_following(rows_: list, cap: int) -> str:
    if not rows_:
        return f"This chat follows nobody yet. /follow &lt;handle or address&gt; \u2014 up to {cap}."
    out = [f"<b>FOLLOWING</b> \u00b7 {len(rows_)} of {cap}", ""]
    for r in rows_:
        name = r["fomo_handle"] or r["address"][:10]
        out.append(f"\u00b7 {esc(name)}" + (f" {r['score']}" if r["score"] is not None else "") + (f" \u00b7 {r['status']}" if r["status"] else ""))
    out.append("\n/unfollow &lt;name&gt; or /unfollow all")
    return "\n".join(out)


def fmt_record(rep: dict) -> str:
    """The record, in a message: the totals, then the last ten alerts with their verdicts."""
    t = rep["totals"]
    x = lambda v: "\u2014" if v is None else f"\u00d7{v:.2f}"  # noqa: E731
    head = [f"<b>THE RECORD</b> \u00b7 last {rep['days']} days", ""]
    if not t["pushes"]:
        return head[0] + "\n\nNo alerts in the window."
    head.append(rows([
        ("alerts", f"{t['pushes']} ({t['bursts']} bursts, {t['launches']} launches)"),
        ("measured", f"{t['clean']}" + (f" + {t['seeded']} seeded" if t["seeded"] else "") + (f" + {t['dead']} dead" if t["dead"] else "") + (f" + {t['honeypot']} honeypot" if t["honeypot"] else "")),
        ("above entry", f"{t['above_entry']} of {t['clean']}" + (f" ({100 * t['above_entry'] // t['clean']}%)" if t["clean"] else "")),
        ("reached \u00d72", f"{t['reached_2x']} of {t['clean']}"),
        ("median peak", x(t["median_best"])),
        ("paper", f"{'+' if t['paper']['pnl'] >= 0 else '-'}${abs(t['paper']['pnl']):,.0f} on ${t['paper']['staked']:,.0f}"),
    ]))
    head.append("")
    marks = {"burst": "\u25b2", "launch": "\u25c6"}
    for r in rep["pushes"][:10]:
        peak = x(r["best"]) + (f" +{r['peak_min']}m" if r.get("peak_min") is not None else "")
        head.append(f"{marks[r['kind']]} {token_link(r['mint'], r['sym'])} \u00b7 {pushes.fmt_time(r['ts'])} \u00b7 peak {peak} \u00b7 now {x(r['now'])} \u00b7 <i>{r['verdict']}</i>")
    if len(rep["pushes"]) > 10:
        head.append(f"\u2026 and {len(rep['pushes']) - 10} more" + (f" \u2014 {settings.public_site_url}/record" if settings.public_site_url else ""))
    return "\n".join(head)


def fmt_paper(rep: dict) -> str:
    """The paper run: a hundred dollars into every alert at the entry, out at the hour read."""
    p = rep["totals"]["paper"]
    if not p["trades"]:
        return f"<b>PAPER</b> \u00b7 last {rep['days']} days\n\nNo closed alerts in the window yet."
    minus = "\u2212"
    sign = lambda v: f"{'+' if v >= 0 else minus}${abs(v):,.0f}"  # noqa: E731
    out = [f"<b>PAPER</b> \u00b7 ${p['stake']:.0f} into every alert at the entry, out at the {rep['followup_min']}-minute read \u00b7 last {rep['days']} days", ""]
    q = rep["totals"]["paper_trail"]
    out.append(rows([
        ("at the hour", f"{sign(p['pnl'])} on ${p['staked']:,.0f} ({100 * p['pnl'] / p['staked']:+.0f}%)"),
        ("trades", f"{p['trades']} \u00b7 {p['wins']} won ({100 * (p['win_rate'] or 0):.0f}%)"),
        ("best / worst", f"{sign(p['best'])} / {sign(p['worst'])}"),
        (f"trail \u2212{100 * q['drop']:.0f}%", f"{sign(q['pnl'])} on ${q['staked']:,.0f}" if q["trades"] else "\u2014"),
        ("trades", f"{q['trades']} \u00b7 {q['wins']} won ({100 * (q['win_rate'] or 0):.0f}%)" if q["trades"] else "\u2014"),
    ]))
    out.append("")
    out.append("<i>Same alerts, two ways out: sold at the hour read, or at the first pullback a fifth under the running high inside that hour.</i>")
    out.append("")
    for c in rep["curve"][-6:][::-1]:
        out.append(f"{sign(c['pnl'])} {token_link(c['mint'], c['sym'])} \u00b7 {pushes.fmt_time(c['ts'])} \u00b7 running {sign(c['total'])}")
    out.append("\nNot a strategy: the hour is where the follow-up looks. The record is what your own chat says." + (f" {settings.public_site_url}/paper" if settings.public_site_url else ""))
    return "\n".join(out)
