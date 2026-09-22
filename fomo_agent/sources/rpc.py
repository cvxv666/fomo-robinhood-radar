"""Robinhood Chain's own public JSON-RPC as a wallet-trade source — free, keyless, unmetered.

Codex charges roughly one request per wallet per pass, which is what makes its 10k/month budget the
binding constraint on this whole project. The chain's public endpoint gives the same fills for
nothing, because `eth_getLogs` accepts a *list* of values for a topic position: one request asks
"every ERC-20 Transfer whose receiver is any of these 300 wallets", a second asks the same for the
sender, and together they cover the entire roster however large it grows.

How a fomo trade looks on chain 4663 (measured Sep 2026):

  * A relayer submits the transaction; the trader's wallet is never its `from`. Every fill is
    routed through one contract, and the wallet's only leg is the token arriving from it (a buy) or
    leaving to it (a sell). Filtering on that counterparty separates real fills from the airdrops
    and plain transfers that make up most of a wallet's log traffic.
  * Exactly one fill per transaction, so the trade's size is unambiguous: it is the WETH moved
    inside that transaction, and the wallet's own token leg gives the direction.
  * Blocks land every ~0.1s. A 200k-block query spans ~5.6 hours and is the widest window the
    endpoint serves before answering "log query timed out", so timestamps are interpolated from two
    probes rather than fetched per block.

Requests are rate-limited: the endpoint starts returning 429 long before it returns bad data. It
also rejects the default httpx user agent outright, hence RPC_USER_AGENT.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

import httpx

from ..config import settings
from ..models import Trade, norm_addr
from ..ratelimit import RateLimiter

log = logging.getLogger(__name__)

CHAIN = "robinhood"
CHAIN_ID = 4663

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# ERC-20 decimals(): the first four bytes of keccak256("decimals()")
DECIMALS_SELECTOR = "0x313ce567"
# ERC-20 balanceOf(address): the selector, then the address padded to 32 bytes
BALANCE_SELECTOR = "0x70a08231"
# what an ERC-20 uses unless it says otherwise, and the only sane guess for one that will not answer
DEFAULT_DECIMALS = 18

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

# Assets a swap is denominated in rather than positions anyone takes. USDG is the chain's dollar
# stablecoin; both of these appear on the quote side of trades and must never read as a signal.
QUOTE_TOKENS = {WETH: ("WETH", 18), USDG: ("USDG", 6)}
# a v4 pool against the chain's own ether has the zero address for it; the launchpad's pools do
NATIVE = "0x" + "0" * 40
POOL_QUOTES = {NATIVE: ("ETH", 18), **QUOTE_TOKENS}
# the pool manager's events, by topic: a pool opening (its id and its two currencies, indexed),
# and a swap in it (the id indexed; amount0 and amount1 as signed 128-bit words in the data)
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def _int128(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


class RpcError(RuntimeError):
    pass


def topic_for(address: str) -> str:
    """An address as a 32-byte log topic: twelve zero bytes, then the twenty address bytes."""
    return "0x" + "0" * 24 + address[2:].lower()


def address_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def parse_transfer(entry: dict) -> dict | None:
    """One ERC-20 Transfer log into {token, frm, to, raw, tx, index, block}, or None if it isn't one."""
    topics = entry.get("topics") or []
    if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
        return None
    data = entry.get("data") or "0x"
    try:
        return {
            "token": norm_addr(entry["address"]),
            "frm": address_from_topic(topics[1]),
            "to": address_from_topic(topics[2]),
            "raw": int(data, 16) if data not in ("", "0x") else 0,
            "tx": entry["transactionHash"],
            "index": int(entry["logIndex"], 16),
            "block": int(entry["blockNumber"], 16),
        }
    except (ValueError, KeyError):
        return None


