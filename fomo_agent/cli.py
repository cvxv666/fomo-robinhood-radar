"""CLI: discover / new-tokens / track / score / report / run."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import typer

from . import db
from .config import settings

app = typer.Typer(
    help="FOMO Robinhood Radar: discover, track and score Robinhood Chain traders.",
    no_args_is_help=True)


def _setup(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _run(kind: str, fn, *args, **kw) -> dict | None:
    """Wrap a step in a `runs` row. Errors are recorded, not raised."""
    conn = db.connect()
    rid = db.run_start(conn, kind)
    try:
        stats = fn(conn, *args, **kw)
        db.run_finish(conn, rid, stats)
        return stats
    except Exception as e:  # noqa: BLE001
        logging.getLogger(kind).error("%s failed: %s", kind, e)
        db.run_finish(conn, rid, error=repr(e)[:500])
        return None
    finally:
        conn.close()


@app.callback()
def main(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    _setup(verbose)


@app.command()
def init() -> None:
    """Create the sqlite database and print config."""
    conn = db.connect()
    conn.close()
    typer.echo(f"db: {settings.db_path.resolve()}")
    typer.echo(f"helius key: {'set' if settings.helius_api_key else 'MISSING'}")
    typer.echo(f"anthropic key: {'set' if settings.anthropic_api_key else 'MISSING'}")
    typer.echo(f"codex key: {'set' if settings.codex_api_key else 'not set (codex source skipped)'}")
    typer.echo(f"track sources: {','.join(settings.track_sources)}")
    typer.echo(f"scorer: {settings.scorer} (SCORER={settings.scorer_mode})")
    conn = db.connect()
    users, resolved = conn.execute(
        "SELECT COUNT(*), COUNT(resolved_at) FROM fomo_users"
    ).fetchone()
    conn.close()
    typer.echo(f"fomo: browser export only (Cloudflare blocks server calls) — "
               f"{users} users known, {resolved} with execution wallets. "
               f"Refresh: scripts/fomo_export.js -> cli fomo-import")
    typer.echo(f"new-token threshold: mcap>={settings.new_token_min_mcap_usd:,.0f} USD, age<={settings.new_token_max_age_hours}h, chains={','.join(settings.dex_chains)}, sources={','.join(settings.token_sources)}")
    if settings.codex_api_key:
        b = codex_budget(conn_counts=chain_counts())
        typer.echo(
            f"codex budget: ~{b['total']:,}/month of {settings.codex_monthly_request_cap:,} "
            f"(tokens {b['tokens']:,} + discovery ~{b['discovery']:,} + "
            f"tracking {b['tracking']:,} for {b.get('codex_wallets', 0)} wallets Codex must cover)"
            + ("  OVER BUDGET - raise the intervals" if b["total"] > settings.codex_monthly_request_cap else "")
        )


def chain_counts() -> dict[str, int]:
    conn = db.connect()
    try:
        return {r[0] or "solana": r[1] for r in conn.execute(
            "SELECT chain, COUNT(*) FROM traders WHERE status IN ('candidate','tracking','active','watch') GROUP BY chain")}
    finally:
        conn.close()


def codex_budget(new_tokens_per_day: int = 20, conn_counts: dict[str, int] | None = None) -> dict:
    """Rough monthly Codex request projection for the current config.

    Wallets on a chain served by a free source earlier in TRACK_SOURCES (today: trenches on
    Robinhood Chain) never reach Codex, so they are excluded from the tracking line.
    """
    from .pipeline.track import build_trackers, pick_tracker

    per_day = 86400
    tokens = round(per_day / max(settings.codex_min_interval_s, 1) * 30)
    discovery = round(min(new_tokens_per_day, settings.discover_tokens_per_pass * per_day / max(settings.new_tokens_interval, 1)) * 30)

    counts = conn_counts or {}
    on_codex = sum(counts.values())
    if counts:
        try:
            trackers = build_trackers()
            on_codex = sum(n for chain, n in counts.items()
                           if type(pick_tracker(trackers, chain)).__name__ == "Codex")
        except Exception:  # noqa: BLE001 - projection must never break `init`
            pass
    wallets_per_pass = min(settings.track_max_wallets_per_pass, on_codex) if counts else settings.track_max_wallets_per_pass
    tracking = round(per_day / max(settings.track_interval, 1) * wallets_per_pass * 30)
    return {"tokens": tokens, "discovery": discovery, "tracking": tracking,
            "codex_wallets": on_codex, "total": tokens + discovery + tracking}


@app.command()
def discover(
    leaderboard: bool = typer.Option(False, "--leaderboard", help="pull fomo leaderboard 24h/7d/30d"),
    mint: Optional[str] = typer.Option(None, "--mint", help="pull top-PnL holders of a token from fomo"),
    add: Optional[str] = typer.Option(None, "--add", help="manually add a wallet address (no fomo needed)"),
    handle: Optional[str] = typer.Option(None, "--handle"),
    chain: Optional[str] = typer.Option(None, "--chain", help="solana | base | robinhood | evm (guessed if omitted)"),
    makers: bool = typer.Option(False, "--makers", help="with --mint: pull recent buyers from Codex instead of fomo holders"),
    trenches: bool = typer.Option(False, "--trenches", help="import fomo traders from rhtrenches.com (free, no session)"),
    window: Optional[str] = typer.Option(None, "--window", help="with --trenches: 1h|24h|7d|30d|all"),
) -> None:
    """Discover candidate traders."""
    from .pipeline import discover as d

    if add:
        conn = db.connect()
        created = d.add_manual(conn, add, handle, chain)
        conn.close()
        typer.echo(f"{'added' if created else 'already known'}: {add}")
    if trenches:
        typer.echo(_run("discover_trenches", d.discover_trenches, None, window))
    if leaderboard:
        typer.echo(_run("discover_leaderboard", d.discover_leaderboard))
    if mint:
        if makers:
            typer.echo(_run("discover_makers", d.discover_makers, mint, chain or "solana"))
        else:
            from .sources.fomo import NETWORKS

            net = next((n for n, c in NETWORKS.items() if c == (chain or "solana")), 1399811149)
            typer.echo(_run("discover_holders", d.discover_holders, mint, None, net))
    if not (add or leaderboard or mint or trenches):
        typer.echo("nothing to do: use --trenches, --leaderboard, --mint [--makers] or --add")


@app.command("fomo-check")
def fomo_check() -> None:
    """Verify FOMO_SESSION against the live API and show what the leaderboard returns."""
    from .sources.fomo import FomoClient, FomoError

    try:
        client = FomoClient()
    except FomoError as e:
        typer.echo(f"not configured: {e}")
        raise typer.Exit(1)
    try:
        rows = client.leaderboard("7d", limit=5)
    except FomoError as e:
        typer.echo(f"FAILED: {e}")
        raise typer.Exit(1)
    typer.echo(f"OK, {len(rows)} leaderboard rows (7d). Top:")
    for r in rows:
        typer.echo(f"  {str(r.fomo_handle):20s} pnl7d={r.pnl_7d} trades={r.trades_cnt} vol={r.volume_usd}")
    if rows:
        addrs = client.execution_addresses(rows[0].fomo_user_id)
        typer.echo(f"execution wallets of {rows[0].fomo_handle}: {addrs or 'none found in recent swaps'}")
        typer.echo(f"(profile address {rows[0].profile_address} is NOT what trades on-chain)")
    typer.echo(f"requests used: {client.requests}")


@app.command("trenches")
def trenches_status(
    window: str = typer.Option("24h", "--window", help="1h|24h|7d|30d|all"),
    tape: int = typer.Option(0, "--tape", help="also print the N latest fills"),
    closed: int = typer.Option(0, "--closed", help="also print the N latest closed positions"),
) -> None:
    """Health and a peek at rhtrenches.com (fomo traders on Robinhood Chain)."""
    from datetime import datetime, timezone

    from .sources.trenches import Trenches

    c = Trenches()
    s = c.status()
    typer.echo(f"chain={s.get('chain')} ({s.get('chain_id')}) wallets={s.get('wallets')} "
               f"trades={s.get('trades')} lag={s.get('lag_seconds')}s source={s.get('source')}")
    o = c.overview(window)
    typer.echo(f"{window}: {o.get('fills')} fills, {o.get('active_traders')} active traders, "
               f"{o.get('tokens')} tokens, volume ${o.get('volume', 0):,.0f}, realized ${o.get('realized_pnl', 0):,.0f}")
    top = sorted(c.traders(window), key=lambda t: -(t.get("realized_pnl") or 0))[:10]
    typer.echo(f"\ntop realized PnL ({window}):")
    for t in top:
        typer.echo(f"  {str(t.get('handle')):20s} pnl={t.get('realized_pnl'):>12,.0f} "
                   f"win={t.get('win_rate')} fills={t.get('fills')} vol={t.get('volume'):>12,.0f} {t.get('address')}")
    for f in c.tape(tape)[:tape] if tape else []:
        when = datetime.fromtimestamp(f["ts"], tz=timezone.utc).strftime("%H:%M:%S")
        first = " FIRST" if f.get("new_position") else ""
        typer.echo(f"  {when} {f['side']:4s} {str(f.get('symbol')):12s} ${f.get('usd', 0):>10,.0f} {f.get('handle')}{first}")
    for p in c.closed(window, closed)[:closed] if closed else []:
        typer.echo(f"  closed {str(p.get('symbol')):12s} pnl={p.get('pnl_usd'):>10,.0f} "
                   f"({p.get('pnl_pct'):.0f}%) hold={p.get('hold_seconds', 0) / 3600:.1f}h {p.get('handle')}")


@app.command()
def receive(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """Run the local endpoint the browser extension posts fomo collections to."""
    from .receiver import serve

    serve(host, port)


@app.command("fomo-import")
def fomo_import(path: Path = typer.Argument(..., help="file produced by scripts/fomo_export.js")) -> None:
    """Load leaderboard / holders / execution wallets exported from your browser."""
    from .pipeline.discover import import_browser_export

    typer.echo(_run("fomo_import", import_browser_export, path))


@app.command("resolve")
def resolve_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", help="how many fomo users to work on"),
    chain: str = typer.Option("robinhood", "--chain"),
    handle: Optional[str] = typer.Option(None, "--handle", help="resolve just this trader and print the ranking"),
) -> None:
    """Infer the real on-chain wallet of fomo traders from the tokens and times they traded."""
    from .pipeline.resolve import maker_source, resolve_pending, resolve_user, user_windows

    if handle:
        conn = db.connect()
        u = conn.execute("SELECT * FROM fomo_users WHERE handle=?", (handle,)).fetchone()
        if u is None:
            typer.echo(f"no fomo user with handle {handle!r} — import a browser export first")
            raise typer.Exit(1)
        windows = user_windows(conn, u["user_id"], chain, settings.resolve_windows)
        typer.echo(f"{handle}: {len(windows)} usable windows on {chain}")
        fetch, client = maker_source(chain)
        address, info = resolve_user(conn, fetch, u["user_id"], chain)
        typer.echo(f"resolved: {address or 'no confident match'}  {info}")
        typer.echo(f"via {type(client).__name__}, {client.requests} requests")
        conn.close()
        return
    typer.echo(_run("resolve", resolve_pending, None, chain, limit))


@app.command("fomo-resolve")
def fomo_resolve(limit: Optional[int] = typer.Option(None, "--limit")) -> None:
    """Record the addresses fomo reports per user. These are internal accounts, NOT trading
    wallets — use `resolve` to infer the wallet that actually trades."""
    from .pipeline.discover import resolve_execution_wallets
    from .sources.fomo import FomoClient

    conn = db.connect()
    typer.echo(resolve_execution_wallets(conn, FomoClient(), limit))
    conn.close()


@app.command("new-tokens")
def new_tokens(
    dry_run: bool = typer.Option(False, "--dry-run", help="only print what DexScreener returns"),
) -> None:
    """Poll DexScreener + GeckoTerminal for fresh high-mcap tokens (triggers holder discovery if fomo works)."""
    from .pipeline import new_tokens as nt
    from .pipeline.discover import safe_fomo

    if dry_run:
        for t in nt.fetch_new_tokens():
            age = (db.now() - t.created_at) / 3600 if t.created_at else None
            typer.echo(f"{t.source:13s} {t.chain:9s} {t.symbol or '?':10s} {t.mint}  mcap={t.mcap_usd or 0:>12,.0f}  liq={t.liquidity_usd or 0:>10,.0f}  age={age and f'{age:.1f}h'}")
        return
    typer.echo(_run("new_tokens", nt.poll_new_tokens, None, safe_fomo()))


@app.command("enrich-tokens")
def enrich_tokens_cmd(limit: int = typer.Option(300, "--limit")) -> None:
    """Give names, prices, decimals and liquidity to tokens we only know as addresses (free)."""
    from .pipeline.new_tokens import enrich_tokens

    typer.echo(_run("enrich_tokens", enrich_tokens, None, limit))


@app.command("health")
def health_cmd(
    push: bool = typer.Option(False, "--push", help="send the report to every bot subscriber"),
    beat: bool = typer.Option(False, "--heartbeat", help="ping HEARTBEAT_URL while everything passes"),
    quiet: bool = typer.Option(False, "--quiet", help="print nothing unless something is wrong"),
) -> None:
    """What is quietly broken: stale collections, a silent tape, a router that moved."""
    from .pipeline.health import heartbeat, report

    conn = db.connect()
    try:
        r = report(conn)
        if not (quiet and r["ok"]):
            for c in r["checks"]:
                typer.echo(f"{'ok ' if c['ok'] else 'BAD'}  {c['name']:<20} {c['detail']}")
        if beat:
            msg = heartbeat(r["ok"])
            if not quiet or not r["ok"]:
                typer.echo(f"heartbeat: {msg}")
        if push:
            from .bot import Telegram, fmt_health, subscribers

            tg, text = Telegram(), fmt_health(r)
            sent = 0
            for sub in subscribers(conn):
                try:
                    tg.send(sub["chat_id"], text)
                    sent += 1
                except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                    logging.getLogger("health").warning("send failed: %s", e)
            typer.echo(f"pushed to {sent} subscribers")
    finally:
        conn.close()
    raise typer.Exit(0 if r["ok"] else 1)


@app.command("fomo-api")
def fomo_api_cmd(
    windows: str = typer.Option("24h,7d", "--windows", help="leaderboard windows, 1 credit each"),
    thesis_pages: int = typer.Option(None, "--thesis-pages", help="50 notes a page, 5 credits each"),
) -> None:
    """Collect the fomo half over HTTP instead of through the browser."""
    from .pipeline.collect_api import collect

    got = _run("fomoapi", collect, tuple(w.strip() for w in windows.split(",") if w.strip()),
               thesis_pages)
    typer.echo(got)


@app.command("hot")
def hot_cmd(
    backtest: bool = typer.Option(False, "--backtest", help="replay the rule over the whole tape"),
    mode: str = typer.Option("current", "--scores", help="current | strict | first: which verdict judges a buy"),
    horizon: int = typer.Option(24, "--horizon", help="hours after a burst to measure"),
    window: int = typer.Option(None, "--window", help="minutes (live)"),
    delta: float = typer.Option(None, "--delta", help="conviction gained inside the window (live)"),
) -> None:
    """Tokens several trusted wallets entered in a burst — live, or replayed to pick the bar."""
    from .pipeline import hot

    conn = db.connect()
    chain = settings.dex_chains[0] if settings.dex_chains else None
    try:
        if backtest:
            r = hot.backtest(conn, chain, horizon_s=horizon * 3600, mode=mode)
            typer.echo(f"{r['days']} days the scores reach, {r['tokens_with_trusted_buys']} tokens with "
                       f"trusted buys, horizon {r['horizon_h']}h, scores: {r['mode']}")
            typer.echo(f"{'delta':>5} {'win':>4} {'n':>2} {'bursts':>6} {'/day':>5} {'meas':>5} {'quiet':>5} "
                       f"{'med best':>8} {'>=2x':>5} {'>=3x':>5} {'med last':>8} {'<0.5':>5}")
            for x in r["rows"]:
                f = lambda v, w: f"{v:>{w}}" if v is not None else f"{'-':>{w}}"
                typer.echo(f"{x['delta']:>5} {x['window_min']:>4} {x['min_wallets']:>2} {x['bursts']:>6} "
                           f"{f(x['per_day'],5)} {x['measured']:>5} {x['silent']:>5} "
                           f"{f(x['median_best'],8)} {f(x['p_best_2x'],5)} {f(x['p_best_3x'],5)} "
                           f"{f(x['median_last'],8)} {f(x['p_last_half'],5)}"
                           + (f"   <- {x['label']}" if x.get('label') else ""))
            return
        rows = hot.hot_now(conn, chain,
                           delta=settings.hot_delta if delta is None else delta,
                           window_s=(settings.hot_window_min if window is None else window) * 60,
                           min_wallets=settings.hot_min_wallets,
                           max_age_s=settings.hot_max_age_h * 3600)
        if not rows:
            typer.echo("nothing is bursting right now")
        for h in rows:
            typer.echo(f"{h['sym']:<12} conviction +{h['conviction']:.2f} from {h['wallets']} wallets "
                       f"in {h['window_s'] // 60}min, ${h['usd']:,.0f}  {h['mint']}")
    finally:
        conn.close()


@app.command("sell-check")
def sell_check_cmd(mint: str = typer.Argument(..., help="token address"),
                   force: bool = typer.Option(True, "--force/--cached", help="ask again even if recent")) -> None:
    """Can it be sold: a simulated transfer to the pool, and the pool's buys against sells."""
    from .pipeline.safety import check

    conn = db.connect()
    typer.echo(check(conn, mint.lower(), force=force))


