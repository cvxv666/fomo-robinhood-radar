"""All thresholds and settings live here. Nothing is hardcoded elsewhere."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


@dataclass
class Settings:
    # keys
    helius_api_key: str = field(default_factory=lambda: _env("HELIUS_API_KEY"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    codex_api_key: str = field(default_factory=lambda: _env("CODEX_API_KEY"))
    fomo_session: str = field(default_factory=lambda: _env("FOMO_SESSION"))
    # fomoapi.io: the fomo half over plain HTTP, with no browser and no account of ours to lose.
    # Free key is 1,000 credits a month, 3,000 once a card is on file. A pass costs two.
    fomoapi_key: str = field(default_factory=lambda: _env("FOMOAPI_KEY"))
    fomoapi_thesis_pages: int = field(default_factory=lambda: _int("FOMOAPI_THESIS_PAGES", 1))
    fomoapi_monthly_credits: int = field(
        default_factory=lambda: _int("FOMOAPI_MONTHLY_CREDITS", 1000))
    fomo_auth_kind: str = field(default_factory=lambda: _env("FOMO_AUTH_KIND", "cookie"))
    # fomo endpoints are filled ONLY from docs/fomo-endpoints.md (phase 0). Empty = adapter disabled.
    fomo_base_url: str = field(default_factory=lambda: _env("FOMO_BASE_URL", "https://prod-api.fomo.family"))
    fomo_user_agent: str = field(default_factory=lambda: _env(
        "FOMO_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    ))
    # the web app sends this on every call and the API filters by it
    fomo_supported_chains: str = field(default_factory=lambda: _env("FOMO_SUPPORTED_CHAINS", "1399811149,4663,8453,56"))
    # resolve execution wallets for at most N leaderboard entries per run (1 request each)
    fomo_resolve_limit: int = field(default_factory=lambda: _int("FOMO_RESOLVE_LIMIT", 25))
    # on-chain wallet resolution: fomo hides the real wallet, so we infer it from token events.
    # Each window costs one Codex request, so windows x users is the budget line to watch.
    resolve_windows: int = field(default_factory=lambda: _int("RESOLVE_WINDOWS", 12))
    resolve_window_s: int = field(default_factory=lambda: _int("RESOLVE_WINDOW_S", 90))
    resolve_min_hits: int = field(default_factory=lambda: _int("RESOLVE_MIN_HITS", 3))
    resolve_min_ratio: float = field(default_factory=lambda: _float("RESOLVE_MIN_RATIO", 1.4))
    resolve_users_per_pass: int = field(default_factory=lambda: _int("RESOLVE_USERS_PER_PASS", 20))
    # a user that could not be resolved is tried again after this many days, or sooner if new swaps arrive
    resolve_retry_days: int = field(default_factory=lambda: _int("RESOLVE_RETRY_DAYS", 3))
    # where the "who else traded this token just then" answer comes from. `rpc` is free on
    # Robinhood Chain; drop it to force every window through Codex.
    resolve_sources: tuple[str, ...] = field(default_factory=lambda: tuple(
        c.strip() for c in _env("RESOLVE_SOURCES", "rpc,codex").split(",") if c.strip()))

    # rhtrenches.com (was robinhoodtrenches.com): third-party public API over fomo traders on Robinhood Chain
    trenches_base_url: str = field(default_factory=lambda: _env("TRENCHES_BASE_URL", "https://rhtrenches.com"))
    trenches_user_agent: str = field(default_factory=lambda: _env(
        "TRENCHES_USER_AGENT", "fomo-agent/0.1 (research; https://github.com/)"))
    trenches_min_interval_s: float = field(default_factory=lambda: _float("TRENCHES_MIN_INTERVAL_S", 900))
    trenches_include_stocks: bool = field(
        default_factory=lambda: _env("TRENCHES_INCLUDE_STOCKS", "false").lower() in ("1", "true", "yes"))
    trenches_window: str = field(default_factory=lambda: _env("TRENCHES_WINDOW", "7d"))

    # Robinhood Chain's own public JSON-RPC: keyless, and two requests cover the whole roster.
    # Blocks land every ~0.1s, so 200k blocks is ~5.6h and is the widest window it will serve.
    rpc_url: str = field(default_factory=lambda: _env("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    # more endpoints, comma-separated; when the first rate-limits, the next is asked before giving up
    rpc_urls: list[str] = field(default_factory=lambda: [u.strip() for u in _env("RPC_URLS", "").split(",") if u.strip()])
    rpc_user_agent: str = field(default_factory=lambda: _env(
        "RPC_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"))
    rpc_max_per_min: int = field(default_factory=lambda: _int("RPC_MAX_PER_MIN", 45))
    rpc_batch_size: int = field(default_factory=lambda: _int("RPC_BATCH_SIZE", 40))
    # every fomo fill is routed through this contract; a wallet leg without it is not a trade
    rpc_routers: tuple[str, ...] = field(default_factory=lambda: tuple(
        r.strip() for r in _env("RPC_ROUTERS", "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f").split(",") if r.strip()))
    rpc_window_blocks: int = field(default_factory=lambda: _int("RPC_WINDOW_BLOCKS", 200_000))
    rpc_min_interval_s: float = field(default_factory=lambda: _float("RPC_MIN_INTERVAL_S", 600))
    # A backfill walks 200k-block windows backwards and is bounded by requests rather than time,
    # so an unattended run cannot sit on the endpoint all night. 900 covers roughly a fortnight.
    backfill_max_requests: int = field(default_factory=lambda: _int("BACKFILL_MAX_REQUESTS", 900))
    # How wide a historical range to ask for. 200k works on hot recent blocks and times out a week
    # back, so a backfill starts narrower and halves further whenever the endpoint says no.
    backfill_window_blocks: int = field(default_factory=lambda: _int("BACKFILL_WINDOW_BLOCKS", 50_000))
    backfill_min_window_blocks: int = field(default_factory=lambda: _int("BACKFILL_MIN_WINDOW_BLOCKS", 3_000))
    # a 429 during a backfill means slow down, not give up
    backfill_cooldown_s: float = field(default_factory=lambda: _float("BACKFILL_COOLDOWN_S", 20))

    # Telegram bot: long polling, so it needs no public address and no webhook
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_poll_timeout: int = field(default_factory=lambda: _int("TELEGRAM_POLL_TIMEOUT", 50))
    # only needed where api.telegram.org is blocked. http://, socks5:// (needs httpx[socks])
    telegram_proxy: str = field(default_factory=lambda: _env("TELEGRAM_PROXY"))
    # conviction floor for a pushed alert. ~4.0 is four wallets scoring 80 agreeing on one token.
    telegram_min_conviction: float = field(default_factory=lambda: _float("TELEGRAM_MIN_CONVICTION", 4.0))
    # Heat floor for a pushed launch. Measured over a live day: 1.0 is 28 messages, 2.0 is six,
    # 3.0 is three. Six a day is a feed somebody reads; thirty is one they mute.
    telegram_min_heat: float = field(default_factory=lambda: _float("TELEGRAM_MIN_HEAT", 2.0))
    # how many tokens the enrichment pass re-asks "can it be sold" about, every fifteen minutes
    sell_check_per_pass: int = field(default_factory=lambda: _int("SELL_CHECK_PER_PASS", 12))
    # candles since a burst count as evidence only if they carry at least this much volume (and
    # at least half the burst's own size): a trap pool's one trade is not a price
    outcome_min_candle_volume_usd: float = field(default_factory=lambda: _float("OUTCOME_MIN_CANDLE_VOLUME_USD", 200))
    # a tracked address with more fills than this in an hour is not a person: quarantined (pipeline/noise.py)
    noise_fills_per_hour: int = field(default_factory=lambda: _int("NOISE_FILLS_PER_HOUR", 300))
    # a launch is pushed only while it is one: this long after the first trusted wallet went in
    telegram_launch_max_age_min: int = field(default_factory=lambda: _int("TELEGRAM_LAUNCH_MAX_AGE_MIN", 60))
    # ... and only while the cohort is still buying: the last trusted buy no older than this.
    # Replayed over 38 launches (12-16 Sep): pushed more than six minutes after the cohort's last
    # buy, one of six reached 2x (and slowly); inside six minutes, thirteen of thirty-two. Two
    # minutes looked sharper on a smaller sample and would have cost SWARM (8x) and musebook (12x).
    telegram_launch_max_gap_s: int = field(default_factory=lambda: _int("TELEGRAM_LAUNCH_MAX_GAP_S", 360))
    # one message per token: a launch is not sent this soon after a burst on the same token, nor
    # a burst after a launch. Twelve of twenty-six pushes were the same event told twice.
    telegram_dedupe_s: int = field(default_factory=lambda: _int("TELEGRAM_DEDUPE_S", 1800))
    # this many minutes after a push, the same chats hear what the market did with it: the peak
    # and when, now, volume since. A fact, not advice. 0 turns it off.
    telegram_followup_min: int = field(default_factory=lambda: _int("TELEGRAM_FOLLOWUP_MIN", 60))
    # how long the watcher waits after the RPC rate-limits it; one tick is enough to clear it
    watch_rate_limit_wait_s: int = field(default_factory=lambda: _int("WATCH_RATE_LIMIT_WAIT_S", 25))
    # ... and this long after the node itself failed (a backend timeout, a 500): the next attempt
    # usually lands on a healthy backend, so there is nothing to wait out
    watch_node_error_wait_s: int = field(default_factory=lambda: _int("WATCH_NODE_ERROR_WAIT_S", 5))
    # blocks per eth_getLogs when a spare endpoint answers for the node; the free plans cap around a hundred
    rpc_spare_slice_blocks: int = field(default_factory=lambda: _int("RPC_SPARE_SLICE_BLOCKS", 100))
    # PRO access: the alerts and the live feeds, paid for in the token and burned. A price of 0
    # means no gate at all - everything is open, which is how a fresh checkout and the tests run.
    # See pipeline/pro.py.
    pro_price_usd: float = field(default_factory=lambda: _float("PRO_PRICE_USD", 0))
    pro_days: int = field(default_factory=lambda: _int("PRO_DAYS", 30))
    # the token a subscription is paid in; empty falls back to the site's PUBLIC_TOKEN_CA
    pro_token: str = field(default_factory=lambda: (_env("PRO_TOKEN") or _env("PUBLIC_TOKEN_CA")).lower())
    pro_token_symbol: str = field(default_factory=lambda: _env("PUBLIC_TOKEN_SYMBOL", "FOMOBRAIN"))
    pro_burn_address: str = field(default_factory=lambda: _env("PRO_BURN_ADDRESS", "0x000000000000000000000000000000000000dEaD"))
    # a quote is good for this long; the amount in it is the receipt, so it is exact and unique
    pro_quote_ttl_min: int = field(default_factory=lambda: _int("PRO_QUOTE_TTL_MIN", 30))
    # a payment claimed by hash is accepted this far under the dollar price (the price moved)
    pro_price_tolerance: float = field(default_factory=lambda: _float("PRO_PRICE_TOLERANCE", 0.03))
    # the token's price is asked of the market again when the stored one is older than this
    pro_price_max_age_s: int = field(default_factory=lambda: _int("PRO_PRICE_MAX_AGE_S", 600))
    # unix time: everyone subscribed before it keeps PRO for free until it; 0 is no grace
    pro_grace_until: int = field(default_factory=lambda: _int("PRO_GRACE_UNTIL", 0))
    # days of PRO a new chat gets on /start; 0 is none
    pro_trial_days: int = field(default_factory=lambda: _int("PRO_TRIAL_DAYS", 0))
    # X: the radar's own account posts every push a few minutes after Telegram and replies with
    # the hour-later read. Off until the app exists and scripts/x_auth.py has written the token file.
    x_enabled: bool = field(default_factory=lambda: _env("X_ENABLED", "0") == "1")
    x_client_id: str = field(default_factory=lambda: _env("X_CLIENT_ID"))
    x_client_secret: str = field(default_factory=lambda: _env("X_CLIENT_SECRET"))
    x_token_file: str = field(default_factory=lambda: _env("X_TOKEN_FILE", "/opt/fomoradar/x_token.json"))
    # an hour: the alert is the bot's, the record is X's, and by the time a push lands there the
    # hour-later read is already known and goes up under it in the same minute
    x_post_delay_s: int = field(default_factory=lambda: _int("X_POST_DELAY_S", 3600))
    x_max_per_day: int = field(default_factory=lambda: _int("X_MAX_PER_DAY", 40))
    # the bot's @name, for links from the site to it
    telegram_bot_name: str = field(default_factory=lambda: _env("TELEGRAM_BOT_NAME", "fomoradarRH_bot").lstrip("@"))
    telegram_alert_window_h: int = field(default_factory=lambda: _int("TELEGRAM_ALERT_WINDOW_H", 6))
    telegram_alert_interval_s: int = field(default_factory=lambda: _int("TELEGRAM_ALERT_INTERVAL_S", 120))
    # never tell the same chat about the same token twice inside this window
    telegram_realert_hours: int = field(default_factory=lambda: _int("TELEGRAM_REALERT_HOURS", 12))
    # A burst: this much conviction from this many trusted wallets, all entering inside the
    # window. 4.0 in 30 minutes is four wallets scoring 80 agreeing inside half an hour — the
    # same bar the pushed signal uses, with a clock on it. From `cli hot --backtest` over 36
    # days: about five a day, 39% reach 2x inside a day against 36% for the feed as it stands,
    # and the feed as it stands fires fifty times a day. No age cap by default: nine in ten
    # bursts are in a token's first hour, and the few that come in hours two to six did best.
    hot_delta: float = field(default_factory=lambda: _float("HOT_DELTA", 4.0))
    hot_window_min: int = field(default_factory=lambda: _int("HOT_WINDOW_MIN", 30))
    hot_min_wallets: int = field(default_factory=lambda: _int("HOT_MIN_WALLETS", 3))
    hot_max_age_h: int = field(default_factory=lambda: _int("HOT_MAX_AGE_H", 0))
    # Whose trade a fill is (pipeline/provenance.py). A buy under max(the floor, this share of the
    # wallet's median buy) is dust. Two percent, not ten: a trader with a $5,000 median scaling
    # into a name at $150 to $380 is trading, and at ten percent three of those hid a token with
    # $1.6M of liquidity and 43 real buyers. The pushes this exists for are under a dollar.
    dust_abs_usd: float = field(default_factory=lambda: _float("DUST_ABS_USD", 5.0))
    dust_ratio: float = field(default_factory=lambda: _float("DUST_RATIO", 0.02))
    # A token is seeded when this many trusted wallets received dust or direct fills inside the
    # window AND they outnumber the trusted wallets that bought it for real. The second clause is
    # what stops the rule being an attack of its own: without it, five dollars of dust into three
    # famous wallets would hide anybody's token from every feed.
    seed_min_wallets: int = field(default_factory=lambda: _int("SEED_MIN_WALLETS", 3))
    seed_window_h: int = field(default_factory=lambda: _int("SEED_WINDOW_H", 24))
    # A wave (provenance.waves): this many trusted wallets whose only buy on a token arrives in
    # a queue - no gap wider than this between one and the next - none of them bigger than this.
    # The four of 17 Sep were 13 wallets, six seconds apart, $30 to $299; the real bursts of the
    # same week never had five single-fill wallets inside 45 seconds of each other.
    seed_wave_min_wallets: int = field(default_factory=lambda: _int("SEED_WAVE_MIN_WALLETS", 5))
    seed_wave_max_gap_s: int = field(default_factory=lambda: _int("SEED_WAVE_MAX_GAP_S", 45))
    seed_wave_max_usd: float = field(default_factory=lambda: _float("SEED_WAVE_MAX_USD", 500))
    # A burst is a cohort arriving, and a cohort takes time: the first and last entrant in the
    # window at least this far apart. Every real burst of 11-17 Sep spanned six minutes or
    # more; the seeded ones were done in 95 to 122 seconds.
    hot_min_span_s: int = field(default_factory=lambda: _int("HOT_MIN_SPAN_S", 180))
    # Tracked wallets selling at least this share of what the window's entrants bought is a
    # distribution, not an entry (Hound, 17 Sep: pushed at 0.33x with twenty wallets leaving).
    hot_max_sell_ratio: float = field(default_factory=lambda: _float("HOT_MAX_SELL_RATIO", 0.5))
    # A token younger than this (since the cohort's first fill) is pushed only if the pool shows
    # somebody besides the cohort in it: a sell, or more buyers than the entrants we counted.
    hot_young_s: int = field(default_factory=lambda: _int("HOT_YOUNG_S", 1800))
    # A launch whose name was pushed this recently is a clone and is not pushed; a burst on one
    # needs half again the conviction. Two more PAWSINUs went out the hour after the real one.
    telegram_clone_hours: int = field(default_factory=lambda: _int("TELEGRAM_CLONE_HOURS", 24))
    # The watcher: how often it asks the chain for the blocks since last time, on how much of
    # the RPC allowance, and how far it reads on its first tick or after a stall. The scheduled
    # pass owns anything older than that.
    watch_poll_s: int = field(default_factory=lambda: _int("WATCH_POLL_S", 20))
    watch_rpc_max_per_min: int = field(default_factory=lambda: _int("WATCH_RPC_MAX_PER_MIN", 15))
    watch_roster_refresh_s: int = field(default_factory=lambda: _int("WATCH_ROSTER_REFRESH_S", 600))
    watch_start_back_blocks: int = field(default_factory=lambda: _int("WATCH_START_BACK_BLOCKS", 600))
    watch_max_range_blocks: int = field(default_factory=lambda: _int("WATCH_MAX_RANGE_BLOCKS", 6000))

    # public HTTP API the site and any third-party client read from
    api_host: str = field(default_factory=lambda: _env("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _int("API_PORT", 8000))
    api_rate_per_min: int = field(default_factory=lambda: _int("API_RATE_PER_MIN", 120))
    # a PRO chat's API key raises its allowance to this; 0 turns keys off
    api_key_rate_per_min: int = field(default_factory=lambda: _int("API_KEY_RATE_PER_MIN", 600))
    # a request with no User-Agent at all is refused with a 400 that says what to send
    api_require_user_agent: bool = field(default_factory=lambda: _env("API_REQUIRE_USER_AGENT", "1") == "1")
    # uvicorn worker processes. One saturated at 40% CPU under six third-party crawlers with cache
    # hits answering in half a second; the response cache and the limiter are per worker.
    api_workers: int = field(default_factory=lambda: _int("API_WORKERS", 1))
    # how long a GET answer is served from memory; the feeds move slower than this
    api_cache_ttl_s: float = field(default_factory=lambda: _float("API_CACHE_TTL_S", 10))
    # Where the site answers. The bot links tokens to it, and Astro bakes the same value into
    # every canonical and og:url at build time, so the two cannot disagree.
    public_site_url: str = field(default_factory=lambda: _env("PUBLIC_SITE_URL", "").rstrip("/"))
    # Every link to fomo.family carries the radar's referral: the token page a push points at, the
    # profile a trader page points at, the home link in the help. FOMO_REF_CODE is the code, the
    # param is how fomo reads it, FOMO_REF_URL is the plain invite link (derived when empty).
    fomo_ref_code: str = field(default_factory=lambda: _env("FOMO_REF_CODE", "").strip())
    fomo_ref_param: str = field(default_factory=lambda: _env("FOMO_REF_PARAM", "ref").strip())
    fomo_ref_url: str = field(default_factory=lambda: _env("FOMO_REF_URL", "").strip())
    api_cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip() for o in _env("API_CORS_ORIGINS", "").split(",") if o.strip()))

    # local endpoint the browser extension posts fomo collections to (loopback only)
    receiver_host: str = field(default_factory=lambda: _env("RECEIVER_HOST", "127.0.0.1"))
    receiver_port: int = field(default_factory=lambda: _int("RECEIVER_PORT", 8787))
    receiver_token: str = field(default_factory=lambda: _env("RECEIVER_TOKEN"))
    # DevTools port of the collector browser. Loopback only; it is how the server asks that
    # browser a question instead of typing at it and photographing the result.
    browser_debug_port: int = field(default_factory=lambda: _int("BROWSER_DEBUG_PORT", 9222))
    # A fomo session handed over from a browser that is already signed in. Written once, read once
    # by the collector extension, then deleted - it is somebody's login, not a stored credential.
    seed_path: Path = field(default_factory=lambda: Path(
        _env("SEED_PATH") or (Path(_env("DB_PATH", "fomo_agent.db")).parent / "fomo-session.json")))

    # storage
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "fomo_agent.db")))

    # thresholds
    new_token_min_mcap_usd: float = field(default_factory=lambda: _float("NEW_TOKEN_MIN_MCAP_USD", 500_000))
    new_token_max_age_hours: float = field(default_factory=lambda: _float("NEW_TOKEN_MAX_AGE_HOURS", 24))
    # sanity cap: a <24h token "worth" more than this is a spoofed supply/price, not a market
    new_token_max_mcap_usd: float = field(default_factory=lambda: _float("NEW_TOKEN_MAX_MCAP_USD", 1_000_000_000))
    # DexScreener chainIds to watch for fresh tokens. On-chain tracking (Helius) works only for solana.
    dex_chains: tuple[str, ...] = field(
        default_factory=lambda: tuple(c.strip() for c in _env("DEX_CHAINS", "robinhood").split(",") if c.strip())
    )
    # new-token sources: dexscreener (profiles/boosts feeds) and/or geckoterminal (trending/new/top pools, no boost needed)
    token_sources: tuple[str, ...] = field(
        default_factory=lambda: tuple(c.strip() for c in _env("TOKEN_SOURCES", "dexscreener,geckoterminal,codex").split(",") if c.strip())
    )
    gecko_feeds: tuple[str, ...] = field(
        default_factory=lambda: tuple(c.strip() for c in _env("GECKO_FEEDS", "trending_1h,trending_6h,top_volume").split(",") if c.strip())
    )
    gecko_max_req_per_min: int = field(default_factory=lambda: _int("GECKO_MAX_REQ_PER_MIN", 20))
    # codex: 10k requests/month on the $1 plan -> one filterTokens per 300s = ~8.6k/month
    codex_min_interval_s: float = field(default_factory=lambda: _float("CODEX_MIN_INTERVAL_S", 900))
    # server-side potentialScam=false filter. Observed 2026-09-04: drops spoofed-supply tokens but ALSO real
    # high-cap launches (MEME on robinhood), so it is off by default.
    codex_exclude_potential_scam: bool = field(default_factory=lambda: _env("CODEX_EXCLUDE_POTENTIAL_SCAM", "false").lower() in ("1", "true", "yes"))
    gecko_min_interval_s: float = field(default_factory=lambda: _float("GECKO_MIN_INTERVAL_S", 2.0))
    new_token_min_liquidity_usd: float = field(default_factory=lambda: _float("NEW_TOKEN_MIN_LIQUIDITY_USD", 10_000))
    holders_top_n: int = field(default_factory=lambda: _int("HOLDERS_TOP_N", 30))
    # How long a token's price stays usable before the enrichment pass re-quotes it. Open
    # positions are marked with it, so a two-hour-old price is fine and a day-old one is fiction.
    price_max_age_s: int = field(default_factory=lambda: _int("PRICE_MAX_AGE_S", 7200))
    # Tokens re-quoted per pass; DexScreener takes 30 addresses per request, and the collection
    # timer fires four times an hour — enough for the whole book to stay inside the age above.
    price_refresh_limit: int = field(default_factory=lambda: _int("PRICE_REFRESH_LIMIT", 400))
    # A token stays in the re-quote queue this long after the cohort's last buy of it, unless a
    # balance or an open fomo position says somebody still holds it. Every buy ever put 6,300
    # tokens in the queue and the screener answered 429 eight hundred times a day.
    price_refresh_days: int = field(default_factory=lambda: _int("PRICE_REFRESH_DAYS", 7))
    # On-chain balances: how long a read stays usable, and how many (wallet, token) pairs one pass
    # re-reads. Forty go per request, so 2000 pairs is 50 free RPC calls.
    holdings_max_age_s: int = field(default_factory=lambda: _int("HOLDINGS_MAX_AGE_S", 3600))
    # Theses. The collector asks fomo for the holders of tokens the cohort has money in, and the
    # notes come back inside that answer. Ask about a token again after six hours — an opinion
    # written at the entry does not change, but new holders keep arriving — and cap the batch,
    # because the whole point of asking about the right names is not asking about every name.
    thesis_window_h: int = field(default_factory=lambda: _int("THESIS_WINDOW_H", 72))
    thesis_max_age_s: int = field(default_factory=lambda: _int("THESIS_MAX_AGE_S", 21600))
    thesis_batch: int = field(default_factory=lambda: _int("THESIS_BATCH", 6))
    # Pools dated per enrichment pass. A launch time never changes, so this queue only
    # shrinks: 120 a pass at thirty per request clears a backlog of three thousand in a day.
    date_refresh_limit: int = field(default_factory=lambda: _int("DATE_REFRESH_LIMIT", 120))
    # A dead man's switch. The server pings this URL while the pipeline is healthy, and the service
    # at the other end shouts when the pings stop. Silence is the alarm, which is the only design
    # that survives the server itself going down — an internal check cannot report its own host
    # being unreachable. Empty means nobody is watching from outside.
    heartbeat_url: str = field(default_factory=lambda: _env("HEARTBEAT_URL", ""))
    holdings_per_pass: int = field(default_factory=lambda: _int("HOLDINGS_PER_PASS", 2000))
    # codex-based discovery: how many fresh tokens per pass get their buyers pulled (1 request each)
    discover_tokens_per_pass: int = field(default_factory=lambda: _int("DISCOVER_TOKENS_PER_PASS", 3))
    discover_min_buy_usd: float = field(default_factory=lambda: _float("DISCOVER_MIN_BUY_USD", 500))
    leaderboard_limit: int = field(default_factory=lambda: _int("LEADERBOARD_LIMIT", 100))
    leaderboard_periods: tuple[str, ...] = ("24h", "7d", "30d")

    # intervals (seconds)
    discover_interval: int = field(default_factory=lambda: _int("DISCOVER_INTERVAL", 3600))
    new_tokens_interval: int = field(default_factory=lambda: _int("NEW_TOKENS_INTERVAL", 900))
    track_interval: int = field(default_factory=lambda: _int("TRACK_INTERVAL", 10800))
    report_interval: int = field(default_factory=lambda: _int("REPORT_INTERVAL", 86400))

    # rate limits
    fomo_rps: float = field(default_factory=lambda: _float("FOMO_RPS", 1.0))
    helius_max_req_per_min: int = field(default_factory=lambda: _int("HELIUS_MAX_REQ_PER_MIN", 50))
    # wallet-trade sources, tried in order per wallet; first one supporting the chain wins.
    # codex: all chains + USD per trade, costs 1 request per wallet per pass (10k/month budget!)
    # helius: solana only, needs HELIUS_API_KEY (free tier = 1M credits = ~10k enhanced calls/month)
    track_sources: tuple[str, ...] = field(
        default_factory=lambda: tuple(c.strip() for c in _env("TRACK_SOURCES", "rpc,trenches,codex,helius").split(",") if c.strip())
    )
    track_max_wallets_per_pass: int = field(default_factory=lambda: _int("TRACK_MAX_WALLETS_PER_PASS", 25))
    codex_track_page_limit: int = field(default_factory=lambda: _int("CODEX_TRACK_PAGE_LIMIT", 200))
    codex_track_max_pages: int = field(default_factory=lambda: _int("CODEX_TRACK_MAX_PAGES", 3))
    helius_tx_page_limit: int = 100
    track_lookback_days: int = field(default_factory=lambda: _int("TRACK_LOOKBACK_DAYS", 30))

    # scoring
    # a wallet is treated as an automated cycler only when all three hold at once: high frequency,
    # almost no breadth, and a hold time too short for any thesis
    bot_min_trades_7d: int = field(default_factory=lambda: _int("BOT_MIN_TRADES_7D", 200))
    bot_max_tokens: int = field(default_factory=lambda: _int("BOT_MAX_TOKENS", 10))
    bot_max_hold_min: float = field(default_factory=lambda: _float("BOT_MAX_HOLD_MIN", 5))
    bot_score: int = field(default_factory=lambda: _int("BOT_SCORE", 25))
    score_model: str = field(default_factory=lambda: _env("SCORE_MODEL", "claude-haiku-4-5"))
    deep_model: str = field(default_factory=lambda: _env("DEEP_MODEL", "claude-sonnet-5"))
    rescore_after_hours: dict[str, float] = field(
        default_factory=lambda: {"active": 24, "watch": 72, "dropped": 14 * 24, "tracking": 0, "candidate": 0}
    )
    deep_top_n: int = 20
    # "api"    - call the Anthropic API (needs ANTHROPIC_API_KEY)
    # "manual" - export contexts to a file, score them inside a Claude chat, import back
    # "auto"   - api when a key is present, otherwise manual
    codex_monthly_request_cap: int = field(default_factory=lambda: _int("CODEX_MONTHLY_REQUEST_CAP", 10_000))
    scorer_mode: str = field(default_factory=lambda: _env("SCORER", "auto").lower())
    manual_scores_path: str = field(default_factory=lambda: _env("MANUAL_SCORES_PATH", "pending_scores.json"))

    @property
    def scorer(self) -> str:
        """Resolved scoring backend: 'api' or 'manual'."""
        if self.scorer_mode in ("api", "manual"):
            return self.scorer_mode
        return "api" if self.anthropic_api_key else "manual"

    # optional
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))


settings = Settings()
