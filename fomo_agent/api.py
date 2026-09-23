"""HTTP API over the watchlist — the one brain the site, the bot and any future client read from.

Everything the product knows already lives in `pipeline/analyze.py`; this exposes it over HTTP so
the Astro front end can render pages on the server and so a third party can eventually build on it.
No business logic lives here: a route reads a query string, calls the same function the terminal
calls, and returns the dict. Anything else would be a second definition of the truth.

One thing it does add: a token nobody on the watchlist has touched is still a fair question. Rather
than answering "unknown", the token route falls back to a live DexScreener lookup, stores what comes
back, and says plainly that no tracked wallet is in it. That is the difference between a list and a
tool.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import db, links
from .config import settings
from .pipeline import analyze, keys, pro
from .sources.rpc import QUOTE_TOKENS

log = logging.getLogger(__name__)

ADDRESS_LEN = 42


def chain() -> str | None:
    """Read the chain from settings on every call rather than caching it at import.

    A module-level global set during startup silently becomes None in any context that does not
    run the lifespan — tests, a script, a worker — and a None chain quietly widens every query.
    """
    return settings.dex_chains[0] if settings.dex_chains else None


# ---------------------------------------------------------------- plumbing

def get_conn() -> sqlite3.Connection:
    """One connection per request. sqlite is fast enough that pooling buys nothing here."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


class RateLimit:
    """A fixed window per client address. Enough to stop a scraper, cheap enough to ignore."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


class DailyQuota:
    """Requests per address per UTC day, counted across the workers in one small table.

    The per-minute window is blind to a steady tap: four requests a second, for ever, never fills
    it. This is the other half - and it is deliberately generous, because the reader it stops is
    the reader who should have a key.
    """

    def __init__(self, per_day: int):
        self.per_day = per_day
        self.counts: dict[str, int] = {}
        self.day = time.gmtime().tm_yday
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """(allowed, used). A worker of its own counts its own share; three workers behind one
        address share the day's number through the shared limiter file instead."""
        if self.per_day <= 0:
            return True, 0
        with self._lock:
            day = time.gmtime().tm_yday
            if day != self.day:
                self.counts.clear()
                self.day = day
            used = self.counts.get(key, 0) + 1
            self.counts[key] = used
            if len(self.counts) > 50_000:
                self.counts = {k: v for k, v in self.counts.items() if v > 20}
            return used <= self.per_day, used


limiter = RateLimit(settings.api_rate_per_min)
quota = DailyQuota(settings.api_rate_per_day)
keyed = RateLimit(settings.api_key_rate_per_min)   # per key, for PRO chats
keyring = keys.Keyring()
WEBHOOKS_URL = f"{settings.public_site_url or 'https://fomoradar.app'}/webhooks"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM traders WHERE score IS NOT NULL").fetchone()[0]
    conn.close()
    log.info("api up: chain=%s, %d scored traders", chain(), n)
    yield