@app.command("pro")
def pro_cmd(
    grant: str = typer.Option(None, "--grant", help="chat id to hand PRO to by hand"),
    days: int = typer.Option(None, "--days", help="with --grant: how many days (default PRO_DAYS)"),
    claim: str = typer.Option(None, "--claim", help="with --grant: attach this burn tx to the chat instead"),
) -> None:
    """PRO in numbers - paid chats, open quotes, unclaimed burns - or a manual grant for support."""
    from .pipeline import pro

    conn = db.connect()
    try:
        if grant:
            if claim:
                ok, why = pro.claim(conn, grant, claim)
                typer.echo(f"{'ok' if ok else 'no'}: {why}")
            else:
                end = pro.grant(conn, grant, days or settings.pro_days)
                typer.echo(f"chat {grant}: PRO until {pro._date(end)}")
            return
        now = db.now()
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
        typer.echo({
            "enabled": pro.enabled(), "usd": settings.pro_price_usd, "days": settings.pro_days,
            "price": pro.price_now(conn) if pro.enabled() else None,
            "paid_now": one("SELECT COUNT(*) FROM bot_subscribers WHERE active=1 AND paid_until > ?", now),
            "expiring_3d": one("SELECT COUNT(*) FROM bot_subscribers WHERE active=1 AND paid_until BETWEEN ? AND ?", now, now + 3 * 86400),
            "quotes_open": one("SELECT COUNT(*) FROM pro_quotes WHERE status='open' AND expires_at > ?", now),
            "payments": one("SELECT COUNT(*) FROM pro_payments WHERE chat_id IS NOT NULL"),
            "unclaimed": [dict(r) for r in conn.execute("SELECT tx, frm, tokens, ts FROM pro_payments WHERE chat_id IS NULL ORDER BY ts DESC LIMIT 10")],
            "burned_tokens": one("SELECT COALESCE(SUM(tokens), 0) FROM pro_payments"),
            "burned_usd": one("SELECT COALESCE(SUM(usd), 0) FROM pro_payments"),
        })
    finally:
        conn.close()