def routed_fills(transfers: list[dict], wallets: set[str], routers: set[str]) -> dict[str, dict]:
    """Keep only the legs traded against a router, keyed by transaction.

    A wallet receiving a token from the router bought it; sending one to the router sold it.
    Everything else in the wallet's log traffic — airdrops, transfers between a user's own
    accounts, LP moves — has no router leg and is not a trade.
    """
    fills: dict[str, dict] = {}
    for t in transfers:
        if t["to"] in wallets and t["frm"] in routers:
            side, wallet = "buy", t["to"]
        elif t["frm"] in wallets and t["to"] in routers:
            side, wallet = "sell", t["frm"]
        else:
            continue
        fills[t["tx"]] = {"side": side, "wallet": wallet, "mint": t["token"],
                          "raw": t["raw"], "index": t["index"], "block": t["block"]}
    return fills


def fill_kind(receipt: dict, routers: set[str]) -> str:
    """`direct` when the transaction was sent to the router itself; `flow` when it came through
    fomo's entrypoint the way every real app trade does.

    A fomo user never calls the router: the app's relayers send to an entrypoint that calls it.
    An outside key that wants a swap delivered to somebody else's wallet has to call the router
    itself, and the receipt says so. Judged on `to` alone — the signer is a relayer either way.
    """
    return "direct" if (receipt.get("to") or "").lower() in routers else "flow"


def quote_legs(receipt: dict) -> dict[str, float]:
    """The largest leg of each quote asset in the transaction, in human units.

    A swap moves the same quote amount through several hops (deposit, pool, payout), so summing the
    legs double-counts; the largest single leg is what the trader actually paid or received. Routes
    are quoted in USDG or in WETH depending on the pool, so both are read.
    """
    biggest: dict[str, int] = {}
    for t in (parse_transfer(e) for e in receipt.get("logs") or []):
        if t and t["token"] in QUOTE_TOKENS and t["raw"] > biggest.get(t["token"], 0):
            biggest[t["token"]] = t["raw"]
    return {token: raw / 10 ** QUOTE_TOKENS[token][1] for token, raw in biggest.items()}


def fill_usd(legs: dict[str, float], eth_price: float | None) -> float | None:
    """Dollar size of a fill: USDG is a dollar, WETH needs a price. USDG wins when both appear."""
    if USDG in legs:
        return legs[USDG]
    if WETH in legs and eth_price:
        return legs[WETH] * eth_price
    return None