app = FastAPI(
    title="FOMO Robinhood Radar",
    version="0.1.0",
    summary="Which fomo.family traders on Robinhood Chain actually know what they are doing.",
    description=(
        "Research over Robinhood Chain. Every trader here was resolved from a fomo "
        "profile to a real on-chain wallet, tracked, and judged by Claude. Nothing on this API "
        "places a trade, and none of it is financial advice.\n\n"
        "**Reading it:** 120 requests a minute per address, no key needed. Send a `User-Agent` "
        "that names your project - requests without one are refused. A PRO subscriber of the "
        "Telegram bot (@fomoradarRH_bot, `/pro`) can ask it for a key with `/apikey` and send it "
        "as `X-API-Key` for 600 a minute. A key can also be bought without the bot at `/webhooks`: "
        "`POST /api/checkout` opens an order, the exact amount it quotes is burned from any wallet, "
        "and the key appears at `GET /api/checkout/{secret}` within a block - no account, no email, "
        "nothing stored about the reader. `GET /api/me` says what a key is and how it is doing, "
        "`POST /api/webhook` points it at a URL, `POST /api/webhook/test` proves the wiring, and "
        "`POST /api/checkout/{secret}/renew` buys another term without changing the key. Answers are "
        "cached for a few seconds; the feeds move on the watcher\u2019s tick, not faster.\n\n"
        "**Webhooks:** a key can name an https URL - in the bot (`/webhook <url>`) or with "
        "`POST /api/webhook`. Every burst, "
        "launch and hour-later read is then POSTed there as JSON the second the chats get it, and so "
        "is a void - a pushed token that has since proved unsellable, with the count that settled it: "
        "`{event: burst|launch|followup|void, ts, chain, mint, symbol, data: {...what the chat was told}, "
        "links: {fomo, site}}`, with `X-Radar-Signature: sha256=<HMAC-SHA256 of the body, keyed "
        "with your API key>` to check it is ours. Answer 2xx inside six seconds; two failed tries "
        "count one failure, twenty failures in a row switch the hook off until it is set again. "
        "`off` as the URL stops it. The record of every alert is at `/api/record`; the alerts are "
        "research, and the receiver decides what to do with them."
    ),
    lifespan=lifespan,
)
# Our own site renders on the server and reaches the API over loopback, so CORS never applied to
# it. The only clients a restriction can reach are third-party browsers, which is the audience this
# API exists for — so it stays open, and the rate limiter above is what guards it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.api_cors_origins) or ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# Ten seconds of memory in front of every GET. The feeds change on the watcher's tick and the
# fifteen-minute pass; the queries behind them walk the whole tape and cost 150 to 750 ms each,
# and under forty readers at once that became two-second tails on every page. Forty readers in
# the same ten seconds are asking the same question, and it is answered once. Keyed by the full
# URL, so a token page and a window size are each their own entry; bounded, so a crawler walking
# six thousand token pages cannot turn it into a second database.
RESPONSE_TTL = settings.api_cache_ttl_s
RESPONSE_CACHE_MAX = 4000
_responses: dict[str, tuple[float, int, bytes, str]] = {}
_responses_lock = threading.Lock()
# one computation in flight per key: when the entry expires under sixty readers at once, one of
# them runs the query and the other fifty-nine wait for that answer instead of running it too
_inflight: dict[str, "asyncio.Future[None]"] = {}
UNCACHED = ("/api/health",)
# a payment page polls for "paid"; a ten-second-old answer is a wrong one. A key's own endpoints
# answer about the caller and must never be served from another caller's copy
UNCACHED_PREFIX = ("/api/pro", "/api/checkout", "/api/me", "/api/webhook")


def _cached(key: str):
    with _responses_lock:
        hit = _responses.get(key)
    if hit and time.monotonic() - hit[0] < RESPONSE_TTL:
        return Response(content=hit[2], status_code=hit[1], media_type=hit[3],
                        headers={"x-cache": "hit"})
    return None


@app.middleware("http")
async def remember_responses(request: Request, call_next):
    if request.method != "GET" or not request.url.path.startswith("/api/") \
            or request.url.path in UNCACHED or request.url.path.startswith(UNCACHED_PREFIX):
        return await call_next(request)
    key = str(request.url)
    if (hit := _cached(key)) is not None:
        return hit
    import asyncio
    waiting = _inflight.get(key)
    if waiting is not None:
        try:
            await asyncio.wait_for(asyncio.shield(waiting), timeout=8.0)
        except asyncio.TimeoutError:
            pass   # the first reader is stuck on something; this one goes and finds out itself
        if (hit := _cached(key)) is not None:
            return hit
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        response = await call_next(request)
        if response.status_code != 200:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        now = time.monotonic()
        with _responses_lock:
            if len(_responses) >= RESPONSE_CACHE_MAX:
                for k in [k for k, v in _responses.items() if now - v[0] >= RESPONSE_TTL]:
                    _responses.pop(k, None)
                if len(_responses) >= RESPONSE_CACHE_MAX:
                    _responses.clear()
            _responses[key] = (now, 200, body, response.media_type or "application/json")
        return Response(content=body, status_code=200, media_type=response.media_type,
                        headers={"x-cache": "miss"})
    finally:
        _inflight.pop(key, None)
        if not fut.done():
            fut.set_result(None)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Per visitor. A public call arrives through Caddy carrying X-Forwarded-For; the site's own
    server-side renders arrive from loopback with no such header, and those are not one visitor
    but all of them. Counting them as one address put the whole site under a single 120-a-minute
    budget - about forty page views - after which every visitor got an empty page. Loopback
    without a forwarded address is the site talking to itself, and is not limited."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    host = request.client.host if request.client else "?"
    if not forwarded and host in ("127.0.0.1", "::1"):
        return await call_next(request)
    if settings.api_require_user_agent and not request.headers.get("user-agent", "").strip():
        return JSONResponse({"error": "send a User-Agent that names your project", "docs": "/docs"}, status_code=400)
    key = request.headers.get("x-api-key") or request.query_params.get("key")
    if key and settings.api_key_rate_per_min > 0:
        conn = db.connect()
        try:
            ok, chat = keyring.check(conn, key)
            if not ok:
                return JSONResponse({"error": "unknown, revoked or lapsed API key", "docs": "/docs"}, status_code=401)
            if not keyed.check(key):
                return JSONResponse({"error": "rate limited", "limit_per_minute": keyed.per_minute}, status_code=429)
            keyring.used(conn, key)
        finally:
            conn.close()
        return await call_next(request)
    who = forwarded or host
    allowed, used = quota.check(who)
    if not allowed:
        # a tap left running, not a visitor: the key that lifts this is the same key that turns
        # the polling into a push
        return JSONResponse({"error": "daily quota spent", "quota_per_day": quota.per_day, "used": used,
                             "more": f"a key reads {keyed.per_minute} a minute with no daily cap, and the alerts "
                                     f"can be POSTed to you instead of polled for: {WEBHOOKS_URL}"},
                            status_code=429)
    if not limiter.check(who):
        # the reader who hits this is the reader who would pay: the alerts can be pushed to them
        # instead of polled for, and nobody polling us had any way of knowing that
        return JSONResponse({"error": "rate limited", "limit_per_minute": limiter.per_minute,
                             "more": f"a key reads {keyed.per_minute} a minute, and the alerts can be POSTed to you "
                                     f"as they go out: {WEBHOOKS_URL}"},
                            status_code=429)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["x-radar-webhooks"] = f"stop polling - the alerts can be POSTed to you: {WEBHOOKS_URL}"
    return response