@app.command("noise")
def noise_cmd(scan: bool = typer.Option(False, "--scan", help="quarantine every hyperactive address now"),
              hours: int = typer.Option(24, "--hours", help="the window to count fills over")) -> None:
    """Addresses that are not traders: list the suspects, or quarantine them with --scan."""
    from .pipeline import noise

    conn = db.connect()
    try:
        found = noise.hyperactive(conn, window_s=hours * 3600)
        for h in found:
            typer.echo(f"{h['address']}  {h['handle'] or '-':<18} {h['status'] or '-':<9} {h['n']:>8} fills  {h['tokens']:>5} tokens")
        if not found:
            typer.echo("nobody over the ceiling")
        if scan:
            for h in found:
                removed = noise.quarantine(conn, h["address"], f"{h['n']} fills across {h['tokens']} tokens in {hours}h")
                typer.echo(f"quarantined {h['address'][:10]}: {removed} fills removed")
        typer.echo(f"noise addresses: {len(db.noise(conn))}")
    finally:
        conn.close()


@app.command("verify-fills")
def verify_fills_cmd(
    days: int = typer.Option(7, "--days", help="how far back to fetch receipts for"),
    per_min: int = typer.Option(20, "--per-min", help="RPC allowance to run on, beside the watcher"),
    limit: int = typer.Option(None, "--limit", help="at most this many buys this run"),
) -> None:
    """Fetch the receipt of every unjudged buy and settle whose trade it was."""
    from .pipeline.provenance import verify

    got = _run("verify_fills", verify, days, None, per_min, limit)
    typer.echo(got)