class RobinhoodRPC:
    """Tracker over the chain's public RPC. Two requests find the fills; receipts are batched."""

    def __init__(self, url: str | None = None, client: httpx.Client | None = None, timeout: float = 90):
        self.url = url or settings.rpc_url
        # the endpoints in the order they are asked; a 429 on one moves the next call to the next
        self.urls = [self.url] + [u for u in settings.rpc_urls if u != self.url]
        self.spared = 0   # calls the extra endpoints answered while the node was rate-limiting
        # ninety seconds suits a 200k-block scan; the watcher passes its own, shorter one
        self.http = client or httpx.Client(
            timeout=timeout,
            headers={"content-type": "application/json", "user-agent": settings.rpc_user_agent},
        )
        self.routers = {r.lower() for r in settings.rpc_routers}
        self.limiter = RateLimiter(settings.rpc_max_per_min, name="robinhood-rpc")
        self.requests = 0
        self.eth_price: float | None = None
        self._wallets: list[str] = []
        self._fills: dict[str, list[Trade]] = {}
        self._fetched_at = 0.0
        self._failed: str | None = None
        self._max_span: int | None = None   # blocks per eth_getLogs the node has been seen to answer
        self._known: set[str] = set()       # transactions the tape already holds; see skip_known
        self._probes: dict[int, int] = {}   # block -> timestamp, for dating an arbitrary moment
        self._decimals: dict[str, int] = {}  # token -> decimals; constant, so cached for the run
        self._pools: dict[str, tuple[str, str]] = {}   # pool id -> (currency0, currency1), from Initialize

    # ---------- transport ----------

    @staticmethod
    def _unsupported(body: object) -> bool:
        """A keyed free-tier endpoint answering "not on this plan" has not answered."""
        if isinstance(body, list):
            return False
        err = (body or {}).get("error") if isinstance(body, dict) else None
        msg = (err or {}).get("message", "").lower() if isinstance(err, dict) else ""
        return "not supported" in msg or "free plan" in msg or "upgrade" in msg

    def _post(self, payload: object) -> object:
        """One JSON-RPC round trip. The first endpoint is the chain's own node and is always
        asked first; the others are asked only for a call it rate-limited, one call at a time,
        never as a switch. A free-tier extra answers a batch with a 500 and a log range over a
        hundred blocks with "not supported on free plan"; either is no answer, and the call goes
        back to waiting on the node rather than failing on the extra."""
        self.limiter.wait()
        self.requests += 1
        for attempt in range(4):
            r = self.http.post(self.urls[0], json=payload)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            for alt in self.urls[1:]:
                try:
                    r2 = self.http.post(alt, json=payload)
                except httpx.HTTPError:
                    continue
                if r2.status_code == 200:
                    body = r2.json()
                    if not self._unsupported(body):
                        self.spared += 1
                        return body
            time.sleep(2 + 2 * attempt)
        raise RpcError("rate limited after 4 attempts")

    def call(self, method: str, params: list) -> object:
        body = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in body:
            raise RpcError(f"{method}: {body['error'].get('message')}")
        return body["result"]

    @staticmethod
    def node_failed(e: Exception) -> bool:
        """The node's own fault, not ours: a backend behind it timed out or threw. Seen from
        2026-09-15 as `Post "http://10.31.x.x:8547/rpc": context deadline exceeded` and
        `internal server error`, dozens an hour. Retried sooner, and read through the spare."""
        s = str(e).lower()
        return "deadline exceeded" in s or "internal server" in s or "connection refused" in s or "bad gateway" in s

    def _post_to(self, url: str, payload: object) -> object | None:
        """One call to one endpoint, no retries: the body, or None if it did not answer."""
        self.limiter.wait()
        self.requests += 1
        try:
            r = self.http.post(url, json=payload)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        body = r.json()
        return None if self._unsupported(body) else body

    def _logs_via_spare(self, from_block: int, to_block: int, flt: dict) -> list | None:
        """The range read through the spare endpoints in slices their plans allow, or None."""
        step = max(1, settings.rpc_spare_slice_blocks)
        for alt in self.urls[1:]:
            out: list = []
            for start in range(from_block, to_block + 1, step):
                body = self._post_to(alt, {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                                           "params": [{"fromBlock": hex(start), "toBlock": hex(min(start + step - 1, to_block)), **flt}]})
                if not body or "error" in body:
                    out = None
                    break
                out += body.get("result") or []
            if out is not None:
                self.spared += 1
                return out
        return None

    @staticmethod
    def _too_big(e: Exception) -> bool:
        """The node refusing a range for its size, not for our rate: split it and ask again."""
        s = str(e).lower()
        return "exceeds limit" in s or "timed out" in s or "too many" in s or "response size" in s

    def logs(self, from_block: int, to_block: int, **flt) -> list:
        """`eth_getLogs` over a range, halving the range as often as the node demands.

        On 2026-09-10 the public node began capping an answer at 10,000 logs; on the 13th the
        roster's two 200k-block calls crossed it and every wallet's tracking failed - four
        hundred wallets, eight hundred requests, no fills, and the watcher starved behind them.
        A range the node will not answer whole is answered in halves, and the halves in halves;
        the depth is logarithmic, so a day of the roster costs a dozen calls, not a thousand.
        """
        span = to_block - from_block + 1
        if self._max_span and span > self._max_span:
            # a size the node already refused once this process is not asked for again
            out: list = []
            step = self._max_span   # fixed for this loop: a refusal inside it shrinks the next one
            for start in range(from_block, to_block + 1, step):
                out += self.logs(start, min(start + step - 1, to_block), **flt)
            return out
        try:
            return self.call("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block), **flt}])
        except RpcError as e:
            if (self.node_failed(e) or "rate limited" in str(e)) and len(self.urls) > 1:
                # the node's backend is down for this call, or it has throttled us out of every
                # retry; the spare reads the same blocks a hundred at a time, which is what its
                # plan allows and what a tick (or a PRO burn check) needs
                got = self._logs_via_spare(from_block, to_block, flt)
                if got is not None:
                    log.info("rpc: node failed on %d..%d (%s); read through the spare", from_block, to_block, str(e)[:60])
                    return got
            if from_block >= to_block or not self._too_big(e):
                raise
            self._max_span = max(1, min(self._max_span or span, span // 2))
            log.info("rpc: %d..%d too big for one answer (%s); %d blocks a call from here",
                     from_block, to_block, str(e)[:60], self._max_span)
            return self.logs(from_block, to_block, **flt)

    def batch(self, method: str, args: list[list], size: int | None = None) -> list:
        """One HTTP round trip per `size` calls; results come back aligned with `args`."""
        size = size or settings.rpc_batch_size
        out: list = [None] * len(args)
        for start in range(0, len(args), size):
            chunk = args[start:start + size]
            body = self._post([{"jsonrpc": "2.0", "id": start + i, "method": method, "params": p}
                               for i, p in enumerate(chunk)])
            if not isinstance(body, list):
                raise RpcError(f"{method}: batch rejected")
            for item in body:
                idx = item.get("id")
                if isinstance(idx, int) and 0 <= idx < len(args):
                    out[idx] = item.get("result")
        return out

    def decimals(self, mints: list[str]) -> dict[str, int]:
        """How many base units make one token, for each mint, batched and remembered.

        A log carries the amount as an integer in the token's own base units, so without this a
        fill cannot be compared to the same token's other fills, let alone to another token. The
        answer never changes, so one batched call covers whatever is new to this process and the
        rest come from memory.
        """
        want = sorted({m for m in mints if m not in self._decimals})
        if want:
            log.debug("rpc: asking for decimals of %d new tokens", len(want))
        if want:
            calls = [[{"to": m, "data": DECIMALS_SELECTOR}, "latest"] for m in want]
            for mint, raw in zip(want, self.batch("eth_call", calls)):
                try:
                    value = int(raw, 16) if raw and raw != "0x" else DEFAULT_DECIMALS
                except (TypeError, ValueError):
                    value = DEFAULT_DECIMALS
                # a contract answering something absurd is answering something else entirely
                self._decimals[mint] = value if 0 <= value <= 36 else DEFAULT_DECIMALS
        return {m: self._decimals.get(m, DEFAULT_DECIMALS) for m in mints}

    def balances(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
        """What each wallet actually holds of each token, asked of the chain itself.

        The tape says what we watched a wallet buy and sell; this says what it has. The two differ
        whenever a position was opened before we started watching, or a fill was recorded without
        a size — and the difference is the whole gap between a book that is roughly right and one
        that is right. `balanceOf` is a free read, forty to a round trip.
        """
        pairs = [(w.lower(), m) for w, m in pairs if w and m]
        if not pairs:
            return {}
        dec = self.decimals(sorted({m for _, m in pairs}))
        calls = [[{"to": m, "data": BALANCE_SELECTOR + topic_for(w)[2:]}, "latest"] for w, m in pairs]
        out: dict[tuple[str, str], float] = {}
        for (wallet, mint), raw in zip(pairs, self.batch("eth_call", calls)):
            try:
                units = int(raw, 16)
            except (TypeError, ValueError):
                continue  # a token that will not answer leaves the position as the tape had it
            out[(wallet, mint)] = units / 10 ** dec.get(mint, DEFAULT_DECIMALS)
        return out

    def load_decimals(self, known: dict[str, int]) -> None:
        """Seed the cache from storage. Each pass is a fresh process; the answers are not."""
        self._decimals.update({norm_addr(m): int(d) for m, d in known.items() if d is not None})

    def known_decimals(self) -> dict[str, int]:
        return dict(self._decimals)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def head(self) -> tuple[int, int]:
        """The newest block and its timestamp in one request, remembered as a probe."""
        b = self.call("eth_getBlockByNumber", ["latest", False])
        n, ts = int(b["number"], 16), int(b["timestamp"], 16)
        self._remember(n, ts)
        return n, ts

    def block_timestamp(self, block: int) -> int:
        if block not in self._probes:
            self._remember(block, int(self.call("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16))
        return self._probes[block]

    def _remember(self, block: int, ts: int) -> None:
        """Every dated block is a probe. A process that dates a block every twenty seconds for a
        week would otherwise hold thirty thousand of them for no reason."""
        self._probes[block] = ts
        if len(self._probes) > 64:
            for b in sorted(self._probes)[:-48]:
                del self._probes[b]

    def block_at(self, ts: int, tolerance_s: int = 20, max_probes: int = 6) -> int:
        """The block nearest a wall-clock time.

        Interpolating between two probes is exact over hours but drifts by hours over months,
        because the chain has not always run at today's pace. So the estimate is refined against
        the block it lands on, using the local rate each probe reveals; every probe is cached, and
        recent timestamps converge on the first one.
        """
        if len(self._probes) < 2:
            head = self.block_number()
            self._probes[head] = self.block_timestamp(head)
            far = max(head - settings.rpc_window_blocks, 1)
            self._probes[far] = self.block_timestamp(far)

        for _ in range(max_probes):
            below = [b for b, t in self._probes.items() if t <= ts]
            above = [b for b, t in self._probes.items() if t > ts]
            lo = max(below) if below else min(self._probes)
            hi = min(above) if above else max(self._probes)
            if lo == hi:  # every probe sits on one side; extrapolate from the two nearest
                lo, hi = sorted(self._probes)[:2] if ts < self._probes[min(self._probes)]                     else sorted(self._probes)[-2:]
            rate = (self._probes[hi] - self._probes[lo]) / max(hi - lo, 1) or 0.1
            guess = max(int(lo + (ts - self._probes[lo]) / rate), 1)
            if guess in self._probes:
                break
            self._probes[guess] = self.block_timestamp(guess)
            if abs(self._probes[guess] - ts) <= tolerance_s:
                return guess
        return min(self._probes, key=lambda b: abs(self._probes[b] - ts))

    def token_makers(self, token: str, ts: int, side: str, window_s: int | None = None) -> set[str]:
        """Everyone who traded `token` the same way within a few seconds of `ts`.

        This is the Codex `getTokenEvents` call that wallet resolution runs on, for free: filtering
        `eth_getLogs` by the token's own address gives every transfer of it in the window, and the
        legs facing a router are the trades. One request per window.
        """
        window_s = settings.resolve_window_s if window_s is None else window_s
        centre = self.block_at(ts)
        span = max(int(window_s / 0.1), 1)
        raw = self.logs(max(centre - span, 1), centre + span, address=norm_addr(token), topics=[TRANSFER_TOPIC])
        makers = set()
        for t in (parse_transfer(e) for e in raw):
            if t is None:
                continue
            if side == "buy" and t["frm"] in self.routers:
                makers.add(t["to"])
            elif side == "sell" and t["to"] in self.routers:
                makers.add(t["frm"])
        return makers

    def transfers(self, wallets: list[str], from_block: int, to_block: int, *, outgoing: bool) -> list[dict]:
        """Every Transfer in the range where any of `wallets` is the sender (or the receiver)."""
        topics: list = [TRANSFER_TOPIC, None, None]
        topics[1 if outgoing else 2] = [topic_for(w) for w in wallets]
        raw = self.logs(from_block, to_block, topics=topics)
        return [t for t in (parse_transfer(e) for e in raw) if t]

    # ---------- a pool by its id ----------

    def pool_currencies(self, pool_id: str, created_ts: int | None = None) -> tuple[str, str] | None:
        """The two currencies of a pool, from the pool manager's Initialize event: around the
        block the pool opened when the caller knows the time, else the recent window. Remembered."""
        if not pool_id or not pool_id.startswith("0x") or len(pool_id) != 66:
            return None
        pool_id = pool_id.lower()
        if pool_id in self._pools:
            return self._pools[pool_id]
        if created_ts:
            b = self.block_at(created_ts)
            first, last = max(b - 3000, 0), b + 3000
        else:
            last = self.block_number()
            first = max(last - settings.rpc_window_blocks, 0)
        logs = self.logs(first, last, address=settings.rpc_pool_manager, topics=[INITIALIZE_TOPIC, pool_id])
        if not logs:
            return None
        t = logs[0]["topics"]
        cur = ("0x" + t[2][-40:], "0x" + t[3][-40:])
        self._pools[pool_id] = cur
        return cur

    def pool_volume_usd(self, pool_id: str, since_ts: int, created_ts: int | None = None) -> float | None:
        """Dollars swapped in a pool since `since_ts`, from its own Swap events: the quote side of
        every swap, summed, priced. None when the pool's currencies are unknown or neither is a
        quote asset. Two or three requests; the screener needs minutes to index a new pool and
        this needs a block."""
        cur = self.pool_currencies(pool_id, created_ts)
        if not cur:
            return None
        side = 0 if cur[0] in POOL_QUOTES else 1 if cur[1] in POOL_QUOTES else None
        if side is None:
            return None
        sym, dec = POOL_QUOTES[cur[side]]
        first = self.block_at(since_ts)
        logs = self.logs(first, self.block_number(), address=settings.rpc_pool_manager, topics=[SWAP_TOPIC, pool_id.lower()])
        units = 0
        for lg in logs:
            data = (lg.get("data") or "0x")[2:]
            if len(data) < 128:
                continue
            units += abs(_int128(data[side * 64:(side + 1) * 64]))
        amount = units / 10 ** dec
        if sym == "USDG":
            return amount
        price = self.weth_price()
        return amount * price if price else None

    def weth_price(self) -> float | None:
        """WETH in dollars, from DexScreener — one free request, cached for the process."""
        if self.eth_price is None:
            try:
                from .dexscreener import DexScreener

                pairs = DexScreener().pairs_for(CHAIN, [WETH])
                prices = [float(p["priceUsd"]) for p in pairs if p.get("priceUsd")]
                self.eth_price = max(prices) if prices else None
            except Exception as e:  # noqa: BLE001 - without a price the fills still carry direction
                log.warning("weth price lookup failed: %s", e)
        return self.eth_price

    # ---------- Tracker protocol ----------

    def supports(self, chain: str) -> bool:
        return chain == CHAIN

    def covers(self, address: str) -> bool:
        return address.lower() in self._wallets

    def prime(self, addresses: list[str], chain: str = CHAIN) -> None:
        """Declare the roster to index. Changing it forces the next pass to refetch."""
        wallets = sorted({a.lower() for a in addresses if a.startswith("0x")})
        if wallets and wallets != self._wallets:
            self._wallets = wallets
            self._fetched_at = 0.0

    def skip_known(self, txs: set[str]) -> None:
        """Transactions already on the tape: their receipts are not fetched again. The watcher
        writes nearly every fill within a tick of it landing, so the fifteen-minute pass over
        the same blocks used to spend hundreds of receipt calls confirming what it already had -
        and got rate-limited for it. With the tape's own hashes handed over, the pass pays only
        for the fills the watcher missed."""
        self._known = {t.lower() for t in txs}

    def scan(self, wallets: list[str], first: int, last: int) -> dict[str, list[Trade]]:
        """Every routed fill in one block range, priced and sized, keyed by wallet.

        The unit both the live pass and the backfill are made of. One range costs two `eth_getLogs`
        however many wallets are in it, plus a batched receipt per fill for the dollar value, plus
        one batched `decimals` call for tokens this process has not seen.
        """
        transfers = (self.transfers(wallets, first, last, outgoing=True)
                     + self.transfers(wallets, first, last, outgoing=False))
        fills = routed_fills(transfers, set(wallets), self.routers)

        # Two probes date every log, because blocks land on a fixed interval. A short range next
        # to one already dated - which is every tick of the watcher - borrows that probe's rate
        # instead of spending a request on its own far end.
        last_ts = self.block_timestamp(last)
        near = [b for b in self._probes if b != last and 0 < abs(last - b) <= settings.rpc_window_blocks]
        if near and last - first < 5_000:
            ref = min(near, key=lambda b: abs(last - b))
            per_block = (last_ts - self._probes[ref]) / (last - ref)
            per_block = abs(per_block) or 0.1
            first_ts = last_ts - (last - first) * per_block
        else:
            first_ts = self.block_timestamp(first)
            per_block = (last_ts - first_ts) / max(last - first, 1)

        txs = [tx for tx in fills if tx not in self._known]
        receipts = self.batch("eth_getTransactionReceipt", [[tx] for tx in txs]) if txs else []
        price = self.weth_price()
        # position sizes, not just dollar sizes: how much of a name a wallet still holds is what
        # separates a position it closed from one it is sitting in
        dec = self.decimals(sorted({f["mint"] for f in fills.values()}))

        out: dict[str, list[Trade]] = defaultdict(list)
        priced = 0
        for tx, receipt in zip(txs, receipts):
            f = fills[tx]
            legs = quote_legs(receipt) if receipt else {}
            usd = fill_usd(legs, price)
            priced += usd is not None
            out[f["wallet"]].append(Trade(
                sig=f"{tx}:{f['index']}", address=f["wallet"], chain=CHAIN, mint=f["mint"],
                side=f["side"], sol_amount=legs.get(WETH), usd_value=usd,
                token_amount=f["raw"] / 10 ** dec.get(f["mint"], DEFAULT_DECIMALS),
                ts=int(last_ts - (last - f["block"]) * per_block), source="rpc",
                kind=fill_kind(receipt, self.routers) if receipt else None,
            ))
        log.info("rpc scan: blocks %d..%d (%.1fh), %d transfers, %d fills (%d already on the tape), %d priced, %d requests",
                 first, last, (last - first) * per_block / 3600,
                 len(transfers), len(txs), len(fills) - len(txs), priced, self.requests)
        return dict(out)

    def windows(self, back_to_ts: int, head: int | None = None, span: int | None = None):
        """Block ranges walking backwards from the head, newest first.

        `eth_getLogs` answers "log query timed out" past about 200k blocks, which on a chain with
        0.1s blocks is under six hours. Anything older than that has to be asked for in slices, and
        going newest-first means a backfill that is interrupted has still filled the part that
        matters most.
        """
        head = head if head is not None else self.block_number()
        span = span or settings.rpc_window_blocks
        # one probe pair converts the requested age into a block count
        head_ts = self.block_timestamp(head)
        probe = max(head - span, 0)
        per_block = (head_ts - self.block_timestamp(probe)) / max(head - probe, 1)
        oldest = max(head - int(max(head_ts - back_to_ts, 0) / max(per_block, 1e-9)), 0)

        last = head
        while last > oldest:
            first = max(last - span, oldest)
            yield first, last
            if first <= oldest:
                return
            last = first - 1

    def _load(self) -> None:
        if not self._wallets:
            raise RpcError("RobinhoodRPC.prime() must be called with the wallets to index")
        if time.monotonic() - self._fetched_at < settings.rpc_min_interval_s:
            if self._failed is not None:
                # the scan for this pass already failed: every wallet asked after it gets the
                # same answer without another two requests each
                raise RpcError(f"scan failed earlier this pass: {self._failed}")
            if self._fills:
                return

        head = self.block_number()
        first = max(head - settings.rpc_window_blocks, 0)
        self._fetched_at = time.monotonic()
        for attempt in range(2):
            try:
                self._fills = self.scan(self._wallets, first, head)
                self._failed = None
                return
            except Exception as e:  # noqa: BLE001 - remembered, then re-raised for the caller
                self._failed = str(e)[:160]
                # a backend timeout on the first try is usually a healthy backend on the second;
                # without this one bad answer cost the whole pass its fills
                if attempt == 0 and self.node_failed(e):
                    log.warning("rpc scan failed (%s); once more in 10s", str(e)[:80])
                    time.sleep(10)
                    continue
                raise

    def get_trades(self, address: str, chain: str = CHAIN, since_ts: int | None = None) -> list[Trade]:
        if chain != CHAIN:
            return []
        self._load()
        trades = self._fills.get(address.lower(), [])
        return [t for t in trades if since_ts is None or t.ts >= since_ts]