# ---------------------------------------------------------------- shaping

def trader_row(r: dict) -> dict:
    """One leaderboard entry, with the tags already unpacked for the client."""
    import json

    tags = json.loads(r["tags"]) if r.get("tags") else {}
    return {
        "handle": r["handle"], "address": r["address"], "score": r["score"],
        "status": r["status"], "summary": r["summary"], "fomo_pnl": r["fomo_pnl"],
        "style": tags.get("style") or [], "red_flags": tags.get("red_flags") or [],
    }


# Addresses a live lookup found nothing for, and when. Six third-party bots walk /api/token/*
# with whatever addresses they have, and every unknown one used to cost a GeckoTerminal call:
# 1,773 of them failed on 429 in a day and each held a request open while it waited. An address
# that answered nothing is not asked about again for an hour.
_missed: dict[str, float] = {}
MISS_TTL = 3600.0
MISS_MAX = 20_000


def live_lookup(conn: sqlite3.Connection, mint: str) -> bool:
    """Name a token we have never seen, so an unknown address still gets a real answer.

    One free request, through the same source the collection pass uses. Returns True when
    something was learned. A miss is remembered for an hour so the same unknown address does
    not cost a request per crawler per minute.
    """
    from .pipeline.new_tokens import lookup_tokens

    key = mint.lower()
    seen = _missed.get(key)
    if seen is not None and time.monotonic() - seen < MISS_TTL:
        return False
    try:
        tokens, _ = lookup_tokens(chain() or "robinhood", [mint], gecko=gecko(), dex=dex())
    except Exception as e:  # noqa: BLE001 - an unknown token is still answerable without this
        # the screener's ceiling is hit whenever crawlers walk new addresses; that is expected
        # and remembered below, not a warning eight hundred times a day
        (log.debug if "429" in str(e) or "allowance" in str(e) else log.warning)("live lookup for %s failed: %s", mint[:10], e)
        tokens = []
    for t in tokens:
        if t.mint.lower() == key:
            _missed.pop(key, None)
            with db.tx(conn):
                db.upsert_token(conn, t.mint, chain=t.chain, symbol=t.symbol, mcap_usd=t.mcap_usd,
                                liquidity_usd=t.liquidity_usd, created_at=t.created_at,
                                price_usd=t.price_usd, price_at=db.now(), checked_at=db.now(),
                                decimals=t.decimals, pool_address=t.pool_address)
            return True
    if len(_missed) >= MISS_MAX:
        _missed.clear()
    _missed[key] = time.monotonic()
    return False


# One upstream client per process, not one per request.
#
# `GeckoTerminal()` in the request path looked harmless and was two bugs. Each instance brought
# its own rate limiter, so the limiter never saw two calls in a row and a crawler walking the token
# pages sent 429s straight back to the page. And each brought its own connection pool that nothing
# closed: after a day of that walk the process held 581 TLS connections to GeckoTerminal open, and
# 2.3 GB of memory with them. Refcounting does not rescue an httpx client that has made a request —
# the pool and its connections reference each other.
_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def gecko():
    from .sources.geckoterminal import GeckoTerminal

    with _clients_lock:
        if "gecko" not in _clients:
            _clients["gecko"] = GeckoTerminal(patient=False)
        return _clients["gecko"]