@app.command("resize-fills")
def resize_fills_cmd() -> None:
    """Judge every sized fill again under the current dust floor and ratio."""
    from .pipeline.provenance import refresh_medians, resize

    conn = db.connect()
    try:
        typer.echo({"medians": refresh_medians(conn), **resize(conn)})
    finally:
        conn.close()


@app.command("watch")
def watch_cmd(
    once: bool = typer.Option(False, "--once", help="one tick, then exit"),
) -> None:
    """Read the chain every few seconds and push a burst the moment it forms."""
    from .pipeline.watch import run

    conn = db.connect()
    try:
        typer.echo(run(conn, once=once))
    finally:
        conn.close()


@app.command("digest")
def digest_cmd(
    hours: int = typer.Option(24, "--hours", help="window the digest covers"),
    push: bool = typer.Option(False, "--push", help="send it to every bot subscriber"),
) -> None:
    """The day in one message: what came in, what went out, who joined, what is broken."""
    from .bot import fmt_digest
    from .pipeline.digest import daily

    conn = db.connect()
    try:
        chain = settings.dex_chains[0] if settings.dex_chains else None
        text = fmt_digest(daily(conn, hours=hours, chain=chain))
        typer.echo(re.sub(r"<[^>]+>", "", text))
        if push:
            from .bot import Telegram, subscribers

            tg, sent = Telegram(), 0
            for chat in subscribers(conn):
                try:
                    tg.send(chat["chat_id"], text)
                    sent += 1
                except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                    logging.getLogger("digest").warning("send failed: %s", e)
            typer.echo(f"pushed to {sent} subscribers")
    finally:
        conn.close()


