"""GeckoTerminal public API (no key, 30 req/min). Covers tokens that never bought a DexScreener boost.

Endpoints (public, documented at https://apiguide.geckoterminal.com):
  GET /networks/{network}/new_pools?page=N              -> newest pools (20/page, mostly dust)
  GET /networks/{network}/trending_pools?duration=5m|1h|6h|24h
  GET /networks/{network}/pools?sort=h24_volume_usd_desc -> top pools by volume
  GET /networks/{network}/tokens/multi/{a,b,...}         -> up to 30 tokens by address
All accept include=base_token. Network ids for solana/base/robinhood match DexScreener chainIds.

The multi-token lookup is what prices Robinhood Chain. DexScreener indexes almost none of it —
measured 2026-09-08: of 30 tokens tracked wallets were holding, it returned pairs for 3 and a
price for none, while this endpoint answered all 30 — and it carries the symbol, the decimals, the
reserve and the FDV in the same response, so one request settles everything we ask about a token.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

import httpx

from ..config import settings
from ..models import NewToken, norm_addr
from ..ratelimit import RateLimiter
from .filters import filter_tokens

log = logging.getLogger(__name__)
BASE = "https://api.geckoterminal.com/api/v2"

# DexScreener chainId -> GeckoTerminal network id, only where they differ
NETWORK_MAP = {"ethereum": "eth", "bsc": "bsc", "arbitrum": "arbitrum", "polygon": "polygon_pos"}


def feed_path(feed: str) -> tuple[str, dict]:
    """'trending_6h' | 'trending_1h' | 'new' | 'new_2' | 'top_volume' -> (path suffix, params)."""
    if feed.startswith("trending"):
        dur = feed.split("_", 1)[1] if "_" in feed else "6h"
        return "/trending_pools", {"duration": dur}
    if feed.startswith("new"):
        page = int(feed.split("_", 1)[1]) if "_" in feed else 1
        return "/new_pools", {"page": page}
    if feed == "top_volume":
        return "/pools", {"sort": "h24_volume_usd_desc"}
    raise ValueError(f"unknown gecko feed: {feed}")


class Busy(httpx.HTTPError):
    """The allowance is spent and this client was told not to wait for it."""


class GeckoTerminal:
    def __init__(self, client: httpx.Client | None = None, chains: Iterable[str] | None = None,
                 feeds: Iterable[str] | None = None, patient: bool = True):
        self.http = client or httpx.Client(base_url=BASE, timeout=20, headers={"accept": "application/json"})
        self.chains = tuple(chains) if chains else settings.dex_chains
        self.feeds = tuple(feeds) if feeds else settings.gecko_feeds
        self.limiter = RateLimiter(settings.gecko_max_req_per_min, "geckoterminal")
        self._last = 0.0
        self.included: list[dict] = []   # the `included` block of the last answer, for callers that asked for it
        # A batch job waits its turn and backs off on 429; that is the right shape for a pass that
        # must finish. A request thread serving the public must never sleep on anybody's limit:
        # with bots walking thousands of token pages, forty threads asleep on GeckoTerminal's
        # thirty-a-minute is the whole API hanging. The impatient client asks whether there is
        # room, and if there is not, or the answer is 429, it says so at once.
        self.patient = patient

    def pools(self, chain: str, feed: str) -> list[dict]:
        network = NETWORK_MAP.get(chain, chain)
        suffix, params = feed_path(feed)
        params["include"] = "base_token"
        for attempt in range(3):
            gap = settings.gecko_min_interval_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self.limiter.wait()
            r = self.http.get(f"/networks/{network}{suffix}", params=params)
            self._last = time.monotonic()
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning("geckoterminal 429 on %s %s; backing off %ds", chain, feed, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        log.warning("geckoterminal %s %s: still 429 after retries, skipping", chain, feed)
        return []

    def _get(self, path: str, params: dict | None = None) -> list[dict]:
        """One GET against the rate limit, backing off on 429 and giving up rather than hammering."""
        if not self.patient:
            if not self.limiter.room():
                raise Busy(f"geckoterminal: allowance spent, not waiting for {path}")
            self.limiter.wait()   # there is room, so this returns at once
            r = self.http.get(path, params=params or {})
            self._last = time.monotonic()
            if r.status_code == 429:
                raise Busy(f"geckoterminal: 429 on {path}, not backing off")
            r.raise_for_status()
            body = r.json()
            self.included = body.get("included") or []
            data = body.get("data", [])
            return data if isinstance(data, list) else [data]
        for attempt in range(3):
            gap = settings.gecko_min_interval_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self.limiter.wait()
            r = self.http.get(path, params=params or {})
            self._last = time.monotonic()
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning("geckoterminal 429 on %s; backing off %ds", path, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            body = r.json()
            self.included = body.get("included") or []
            data = body.get("data", [])
            return data if isinstance(data, list) else [data]
        log.warning("geckoterminal %s: still 429 after retries, skipping", path)
        return []

    def tokens(self, chain: str, addresses: Iterable[str]) -> list[NewToken]:
        """Look tokens up by address, thirty at a time. The one call that prices this chain."""
        network = NETWORK_MAP.get(chain, chain)
        out: list[NewToken] = []
        addresses = [a for a in addresses if a]
        for i in range(0, len(addresses), 30):
            chunk = ",".join(addresses[i:i + 30])
            try:
                items = self._get(f"/networks/{network}/tokens/multi/{chunk}", {"include": "top_pools"})
            except httpx.HTTPError as e:
                log.warning("geckoterminal token lookup on %s failed: %s", chain, e)
                continue
            pools = {p.get("id"): p for p in (self.included or []) if p.get("type") == "pool"}
            out.extend(t for t in (parse_token(it, chain, pools) for it in items) if t)
        return out

    def pool_ages(self, chain: str, pools: Iterable[str]) -> dict[str, int]:
        """When each pool opened, keyed by pool address. Thirty at a time.

        The token endpoint does not carry a creation time — only the pool endpoint does — which is
        why a token discovered from our own tape arrives undated while one found on the new-pools
        feed does not. That gap reached the reader: the fresh feed is ranked by how early each
        wallet was, and 43% of tokens had no launch time to be early against.
        """
        network = NETWORK_MAP.get(chain, chain)
        out: dict[str, int] = {}
        pools = [p for p in pools if p]
        for i in range(0, len(pools), 30):
            chunk = ",".join(pools[i:i + 30])
            try:
                items = self._get(f"/networks/{network}/pools/multi/{chunk}")
            except httpx.HTTPError as e:
                log.warning("geckoterminal pool lookup on %s failed: %s", chain, e)
                continue
            for it in items:
                t = parse_pool(it)
                addr = ((it.get("attributes") or {}).get("address")) or ""
                if t and t.created_at and addr:
                    out[norm_addr(addr)] = t.created_at
        return out

    def pool(self, chain: str, pool: str) -> dict | None:
        """One pool's attributes: transactions by window, when it opened. None if it cannot say."""
        network = NETWORK_MAP.get(chain, chain)
        try:
            items = self._get(f"/networks/{network}/pools/{pool}")
        except httpx.HTTPError as e:
            log.debug("geckoterminal pool %s/%s: %s", network, pool, e)
            return None
        if not items:
            return None
        a = (items[0] or {}).get("attributes") or {}
        created = a.get("pool_created_at")
        try:
            created_ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
        except ValueError:
            created_ts = None
        return {"transactions": a.get("transactions") or {}, "created_at": created_ts,
                "reserve_usd": _f(a.get("reserve_in_usd")), "address": a.get("address")}

    def ohlcv(self, chain: str, pool: str, timeframe: str = "hour", aggregate: int = 1,
              limit: int = 168) -> list[list[float]]:
        """Candles for one pool: [timestamp, open, high, low, close, volume], oldest first.

        The chart on a token page is drawn from these rather than from an embedded widget. Every
        third-party widget was tried on 2026-09-08: DexScreener never finishes loading a Robinhood
        pair because it does not index the chain, GeckoTerminal's own iframe renders its chrome and
        no candles, and defined.fi frames its whole app with a sign-in bar over it. The data itself
        is here and complete, so the page draws it in its own hand.
        """
        network = NETWORK_MAP.get(chain, chain)
        try:
            data = self._get(f"/networks/{network}/pools/{pool}/ohlcv/{timeframe}",
                             {"aggregate": aggregate, "limit": min(limit, 1000)})
        except Busy:
            log.debug("geckoterminal ohlcv %s/%s: busy, no candles this time", network, pool)
            return []
        except httpx.HTTPError as e:
            log.warning("geckoterminal ohlcv %s/%s failed: %s", network, pool, e)
            return []
        return parse_ohlcv(data[0] if data else None)

    def new_tokens(self, min_mcap: float | None = None, max_age_hours: float | None = None) -> list[NewToken]:
        min_mcap = settings.new_token_min_mcap_usd if min_mcap is None else min_mcap
        max_age_hours = settings.new_token_max_age_hours if max_age_hours is None else max_age_hours
        tokens: list[NewToken] = []
        for chain in self.chains:
            for feed in self.feeds:
                try:
                    tokens.extend(t for t in map(parse_pool, self.pools(chain, feed)) if t)
                except httpx.HTTPError as e:
                    log.warning("geckoterminal %s %s failed: %s", chain, feed, e)
        return filter_tokens(tokens, min_mcap=min_mcap, max_age_hours=max_age_hours)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_ohlcv(payload: dict | None) -> list[list[float]]:
    """Pure: the ohlcv response -> candles oldest first, dropping any row that is not a number.

    The API answers newest first and occasionally carries a candle with a null in it; a chart that
    reverses the axis or plots a null is worse than a chart that is one bar shorter.
    """
    rows = ((payload or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            ts, o, h, low, c, v = (float(x) for x in row[:6])
        except (TypeError, ValueError):
            continue
        if h > 0 and low > 0:
            out.append([int(ts), o, h, low, c, v])
    return sorted(out, key=lambda r: r[0])


JUNK_POOL_FEE = 10.0   # percent: a pool with a fee this high is a trap, not a market


def pool_fee(name: str | None) -> float | None:
    """The fee tier GeckoTerminal writes into a pool's name: "SYNTH / USDG 89%" -> 89.0."""
    tail = (name or "").rsplit(" ", 1)[-1]
    if tail.endswith("%"):
        try:
            return float(tail[:-1])
        except ValueError:
            return None
    return None


def best_pool(ids: list[str], pools: dict) -> tuple[str | None, dict | None]:
    """The pool to read a token by: the deepest one whose fee is not a trap.

    GeckoTerminal's own ranking put an 89%-fee pool holding $85 first for SYNTH, and the token's
    price came from a single $253 trade in it - 384x the launch, on a token that had gone
    nowhere. Given the pools' attributes, prefer the largest reserve among sane fees; without
    attributes (an older cache, a test), the first id is all there is.
    """
    if not ids:
        return None, None
    rows = [(i, (pools.get(i) or {}).get("attributes") or {}) for i in ids]
    known = [(i, a) for i, a in rows if a]
    if not known:
        return ids[0].split("_", 1)[-1], None
    sane = [(i, a) for i, a in known if (pool_fee(a.get("name")) or 0) < JUNK_POOL_FEE] or known
    i, a = max(sane, key=lambda r: _f(r[1].get("reserve_in_usd")) or 0)
    return i.split("_", 1)[-1], a


def parse_token(item: dict, chain: str, pools: dict | None = None) -> NewToken | None:
    """Pure: one entry of /tokens/multi -> NewToken.

    Everything the rest of the pipeline asks about a token arrives here at once: what to call it,
    how many base units make one of it, what it is worth, and how deep the market behind that
    price is. `market_cap_usd` is usually null on a memecoin, so FDV stands in for it. With the
    pools' attributes in hand (`include=top_pools`), the price and the chart come from the deepest
    sane pool rather than whichever one GeckoTerminal listed first.
    """
    a = item.get("attributes") or {}
    address = a.get("address")
    if not address:
        return None
    decimals = a.get("decimals")
    # Ids arrive prefixed with the network ("robinhood_0x...") because the same call can span chains.
    ids = [p.get("id") or "" for p in (((item.get("relationships") or {}).get("top_pools") or {}).get("data") or [])]
    pool, pa = best_pool(ids, pools or {})
    price = _f(a.get("price_usd"))
    if pa:
        base = (((pools or {}).get(next(i for i in ids if i.endswith(pool)), {}).get("relationships") or {}).get("base_token") or {}).get("data") or {}
        ours = (base.get("id") or "").lower().endswith(norm_addr(address))
        pool_price = _f(pa.get("base_token_price_usd" if ours else "quote_token_price_usd"))
        if pool_price:
            price = pool_price
    return NewToken(
        mint=norm_addr(address),
        chain=chain,
        symbol=a.get("symbol") or a.get("name") or None,
        mcap_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
        liquidity_usd=_f(a.get("total_reserve_in_usd")),
        price_usd=price,
        decimals=int(decimals) if isinstance(decimals, int) and 0 <= decimals <= 36 else None,
        pool_address=pool or None,
        source="geckoterminal",
    )


def parse_pool(p: dict) -> NewToken | None:
    """Pure: GeckoTerminal pool object -> NewToken. mcap falls back to FDV (mcap is often null)."""
    a = p.get("attributes") or {}
    rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    tid = rel.get("id") or ""          # "<network>_<address>"
    network, _, address = tid.partition("_")
    if not address:
        return None
    created = a.get("pool_created_at")
    try:
        created_ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
    except ValueError:
        created_ts = None
    name = a.get("name") or ""
    symbol = name.split(" / ")[0].strip() if " / " in name else name or None
    chain = next((k for k, v in NETWORK_MAP.items() if v == network), network)
    return NewToken(
        mint=norm_addr(address),
        chain=chain,
        symbol=symbol,
        mcap_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
        liquidity_usd=_f(a.get("reserve_in_usd")),
        created_at=created_ts,
        dex=((p.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id"),
        pair_address=(a.get("address")),
        source="geckoterminal",
    )