def dex():
    from .sources.dexscreener import DexScreener

    with _clients_lock:
        if "dex" not in _clients:
            _clients["dex"] = DexScreener()
        return _clients["dex"]


# A candle set is the same for every visitor, and the pool it comes from produces one new bar an
# hour at most. Serving it from memory keeps a page nobody has cached off the upstream rate limit.
RANGES = {"24h": ("hour", 1, 24), "7d": ("hour", 1, 168), "30d": ("day", 1, 30)}
_candles: dict[tuple[str, str], tuple[float, list, float]] = {}   # key -> (fetched at, rows, ttl)
CANDLE_TTL = 300
# the bars are hourly or daily; a new one lands an hour apart at the soonest, and the bar in
# progress moving is not worth a request every five minutes. Bots asked for eighty pools' charts
# an hour and three workers each refetched every one of them every five minutes
CANDLE_TTLS = {"24h": 600, "7d": 1200, "30d": 3600}
# a burst whose day is over has its outcome; its candles are read again every few hours, not
# every five minutes. /api/hot is asked eighty times a minute by bots, each answer measures every
# burst in the window, and three workers refreshing forty settled pools every five minutes were
# most of the box's GeckoTerminal allowance - the token page's chart found it spent
SETTLED_TTL = 6 * 3600


def _evict_stale() -> None:
    """An expired entry is never served, but until this it was never dropped either, so the cache
    only ever grew — one entry per pool per span, for every token anybody had ever looked at."""
    now = time.monotonic()
    for key in [k for k, (at, _, ttl) in _candles.items() if now - at > ttl]:
        _candles.pop(key, None)


def candles_for(pool: str, chain_name: str, span: str, ttl: float | None = None,
                conn: sqlite3.Connection | None = None) -> list[list[float]]:
    """OHLCV for one pool, cached in this worker and (given `conn`) in the DB for every worker,
    for `ttl` seconds (the span's own by default), and empty rather than raising."""
    ttl = ttl or CANDLE_TTLS.get(span, CANDLE_TTL)
    key = (pool, span)
    hit = _candles.get(key)
    if hit and time.monotonic() - hit[0] < hit[2]:
        return hit[1]
    stored = None
    if conn is not None:
        try:
            stored = conn.execute("SELECT fetched_at, rows FROM candle_cache WHERE pool=? AND span=?", key).fetchone()
        except sqlite3.Error as e:
            log.debug("candle cache read: %s", e)
        if stored and db.now() - stored["fetched_at"] < ttl:
            rows = json.loads(stored["rows"])
            _candles[key] = (time.monotonic(), rows, ttl)
            return rows
    timeframe, aggregate, limit = RANGES[span]
    try:
        rows = gecko().ohlcv(chain_name, pool, timeframe, aggregate, limit)
    except Exception as e:  # noqa: BLE001 - a page without a chart is still a page
        # an allowance spent is the ordinary case under load, not something to log every time
        if "allowance spent" not in str(e) and "not backing off" not in str(e):
            log.warning("candles for %s failed: %s", pool[:12], e)
        # what any worker last saw beats an empty chart
        return hit[1] if hit else json.loads(stored["rows"]) if stored else []
    _evict_stale()
    _candles[key] = (time.monotonic(), rows, ttl)
    if conn is not None and rows:
        try:
            with db.tx(conn):
                conn.execute("INSERT OR REPLACE INTO candle_cache(pool, span, fetched_at, rows) VALUES(?,?,?,?)",
                             (pool, span, db.now(), json.dumps(rows)))
        except sqlite3.Error as e:
            log.debug("candle cache write: %s", e)
    return rows


# ---------------------------------------------------------------- routes