@app.command("calibrate")
def calibrate_cmd(
    min_usd: float = typer.Option(100.0, "--min-usd", help="ignore positions smaller than this"),
    json_out: bool = typer.Option(False, "--json", help="print the raw numbers instead"),
) -> None:
    """Did the score predict anything? Measured only on positions opened after the verdict."""
    import json

    from .pipeline.calibrate import calibrate, report

    conn = db.connect()
    try:
        r = calibrate(conn, min_usd=min_usd)
        typer.echo(json.dumps(r, indent=2) if json_out else report(r))
    finally:
        conn.close()


@app.command("backfill")
def backfill_cmd(
    days: int = typer.Option(30, "--days", help="how far back to walk"),
    max_requests: int = typer.Option(None, "--max-requests", help="stop after this many RPC calls"),
) -> None:
    """Fill the tape from before tracking started. Free, newest window first, resumable."""
    from .pipeline.backfill import backfill

    typer.echo(_run("backfill", backfill, days=days, max_requests=max_requests))


@app.command("browser")
def browser_cmd(
    seed: bool = typer.Option(False, "--seed", help="write the waiting session into the browser"),
    collect: bool = typer.Option(False, "--collect", help="make it collect now, without waiting for its alarm"),
) -> None:
    """Ask the collector browser what it sees, and optionally hand it a waiting session."""
    from . import browser

    if collect:
        typer.echo(f"collect: {browser.collect_now()}")
        typer.echo("the collection takes about a minute; watch `journalctl -u radar-receive`")
        raise typer.Exit(0)
    if seed:
        path = settings.seed_path
        if not path.exists():
            typer.echo(f"no session waiting at {path}")
            raise typer.Exit(1)
        import json as _json

        wrote = browser.write_session(_json.loads(path.read_text(encoding="utf-8")))
        path.unlink(missing_ok=True)
        typer.echo(f"wrote {wrote} and deleted the file")
        time.sleep(8)
    st = browser.state()
    typer.echo(st)
    if st.get("restricted"):
        typer.echo("ACCOUNT RESTRICTED - fomo is refusing this account, not this machine. "
                   "A proxy will not help; the account itself has to be cleared or replaced.")
        raise typer.Exit(1)
    typer.echo("signed in" if st.get("hasToken") and not st.get("showsLogin")
               else "NOT signed in - the page still offers a login")


