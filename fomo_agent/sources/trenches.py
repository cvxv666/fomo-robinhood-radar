"""rhtrenches.com — a public, keyless JSON API over fomo.family traders on Robinhood Chain.

Someone else already indexes what we need for chain 4663: they watch transfer logs over a websocket
and keep a live tape for ~108 curated fomo wallets. Their `/api/traders` hands us the one thing
fomo's own API hides behind Cloudflare — the mapping from a fomo handle to the wallet that actually
executes trades — plus realized PnL, win rate and hold times already computed.

Scope and etiquette:
  - Robinhood Chain only. Solana and Base stay on Codex.
  - Third-party and unofficial: every call fails soft, and the loop keeps working without it.
  - The site has moved once already (robinhoodtrenches.com -> rhtrenches.com, a 301 on every
    path); the client follows redirects so the next move degrades to a log line, not a dead source.
  - `robots.txt` sets no restrictions, but we still identify ourselves, cache, and poll slowly
    (TRENCHES_MIN_INTERVAL_S). `/api/tape` returns at most 500 newest fills and ignores any
    pagination parameter, so polling faster than the tape moves buys nothing.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..models import Trade, norm_addr

log = logging.getLogger(__name__)

CHAIN = "robinhood"
WINDOWS = ("1h", "24h", "7d", "30d", "all")
TAPE_MAX = 500


class TrenchesError(RuntimeError):
    pass


class Trenches:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or settings.trenches_base_url).rstrip("/")
        self.http = client or httpx.Client(
            base_url=self.base_url, timeout=25, follow_redirects=True,
            headers={"accept": "application/json", "user-agent": settings.trenches_user_agent},
        )
        self._cache: dict[str, tuple[float, object]] = {}
        self.requests = 0

    def _get(self, path: str, ttl: float | None = None):
        ttl = settings.trenches_min_interval_s if ttl is None else ttl
        hit = self._cache.get(path)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        self.requests += 1
        r = self.http.get(path)
        if r.status_code == 422:
            raise TrenchesError(f"trenches rejected {path} (422): parameter out of range")
        r.raise_for_status()
        data = r.json()
        self._cache[path] = (time.monotonic(), data)
        return data

    # ---------- endpoints ----------

    def status(self) -> dict:
        return self._get("/api/status", ttl=60)

    def traders(self, window: str = "24h", stocks: bool = False) -> list[dict]:
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}")
        return self._get(f"/api/traders?window={window}&stocks={str(stocks).lower()}")

    def tape(self, limit: int = TAPE_MAX, stocks: bool = True) -> list[dict]:
        limit = max(1, min(limit, TAPE_MAX))
        return self._get(f"/api/tape?limit={limit}&stocks={str(stocks).lower()}")

    def closed(self, window: str = "24h", limit: int = 80, stocks: bool = False) -> list[dict]:
        return self._get(f"/api/closed?window={window}&stocks={str(stocks).lower()}&limit={limit}")

    def tokens(self, window: str = "24h", limit: int = 60, stocks: bool = False) -> list[dict]:
        return self._get(f"/api/tokens?window={window}&stocks={str(stocks).lower()}&limit={limit}")

    def overview(self, window: str = "24h", stocks: bool = False) -> dict:
        return self._get(f"/api/overview?window={window}&stocks={str(stocks).lower()}")

    # ---------- Tracker protocol (see pipeline/track.py) ----------

    def supports(self, chain: str) -> bool:
        return chain == CHAIN

    def covers(self, address: str) -> bool:
        """Their tape only carries their own curated roster, so a wallet we found ourselves
        would silently come back empty. Saying so lets the tracker chain fall through to Codex."""
        try:
            roster = {norm_addr(t["address"]) for t in self.traders(settings.trenches_window) if t.get("address")}
        except (httpx.HTTPError, TrenchesError) as e:
            log.warning("trenches roster unavailable: %s", e)
            return False
        return norm_addr(address) in roster

    def get_trades(self, address: str, chain: str = CHAIN, since_ts: int | None = None) -> list[Trade]:
        """Trades for one wallet, sliced out of the shared tape.

        The tape is global, so it is fetched once and cached; tracking N wallets in a pass costs
        one HTTP request, not N.
        """
        fills = self.tape(TAPE_MAX, stocks=settings.trenches_include_stocks)
        wanted = norm_addr(address)
        out = []
        for f in fills:
            if norm_addr(f.get("wallet") or "") != wanted:
                continue
            t = parse_fill(f)
            if t and (since_ts is None or t.ts > since_ts):
                out.append(t)
        return out


# ---------- pure parsers (fixture-testable) ----------

def _f(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def parse_fill(f: dict) -> Trade | None:
    """One tape entry -> Trade. `tx` is the signature; a wallet can appear twice in one tx."""
    tx, wallet, token = f.get("tx"), f.get("wallet"), f.get("token")
    side = (f.get("side") or "").lower()
    ts = f.get("ts")
    if not (tx and wallet and token and ts) or side not in ("buy", "sell"):
        return None
    fill_id = f.get("id")
    return Trade(
        sig=f"{tx}:{fill_id}" if fill_id is not None else tx,
        address=norm_addr(wallet),
        chain=CHAIN,
        mint=norm_addr(token),
        side=side,
        sol_amount=None,                       # Robinhood Chain has no SOL leg
        token_amount=_f(f.get("amount")),
        usd_value=_f(f.get("usd")),
        ts=int(ts),
        source="trenches",
    )


TRADER_STAT_FIELDS = (
    "fills", "buys", "sells", "closed_trades", "wins", "win_rate", "realized_pnl",
    "unrealized_pnl", "net_pnl", "open_bags", "open_cost", "open_value", "volume",
    "best_trade", "worst_trade", "state", "active", "last_ts", "followers",
)


def parse_trader(d: dict) -> dict | None:
    """One `/api/traders` row -> fields for `traders`, plus a stats blob for the scoring context."""
    address = d.get("address")
    if not address:
        return None
    win_rate = _f(d.get("win_rate"))
    return {
        "address": norm_addr(address),
        "chain": CHAIN,
        "fomo_handle": d.get("handle"),
        "trades_cnt": d.get("fills"),
        "volume_usd": _f(d.get("volume")),
        "win_rate": win_rate / 100 if win_rate is not None and win_rate > 1 else win_rate,
        "stats": {k: d.get(k) for k in TRADER_STAT_FIELDS if d.get(k) is not None},
    }