@app.get("/api/record", tags=["signals"])
def record_route(
    days: int = Query(30, ge=1, le=120),
    kind: str | None = Query(None, pattern="^(burst|launch)$"),
    include_unsent: bool = Query(False, description="also the launches nobody was sent, kept for the study"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Every alert the bot sent in the window and what came of each: entry, the hour's peak and
    when, where it sits now, what traded, a verdict - and the paper run those rows make, a
    hundred dollars into every alert at the entry and out at the hour read. The record is what
    the messages in a subscriber's chat add up to; nothing here is recomputed to look better.

    Launches stopped being pushed on 22 September and are still measured on the same ledger;
    `include_unsent=true` adds those rows, which is the honest way to ask whether they should
    come back rather than the way to make the record look busier."""
    from .pipeline import record

    return record.report(conn, days=days, kind=kind, sent_only=not include_unsent)


@app.get("/api/pro", tags=["meta"])
def pro_terms(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """What PRO costs and where it is paid: the terms the /pro page shows before there is a quote."""
    return {"enabled": pro.enabled(), "usd": settings.pro_price_usd, "days": settings.pro_days,
            "token": settings.pro_token, "symbol": settings.pro_token_symbol,
            "burn_address": settings.pro_burn_address, "price": pro.price_now(conn) if pro.enabled() else None,
            "bot": settings.telegram_bot_name}


@app.post("/api/checkout", tags=["meta"])
def checkout_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Start an order for an API key with webhooks, without a Telegram chat in the way.

    Returns the secret for this order, which is the page's bearer and nobody else's, and the
    exact amount to burn. The watcher matches the burn by that amount, as it does for the bot's
    quotes, and the key appears here the moment the block lands.
    """
    from .pipeline import checkout

    order = checkout.start(conn)
    if order is None:
        raise HTTPException(404, "no key tier right now")
    log.info("checkout: order %s opened for %s", order["code"], (request.client.host if request.client else "?"))
    return order


@app.get("/api/checkout/{secret}", tags=["meta"])
def checkout_status(secret: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """The order this secret owns: the quote, and once the burn lands, the key and its webhook."""
    from .pipeline import checkout

    out = checkout.status(conn, secret)
    if out is None:
        raise HTTPException(404, "no such order")
    return out


@app.post("/api/checkout/{secret}/renew", tags=["meta"])
def checkout_renew(secret: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Another term on the same order: the key stays what it is, so a receiver already wired to
    it keeps working. Returns the next amount to burn."""
    from .pipeline import checkout

    out = checkout.renew(conn, secret)
    if out is None:
        raise HTTPException(404, "no such order")
    return out


def _caller_key(request: Request, conn: sqlite3.Connection) -> str:
    """The key this request was authenticated with. The middleware has already checked it is
    live; this is only which one."""
    key = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    if not key or keyring.check(conn, key)[0] is False:
        raise HTTPException(401, "send a live key in X-Api-Key")
    return key


@app.get("/api/me", tags=["meta"])
def me(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """What this key is: its term, its allowance, its webhook and how it is doing."""
    from .pipeline import keys as keymod

    key = _caller_key(request, conn)
    row = keymod.by_key(conn, key)
    return {"key": key[:6] + "…", "paid_until": row["paid_until"], "requests": row["requests"],
            "rate_per_minute": keyed.per_minute, "webhook_url": row["webhook_url"],
            "webhook_failures": row["webhook_failures"], "webhook_last": row["webhook_last"],
            "events": ["burst", "launch", "followup", "void"], "now": db.now()}


@app.post("/api/webhook", tags=["meta"])
def set_webhook(request: Request, body: dict, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Point this key's alerts at a URL of yours: `{"url": "https://…"}`, or `"off"` to stop.

    Every burst, launch, hour-later read and void is POSTed there as it goes to the chats, signed
    with `X-Radar-Signature: sha256=<HMAC-SHA256 of the body, keyed with your key>`.
    """
    from .pipeline import webhooks

    key = _caller_key(request, conn)
    ok, why = webhooks.set_for_key(conn, key, str(body.get("url", "")))
    if not ok:
        raise HTTPException(400, why)
    return {"ok": True, "state": why, "docs": "/docs#webhooks"}


@app.post("/api/webhook/test", tags=["meta"])
def test_webhook(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """One signed POST to your URL right now, shaped like a real event, so you can wire the
    receiver before the first alert rather than after it."""
    from .pipeline import webhooks

    key = _caller_key(request, conn)
    ok, why = webhooks.deliver_sample(conn, key)
    return {"ok": ok, "detail": why, "sent": webhooks.sample()}


@app.get("/api/pro/{code}", tags=["meta"])
def pro_quote(code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """One quote by its code: the exact amount, the address, the deadline, and whether it has
    been paid. Read by the payment page, which polls it until `status` says paid. The chat id
    stays inside; a code is what the bot handed one person and nothing else."""
    if not pro.enabled():
        raise HTTPException(404, "no PRO tier")
    q = pro.quote_by_code(conn, code)
    if q is None:
        raise HTTPException(404, "no such quote")
    now = db.now()
    status = q["status"]
    if status == "open" and q["expires_at"] <= now:
        status = "expired"
    return {"code": q["code"], "usd": q["usd"], "price": q["price"], "tokens": q["tokens"],
            "symbol": settings.pro_token_symbol, "token": settings.pro_token,
            "burn_address": settings.pro_burn_address, "days": settings.pro_days,
            "created_at": q["created_at"], "expires_at": q["expires_at"], "status": status,
            "tx": q["tx"], "paid_until": q["paid_until"], "now": now,
            "bot": settings.telegram_bot_name}


@app.get("/api/health", tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    last = conn.execute("SELECT MAX(ts) FROM trades").fetchone()[0]
    return {"ok": True, "chain": chain(), "last_fill_ts": last,
            "stale_seconds": (db.now() - last) if last else None}


@app.get("/api/stats", tags=["meta"])
def stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """The numbers the masthead shows. Quote assets are excluded from the fill count."""
    not_quote = analyze.NOT_QUOTE.format(col="mint")

    def one(sql: str, *args):
        return conn.execute(sql, args).fetchone()[0]

    return {
        "traders": one("SELECT COUNT(*) FROM traders"),
        "scored": one("SELECT COUNT(*) FROM traders WHERE score IS NOT NULL"),
        "active": one("SELECT COUNT(*) FROM traders WHERE status=?", "active"),
        "watch": one("SELECT COUNT(*) FROM traders WHERE status=?", "watch"),
        "dropped": one("SELECT COUNT(*) FROM traders WHERE status=?", "dropped"),
        "fills": one(f"SELECT COUNT(*) FROM trades WHERE 1=1{not_quote}"),
        "positions": one("SELECT COUNT(*) FROM fomo_positions"),
        "open_pnl": one("SELECT SUM(unrealized_pnl) FROM fomo_positions") or 0,
        "tokens": one("SELECT COUNT(*) FROM tokens WHERE symbol IS NOT NULL"),
        "updated_ts": one("SELECT MAX(ts) FROM trades"),
    }


@app.get("/api/activity", tags=["meta"])
def activity(
    hours: int = Query(48, ge=6, le=336),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Fills per hour by trusted wallets — the pulse the masthead draws as a sparkline.

    Empty hours are returned as zeros rather than skipped, otherwise a quiet night reads as a
    gap in the chart instead of as quiet.
    """
    since = db.now() - hours * 3600
    rows = dict(conn.execute(
        "SELECT CAST((tr.ts - ?) / 3600 AS INTEGER) bucket, COUNT(*) n "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.ts >= ? AND t.score >= ?" + analyze.NOT_QUOTE.format(col="tr.mint")
        + " AND COALESCE(tr.kind, 'trade') = 'trade'"
        " GROUP BY bucket", (since, since, analyze.TRUSTED)).fetchall())
    series = [rows.get(i, 0) for i in range(hours)]
    return {"hours": hours, "series": series, "total": sum(series), "peak": max(series or [0])}


@app.get("/api/distribution", tags=["meta"])
def distribution(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """How the roster's scores are shaped — ten buckets of ten points each."""
    buckets = [0] * 10
    for (score,) in conn.execute("SELECT score FROM traders WHERE score IS NOT NULL"):
        buckets[min(int(score) // 10, 9)] += 1
    return {"buckets": buckets, "total": sum(buckets), "peak": max(buckets)}


@app.get("/api/signals", tags=["signals"])
def signals(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(40, ge=1, le=100),
    min_buyers: int = Query(2, ge=2, le=50),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Tokens several trusted wallets bought in the window, ranked by conviction.

    Conviction is the sum of each buyer's (score/100)^2 — it answers *whose* money moved rather
    than how many wallets did, because anyone can open a wallet.
    """
    rows = analyze.signals(conn, chain(), hours=hours, min_buyers=min_buyers, limit=limit)
    # which of these arrived in a burst rather than drifting in over the day
    burst_at = {r["mint"]: dict(r) for r in conn.execute(
        "SELECT mint, MAX(ts) ts, MAX(conviction) conviction FROM bursts WHERE ts >= ? GROUP BY mint",
        (db.now() - hours * 3600,))}
    for r in rows:
        r["who"] = [h for h in (r.get("who") or "").split(",") if h]
        r["scores"] = [int(s) for s in (r.get("scores") or "").split(",") if s]
        r["burst"] = burst_at.get(r["mint"])
    return {"hours": hours, "count": len(rows), "signals": rows}


@app.get("/api/hot", tags=["signals"])
def hot_route(
    hours: int = Query(24, ge=1, le=168),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Bursts: several trusted wallets entering one token inside minutes rather than over a day.

    `now` is what is bursting this minute, straight from the tape. `recent` is every burst the
    watcher wrote down in the window, each with what the price did afterwards in the price the
    cohort itself paid — so the feed carries its own scorecard.
    """
    from .pipeline import hot

    return {
        "delta": settings.hot_delta, "window_min": settings.hot_window_min,
        "min_wallets": settings.hot_min_wallets, "hours": hours,
        "now": hot.hot_now(conn, chain(), delta=settings.hot_delta,
                           window_s=settings.hot_window_min * 60,
                           min_wallets=settings.hot_min_wallets,
                           max_age_s=settings.hot_max_age_h * 3600 or None),
        "recent": hot.recent(conn, chain(), hours=hours, candles_for=_pool_candles(conn, hours)),
    }


def _pool_candles(conn: sqlite3.Connection, hours: int):
    """mint -> the pool's candles over the window, from the same cache the token page fills."""
    span = "24h" if hours <= 24 else "7d" if hours <= 168 else "30d"

    def candles(mint: str):
        row = conn.execute("SELECT pool_address, chain FROM tokens WHERE mint=?", (mint,)).fetchone()
        if not row or not row["pool_address"]:
            return None
        last = conn.execute("SELECT MAX(ts) FROM bursts WHERE mint=?", (mint,)).fetchone()[0]
        settled = last is not None and db.now() - last > 86400
        return candles_for(row["pool_address"], row["chain"] or chain() or "robinhood", span,
                           ttl=SETTLED_TTL if settled else None, conn=conn) or None
    return candles


@app.get("/api/exits", tags=["signals"])
def exits(
    hours: int = Query(6, ge=1, le=720),
    limit: int = Query(40, ge=1, le=100),
    min_sellers: int = Query(2, ge=1, le=50),
    min_exit: float = Query(0.5, ge=0.05, le=1.0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Tokens the trusted wallets are leaving, heaviest departure first.

    The mirror of /api/signals, and the half nobody publishes. An entry feed cannot tell you that
    the wallets you copied have gone — a token sits on it as long as the buy is inside the window,
    whether or not the buyer is still there.

    A sale is not an exit: a wallet counts once it has sold `min_exit` of what the tape watched it
    buy, measured in tokens rather than dollars because dollars move with the price. `gone` is how
    many of the sellers are out entirely.
    """
    rows = analyze.exits(conn, chain(), hours=hours, min_sellers=min_sellers,
                         min_exit=min_exit, limit=limit)
    return {"hours": hours, "count": len(rows), "exits": rows}


@app.get("/api/tape", tags=["signals"])
def tape(
    limit: int = Query(60, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Every recent fill by a wallet scoring 60+, newest first."""
    rows = [dict(r) for r in conn.execute(
        "SELECT tr.ts, tr.side, tr.usd_value usd, tr.mint, t.fomo_handle handle, t.score, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,8)) sym "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        "WHERE t.score >= ? AND tr.usd_value IS NOT NULL"
        + analyze.NOT_QUOTE.format(col="tr.mint") + " AND COALESCE(tr.kind, 'trade') = 'trade'"
        " ORDER BY tr.ts DESC LIMIT ?", (analyze.TRUSTED, limit))]
    return {"count": len(rows), "fills": rows}


@app.get("/api/fresh", tags=["signals"])
def fresh(
    hours: int = Query(24, ge=1, le=168),
    max_age_h: int = Query(72, ge=1, le=720),
    min_liquidity: float = Query(5_000, ge=0),
    min_buyers: int = Query(2, ge=1, le=20),
    limit: int = Query(40, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Tokens the cohort has just started buying, ranked by heat.

    The signal feed ranks everything trusted wallets bought today, however long they have held it.
    This one only lists tokens whose first trusted buy landed inside the window — the cohort
    entering rather than sitting — and weights each buyer by how soon after the launch they got in.
    """
    return analyze.fresh(conn, chain(), hours, max_age_h, min_liquidity, min_buyers, limit)


@app.get("/api/leaderboard", tags=["traders"])
def leaderboard(
    status: str = Query("active", pattern="^(active|watch|dropped|all)$"),
    limit: int = Query(50, ge=1, le=400),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Our own ranking — by judgement of the process, not by the headline PnL fomo shows."""
    rows = [trader_row(r) for r in analyze.leaderboard(conn, limit, status)]
    return {"status": status, "count": len(rows), "traders": rows}


@app.get("/api/trader/{who}", tags=["traders"])
def trader(
    who: str,
    hours: int = Query(168, ge=1, le=8760),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """A handle or a wallet address: the verdict, the open book, recent fills, the company kept."""
    a = analyze.analyze_trader(conn, who, hours)
    if a is None:
        raise HTTPException(404, f"no trader matches {who!r}")
    return a


@app.get("/api/token/{mint}", tags=["tokens"])
def token(
    mint: str,
    hours: int = Query(48, ge=1, le=720),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Whose money is in this token, what it cost them, and who traded it in the window.

    An address nobody tracked has touched still gets an answer: we look it up live, then say so.
    """
    if not (mint.startswith("0x") and len(mint) == ADDRESS_LEN) and len(mint) < 32:
        raise HTTPException(400, "that is not a token address")
    a = analyze.analyze_token(conn, mint, hours)
    if a["symbol"] is None and not a["holders"] and not a["flow"]:
        if live_lookup(conn, a["mint"]):
            a = analyze.analyze_token(conn, mint, hours)
    a["tracked"] = bool(a["holders"] or a["flow"])
    a["is_quote"] = a["mint"] in QUOTE_TOKENS
    a["links"] = links.token_links(a["mint"], a.get("chain") or "robinhood")
    return a


@app.get("/api/token/{mint}/chart", tags=["tokens"])
def token_chart(
    mint: str,
    span: str = Query("7d", pattern="^(24h|7d|30d)$"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Candles for the token's deepest pool: [ts, open, high, low, close, volume], oldest first.

    Every third-party chart widget was tried against this chain and none of them draws, so the
    page draws its own from these. An unknown pool answers with an empty list and a reason, not a
    404: a token page without a chart is a smaller answer, not a broken one.
    """
    mint = mint.lower() if mint.startswith("0x") else mint
    row = conn.execute("SELECT pool_address, chain, symbol FROM tokens WHERE mint=?", (mint,)).fetchone()
    if row is None or not row["pool_address"]:
        return {"mint": mint, "span": span, "pool": None, "candles": [],
                "why": "no pool on record for this token yet"}
    rows = candles_for(row["pool_address"], row["chain"] or chain() or "robinhood", span, conn=conn)
    out = {"mint": mint, "span": span, "pool": row["pool_address"],
           "symbol": row["symbol"], "candles": rows, "source": "geckoterminal"}
    if not rows:
        out["why"] = "the candle source is rate-limiting us this minute; the chart comes back on its own"
    return out


@app.get("/api/search", tags=["meta"])
def search(
    q: str = Query(..., min_length=1, max_length=64),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """One box for both questions: an address or a handle, resolved to where it should go."""
    q = q.strip()
    row = analyze.find_trader(conn, q)
    if row:
        return {"kind": "trader", "handle": row["fomo_handle"], "address": row["address"]}
    if q.startswith("0x") and len(q) == ADDRESS_LEN:
        return {"kind": "token", "address": q.lower()}
    like = f"%{q}%"
    hits = [dict(r) for r in conn.execute(
        "SELECT fomo_handle handle, address, score, status FROM traders "
        "WHERE fomo_handle LIKE ? AND score IS NOT NULL ORDER BY score DESC LIMIT 10", (like,))]
    tokens = [dict(r) for r in conn.execute(
        "SELECT mint, symbol FROM tokens WHERE symbol LIKE ? LIMIT 10", (like,))]
    if not hits and not tokens:
        return {"kind": "none", "query": q}
    return {"kind": "suggestions", "traders": hits, "tokens": tokens}


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    import uvicorn

    # The reloader must watch the package and nothing else. Pointed at the working directory it
    # also watches fomo_agent.db-wal, which sqlite rewrites on every read — the service then
    # restarts in a loop and drops requests mid-flight, which looks exactly like flaky data.
    # Three seconds for in-flight requests on shutdown, then they are dropped. Without the cap a
    # restart waits for every request to finish, and a request waiting on an upstream that is
    # rate-limiting us can take a minute; the site sat behind a 502 for 64 seconds once because
    # of exactly that. Anything a request could not finish in three seconds, the client has
    # already given up on.
    # Caddy writes every request already; uvicorn writing each one again was 776k lines a day
    # and half the journal, which then held twenty-one hours instead of thirty days
    uvicorn.run("fomo_agent.api:app", host=host or settings.api_host,
                port=port or settings.api_port, reload=reload,
                reload_dirs=["fomo_agent"] if reload else None,
                workers=None if reload else max(1, settings.api_workers),
                timeout_graceful_shutdown=3, access_log=False)