@app.command("holdings")
def holdings_cmd(limit: int = typer.Option(None, "--limit", help="wallet/token pairs to re-read")) -> None:
    """Read what tracked wallets actually hold, off the chain. Free, and the book depends on it."""
    from .pipeline.holdings import mark_holdings

    typer.echo(_run("holdings", mark_holdings, None, limit))


@app.command()
def track(
    address: Optional[str] = typer.Option(None, "--address", help="track only this wallet"),
    chain: Optional[str] = typer.Option(None, "--chain", help="chain of --address (guessed if omitted)"),
    limit: Optional[int] = typer.Option(None, "--limit", help="max wallets this pass"),
    show: bool = typer.Option(False, "--show", help="print the collected trades"),
) -> None:
    """Collect swaps for tracked wallets (sources from TRACK_SOURCES)."""
    from .pipeline import track as tr
    from .pipeline.discover import add_manual, guess_chain

    if address:
        conn = db.connect()
        row = db.get_trader(conn, address)
        if row is None:
            add_manual(conn, address, chain=chain)
            row = db.get_trader(conn, address)
        chain = chain or row["chain"] or guess_chain(address)
        tracker = tr.pick_tracker(tr.build_trackers(), chain, address)
        if tracker is None:
            typer.echo(f"no configured source supports chain {chain!r} (TRACK_SOURCES={','.join(settings.track_sources)})")
            raise typer.Exit(1)
        n = tr.track_wallet(conn, tracker, address, chain)
        typer.echo(f"{address} [{chain}] via {type(tracker).__name__}: {n} new trades")
        if show:
            for t in conn.execute(
                "SELECT ts, side, mint, token_amount, sol_amount, usd_value FROM trades WHERE address=? ORDER BY ts DESC LIMIT 15",
                (address,),
            ):
                typer.echo(f"  {t['ts']} {t['side']:4s} {t['mint'][:16]:16s} amt={t['token_amount']} sol={t['sol_amount']} usd={t['usd_value']}")
        conn.close()
        return
    typer.echo(_run("track", tr.track_all, None, limit))


@app.command()
def score(
    address: Optional[str] = typer.Option(None, "--address"),
    deep: bool = typer.Option(False, "--deep", help="Sonnet review of top-N active"),
    force: bool = typer.Option(False, "--force", help="ignore rescore schedule"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    show_context: bool = typer.Option(False, "--show-context", help="print the context JSON instead of calling Claude"),
    export: Optional[Path] = typer.Option(None, "--export", help="write pending contexts to a file for in-chat scoring"),
    import_: Optional[Path] = typer.Option(None, "--import", help="import scores produced in chat"),
    model_label: str = typer.Option("manual", "--model-label", help="label stored with imported scores"),
    unscored: bool = typer.Option(False, "--unscored", help="with --export: only wallets with no verdict yet"),
    digest: int = typer.Option(0, "--digest", help="with --export: also print N wallets as a compact table"),
    offset: int = typer.Option(0, "--offset", help="with --digest: skip the first N wallets"),
) -> None:
    """Score traders with Claude. Uses the API, or the export/import flow when SCORER=manual."""
    import json

    from .pipeline import score as sc

    if export:
        conn = db.connect()
        typer.echo(sc.export_contexts(conn, export, force=force, limit=limit,
                                      unscored_only=unscored))
        if digest:
            payload = json.loads(export.read_text(encoding="utf-8"))
            for line in sc.digest_lines(payload)[: (offset + digest) if digest else None][offset:]:
                typer.echo(line)
        conn.close()
        return
    if import_:
        conn = db.connect()
        typer.echo(sc.import_results(conn, import_, model_label))
        conn.close()
        return
    if address:
        conn = db.connect()
        if show_context:
            typer.echo(json.dumps(sc.build_context(conn, address), indent=1))
            return
        res, cost = sc.score_trader(conn, address, settings.deep_model if deep else None)
        conn.close()
        typer.echo(res.model_dump_json(indent=1) if res else "needs_review")
        typer.echo(f"cost: ${cost:.4f}")
        return
    typer.echo(_run("score", sc.score_all, deep=deep, force=force, limit=limit))


@app.command()
def token(
    mint: str = typer.Argument(..., help="contract address of the token"),
    hours: int = typer.Option(48, "--hours", help="window for the flow section"),
) -> None:
    """Who on the watchlist holds this token, what it cost them, and who traded it lately."""
    from .pipeline.analyze import analyze_token, format_token

    conn = db.connect()
    typer.echo(format_token(analyze_token(conn, mint, hours)))
    conn.close()


@app.command()
def trader(
    who: str = typer.Argument(..., help="fomo handle or wallet address"),
    hours: int = typer.Option(168, "--hours", help="window for the fills section"),
) -> None:
    """One trader: the verdict, the open bags, recent fills and the company they keep."""
    from .pipeline.analyze import analyze_trader, format_trader

    conn = db.connect()
    a = analyze_trader(conn, who, hours)
    conn.close()
    if a is None:
        raise typer.BadParameter(f"no trader matches {who!r} (try a fomo handle or a wallet address)")
    typer.echo(format_trader(a))


@app.command("serve")
def serve_cmd(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload", help="restart on code changes (development)"),
) -> None:
    """Run the HTTP API the site and any third-party client read from."""
    from .api import serve

    typer.echo(f"api on http://{host or settings.api_host}:{port or settings.api_port}/docs")
    serve(host, port, reload)


@app.command("bot")
def bot_cmd(
    once: bool = typer.Option(False, "--once", help="one poll and one broadcast, then exit"),
    check: bool = typer.Option(False, "--check", help="verify the token and print the bot identity"),
) -> None:
    """Run the Telegram bot: answers questions and pushes signals as they happen."""
    from .bot import Telegram, broadcast, run

    tg = Telegram()
    if check:
        me = tg.me()
        typer.echo(f"@{me.get('username')} ({me.get('first_name')}) — token works")
        conn = db.connect()
        typer.echo(f"subscribers: {conn.execute('SELECT COUNT(*) FROM bot_subscribers WHERE active=1').fetchone()[0]}")
        conn.close()
        return
    conn = db.connect()
    try:
        typer.echo(run(conn, tg, once=once) if once else run(conn, tg))
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        conn.close()


@app.command()
def report(
    hours: int = typer.Option(24, "--hours"),
    out: Optional[Path] = typer.Option(None, "--out", help="write markdown to file"),
) -> None:
    """Markdown summary of the current state."""
    from .pipeline.report import build_report

    conn = db.connect()
    md = build_report(conn, hours)
    conn.close()
    if out:
        out.write_text(md, encoding="utf-8")
        typer.echo(f"written: {out}")
    else:
        typer.echo(md)


@app.command()
def run(once: bool = typer.Option(False, "--once", help="single pass of every step, then exit")) -> None:
    """Polling loop: discover / new-tokens / resolve / track / score / report."""
    from .pipeline import discover as d
    from .pipeline import holdings as hd
    from .pipeline import new_tokens as nt
    from .pipeline import resolve as rs
    from .pipeline import score as sc
    from .pipeline import track as tr
    from .pipeline.discover import safe_fomo
    from .pipeline.report import build_report

    fomo = safe_fomo()

    def do_report(conn):
        md = build_report(conn)
        Path("report.md").write_text(md, encoding="utf-8")
        return {"bytes": len(md)}

    steps = [
        ("discover_trenches", settings.discover_interval,
         lambda c: d.discover_trenches(c) if "trenches" in settings.track_sources else {"skipped": "trenches disabled"}),
        ("discover_leaderboard", settings.discover_interval, lambda c: d.discover_leaderboard(c, fomo) if fomo else {"skipped": "fomo not configured"}),
        ("new_tokens", settings.new_tokens_interval, lambda c: nt.poll_new_tokens(c, None, fomo)),
        # free on Robinhood Chain, and it is what turns collected fomo users into trackable wallets
        ("resolve", settings.discover_interval, lambda c: rs.resolve_pending(c)),
        ("track", settings.track_interval, lambda c: tr.track_all(c)),
        # bare contract addresses are useless on the page, and naming them costs nothing
        ("enrich_tokens", settings.track_interval, lambda c: nt.enrich_tokens(c)),
        # what a wallet holds is a free read, and without it every position is only as complete
        # as the fills we happened to watch
        ("holdings", settings.track_interval, lambda c: hd.mark_holdings(c)),
        ("score", settings.track_interval * 10, lambda c: sc.score_all(c)),
        ("report", settings.report_interval, do_report),
    ]
    last: dict[str, float] = {k: 0.0 for k, _, _ in steps}
    while True:
        now = time.monotonic()
        for kind, interval, fn in steps:
            if now - last[kind] >= interval:
                last[kind] = now
                _run(kind, fn)
        if once:
            break
        time.sleep(5)


if __name__ == "__main__":
    app()
