# FOMO Robinhood Radar

![tests](https://github.com/cvxv666/fomo-robinhood-radar/actions/workflows/tests.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-00ff85)
![cost](https://img.shields.io/badge/runs%20on-%240%20%2F%20month-00ff85)

![FOMO Robinhood Radar](assets/brand/tg-start-1280x640.png)

Finds the [fomo.family](https://fomo.family) traders worth watching **on Robinhood Chain**, resolves
each profile to the wallet that actually trades, reads their fills off the chain every twenty
seconds, checks the receipt of every fill to know whose trade it really was, and has an AI judge
say which of them look repeatable. The result is a live site, a Telegram bot and an HTTP API over
one database: signals, bursts, launches, exits, a token analyzer, a trader analyzer, and a
leaderboard ranked by judgement rather than by headline PnL.

**Live: [fomoradar.app](https://fomoradar.app) · [@fomoradarRH_bot](https://t.me/fomoradarRH_bot) · [API docs](https://fomoradar.app/docs)**

## What it looks like

The home page: the headline, and the brain — a point cloud of the six things the pipeline does,
with the roster's wallets walking between them.

![Home](docs/screens/home.png)

A trader, judged: the verdict and its reasoning, the open book with cost bases and multiples, and
every recent fill — with the ones that were never the wallet's own marked as such.

![Trader](docs/screens/trader.png)

A token: who holds it, what it cost them, the flow in and out, and what they said about it.

![Token](docs/screens/token.png)

| Fresh — launches the cohort is entering, by how early | Exits — the same wallets leaving |
|---|---|
| ![Fresh](docs/screens/fresh.png) | ![Exits](docs/screens/exits.png) |

The leaderboard, ranked by judgement rather than by headline PnL:

![Leaderboard](docs/screens/leaderboard.png)

```bash
pip install -e .
cp .env.example .env          # nothing is required to start; see "Keys" below
fomo-radar init               # create the database, print the request budget
fomo-radar discover --trenches   # import a starter roster, wallets already resolved
fomo-radar track                 # pull their fills off the chain
fomo-radar score --export pending.json   # score in a chat, or set ANTHROPIC_API_KEY
fomo-radar serve                 # the HTTP API
fomo-radar bot                   # the Telegram bot
```

## What it actually knows

Four things took a while to learn and are the reason this repo exists.

**1. A fomo profile address is not a wallet.** Every address fomo's API returns is an internal
account with zero on-chain history. The wallet that executes the trades is separate and the API
never links them. `pipeline/resolve.py` infers it: take ~12 of a trader's swaps, ask who else
traded that token in the same 90-second window, and weight each window by how quiet it was — being
one of three makers is evidence, being one of a hundred is not. Graded against fomo's own verified
wallets once those became available: **101 agreements, zero disagreements.**

**2. The leaderboard's PnL is real money, and it includes open bags.** A wallet showing $2.8M on a
$5.2k on-chain outlay is not a glitch: the position ran and was never sold, so the profit is real
and unrealized at once. Negative on-chain cash flow is what *accumulating* looks like, not what
losing looks like. fomo's figure is the primary scoring signal here; on-chain flow is the sanity
check, and the scoring prompt says so explicitly.

**3. On Robinhood Chain, a relayer submits every trade.** The trader's wallet is never the
transaction's `from`. Each fill routes through one contract, and the wallet's only leg is the token
arriving from it (a buy) or leaving to it (a sell). Filtering on that counterparty is what
separates real fills from the airdrops that make up most of a wallet's log traffic — and it is why
`eth_getLogs` is enough to track the whole roster for free.

**4. A token arriving from the router is not always the wallet buying it.** Anyone can call the
router directly, pay with their own ETH, and name a famous wallet as the recipient; anyone can push
fifty cents of a token through fomo's own flow into eighteen such wallets for thirty-five dollars.
To every tracker that reads "token arrived from a swap" as "wallet bought", both look like the
cohort piling in. `pipeline/provenance.py` reads the receipt: a swap sent to the router itself is
nobody's trade, a buy far below the wallet's own size is dust, and a token pushed into more trusted
wallets than bought it for real is quarantined from every feed. On the first week re-checked,
**one buy in eleven** credited to a trusted wallet was not that wallet's trade.

## Data sources

**Wallet fills** (`TRACK_SOURCES`), first source that supports the wallet's chain wins:

| Source | Chains | Cost |
|---|---|---|
| `rpc` | robinhood | free, keyless. Two `eth_getLogs` calls cover the entire roster |
| `trenches` | robinhood | free, keyless, but only the ~150 wallets [rhtrenches.com](https://rhtrenches.com) curates |
| `codex` | solana, base, robinhood | `CODEX_API_KEY`; ~1 request per wallet per pass |
| `helius` | solana | `HELIUS_API_KEY`; 100 credits per Enhanced Transactions call |

`rpc` is the default and the reason the Codex budget is no longer the binding constraint.
`eth_getLogs` accepts a *list* of values for a topic position, so one request asks for every ERC-20
Transfer whose receiver is any of 300 wallets and a second asks the other direction; receipts for
the fills that come back are batched 40 to a round trip. A full pass over 302 wallets costs about
30 free requests. Against the trenches tape over the same window, 209 of 209 fills agreed on side
and token with a median dollar error of 0.00%.

**New tokens** (`TOKEN_SOURCES`), merged and deduped per chain and address:

| Source | What it sees | Cost |
|---|---|---|
| `codex` | one `filterTokens` across all chains with server-side mcap / liquidity / age filters | `CODEX_API_KEY`; $1 one-time, 10k requests/month |
| `geckoterminal` | trending and top-volume pools per chain (`GECKO_FEEDS`) | free, ~30 rpm |
| `dexscreener` | tokens that bought a profile or boost, and name/price lookups 30 at a time | free, 60 rpm |

**fomo.family itself** comes over HTTP from fomoapi.io — see below. It supplies the leaderboard,
the PnL that drives scoring, each trader's verified wallet, and the notes they write on positions.

### Quote assets are not signals

USDG is Robinhood Chain's dollar stablecoin and every trade passes through it. Sources that book a
swap from the pool's side file "sold token X for USDG" as a *USDG purchase*, which is enough to put
the stablecoin at the top of a signal feed with more buyers than any real token. `sources/rpc.py`
lists the quote assets and the page excludes them everywhere. Worth knowing before trusting any
feed built on raw DEX trade data, this one included.

## Scoring without an API key

Scoring runs either way:

```bash
fomo-radar score                          # needs ANTHROPIC_API_KEY
fomo-radar score --export pending.json    # ...or paste the file into any LLM chat: Claude, GPT, Grok
fomo-radar score --import scored.json     # and load the answer back
```

The export carries the same context and instructions the API path sends, so the two produce
comparable verdicts. `docs/example_scores.json` shows the expected shape.

## The page

`fomo-radar page` writes one standalone HTML file — no server, no build step, no runtime
dependency beyond a webfont — with three views:

- **Signals** — tokens that two or more traders scoring 60+ bought inside the window, ranked by how
  many agree, next to a live tape of every fill by a trusted wallet.
- **Fresh** (`/fresh` on the site, `/api/fresh`, `/fresh` in the bot) — only the names the cohort
  has *just started* buying: a token qualifies when its first trusted buy lands inside the window.
  Ranked by **heat**, which is conviction scaled by how soon after the launch each wallet arrived —
  full weight at the pool opening, half an hour later, a tenth after ten hours. Pools with less
  than the liquidity floor left are counted under the table rather than ranked; on a live window
  the token with the most trusted buyers of all had twenty-nine dollars left in it.
- **Tokens** — everything the cohort still holds, ranked by unrealised profit, with what it cost
  them and the multiple that implies.
- **Traders** — the scored roster: a verdict, the reasoning, the figures behind it, and each
  trader's largest open bags.

`fomo-radar token <address>` and `fomo-radar trader <handle>` answer the same two questions in the
terminal. Both lean on one measure: **conviction**, the sum of each holder's (score/100)². It says
*whose* money is in a name rather than how many wallets are in it, because anyone can open a wallet
and one trader scoring 85 is worth more than ten scoring 40.

## A trader's book comes from the tape

fomo reports three positions per trader — the largest — and nothing at all about exits. That is a
bag list, not a portfolio, so the book is rebuilt from the fills instead: the buys and sells of one
token collapse into money in, money out, and how much of the entry is still held. That last figure
is what separates a position someone closed from one they are sitting in, and it turns three
positions into thirty, with a **realised** side ranked by what each one earned. `pipeline/analyze.py`
holds the only definition; the site, the terminal and the bot all read it from there.

Two honesty rules keep it from inventing the part it cannot see:

- A wallet that sold **more** of a name than the tape ever saw it buy entered before we started
  watching. Out-minus-in there is a windfall conjured from half a record, so those names are
  excluded and counted rather than ranked.
- A **win rate** counts only positions sold out entirely — a trim is a position still running — and
  is withheld below five of them.

What a position is *worth* needs no tape at all: `balanceOf` is a free read, forty to a round trip,
so `pipeline/holdings.py` asks the chain what each wallet actually holds and prices that. It agrees
with fomo's own portfolio to within a rounding error — a position fomo marks at $110,845 comes back
at $110,932 by a completely separate route — and it settles the two things the tape cannot: names
entered before we started watching, and fills an older source recorded without a size.

Prices come from GeckoTerminal, which indexes this chain. DexScreener, measured 2026-09-08, knew 3
of 30 tokens tracked wallets were holding and priced none of them; it stays as the fallback for the
chains it does cover.

The token page's chart is drawn from GeckoTerminal's OHLCV rather than embedded. All three widgets
were tried against a real Robinhood pool on the same day: DexScreener never leaves "Loading
pair...", GeckoTerminal's own iframe renders its toolbar over an empty canvas, and defined.fi
frames its entire app including a sign-in bar. The candles are free and complete, so
`site/src/components/Chart.astro` draws them as server-rendered SVG — no third-party script, and
the chart follows the same visual rules as the page around it.

## Commands

```
fomo-radar init                                  # create db, print the Codex projection
fomo-radar new-tokens                            # store fresh tokens, trigger holder discovery
fomo-radar enrich-tokens                         # resolve names and liquidity for bare addresses
fomo-radar discover --trenches                   # import the trenches roster (free, resolved)
fomo-radar discover --add <wallet> [--chain base]
fomo-radar fomo-api --windows 24h,7d             # fomo leaderboard + verified wallets + notes
fomo-radar discover --mint <mint> --makers       # buyers of a token -> candidates (Codex)
fomo-radar resolve [--handle <name>]             # infer execution wallets
fomo-radar track [--address <wallet>] [--show]
fomo-radar score [--address <wallet>] [--deep] [--force] [--show-context]
fomo-radar token <address> [--hours 48]          # whose money is in this token
fomo-radar trader <handle-or-address>            # one trader in full
fomo-radar report [--hours 24] [--out report.md]
fomo-radar run [--once]                          # polling loop
fomo-radar receive                               # local endpoint for the browser extension (retired)
fomo-radar trenches [--window 7d] [--tape 10]
fomo-radar fomo-import <file>
```

Or `python -m fomo_agent.cli ...` from the repo. (`fomo-agent` still works as an alias; the
Python package keeps its original name so existing imports and scripts do not break.)

## Keys

Everything in `.env`, nothing in code. All of it is optional — the parts that need a missing key
disable themselves and the rest keeps running.

| Variable | Unlocks |
|---|---|
| *(none)* | `rpc` tracking, trenches discovery, dexscreener and geckoterminal |
| `CODEX_API_KEY` | Solana and Base tracking, token discovery, wallet resolution |
| `FOMOAPI_KEY` | the fomo leaderboards, verified wallets and theses over HTTP (free key at fomoapi.io) |
| `ANTHROPIC_API_KEY` | scoring without the export/import loop |
| `TELEGRAM_BOT_TOKEN` | the bot |
| `HELIUS_API_KEY` | Solana tracking |

Every source fails soft: one API being down never stops the loop.

## Getting fomo data

fomo's own API cannot be called from a server. Cloudflare rejects every non-browser client at the
edge with `430 {"error":"unauthorized"}` — including the exact cURL that Chrome generates with a
fresh token — and the Privy bearer expires hourly.

**fomoapi.io** (`FOMOAPI_KEY`) sells the same data over plain HTTP and is what runs today:

```
fomo-radar fomo-api --windows 24h --thesis-pages 0   # the hot board, 1 credit
fomo-radar fomo-api --windows 7d  --thesis-pages 1   # the slow board and 50 notes, 6 credits
```

The leaderboard carries each trader's verified Solana and EVM wallets in the row, so the wallet
this project used to infer from swap timing arrives for free — and arrives for the quiet traders it
could never infer at all. Checked against 101 wallets resolved independently: 101 agreements and no
disagreements. A verified wallet fills a gap but never overwrites one we inferred differently; that
conflict is written down instead.

A free key is 1,000 credits a month. The schedule spends about 30 a day and `collect()` checks the
month's spend before each call, buying the boards first and the notes only if what remains covers
them, so an exhausted month degrades instead of stopping.

What this does not replace is the tape. fomoapi's swap endpoint is a window on the last hundred
fills per trader with no way past it; ours is read straight off chain 4663 and has 109,669 fills in
it. The two are not competing.

**Through a browser** is the older route, still in the tree and no longer running: `extension/` is
a Chrome MV3 extension that collects from a logged-in tab and posts to `fomo-radar receive`, and
`scripts/fomo_export.js` does the same by hand through the DevTools console. Both produce the same
payload and go through the same parsers. It worked for months and cost a Xvfb display, a VNC
server, a packed extension with its own signing key, and a login done by hand through a remote
desktop — and it ended the day fomo restricted the account behind it.

## Layout

```
fomo_agent/
  sources/     one module per external API, each with pure parse_* functions
  pipeline/    discover -> resolve -> track -> score -> analyze
  db.py        sqlite, versioned migrations, no ORM
  config.py    every threshold and interval, read from .env
extension/     Chrome MV3 extension that collected fomo from a logged-in tab (retired)
tests/         offline: every source is exercised through a stored fixture
docs/STATUS.md current state and the work list
docs/robinhood-chain.md what chain 4663 actually looks like, measured
```

## Tests

```bash
pytest -q
```

No network. Every parser has a fixture captured from the real response it must handle; adding a
source means adding one of each.

## Disclaimer

fomo.family has no public API. The fomo side of this reads [fomoapi.io](https://fomoapi.io), a
third party, on a free key. The older route in `extension/` reads what the web app calls
internally from a signed-in browser; that can change or break at any time, may violate their
terms, and accounts used that way get restricted. Robinhood Chain's public RPC is used within its
ordinary rate limits. Nothing here is financial advice, and nothing in this repository places a
trade.

## License

MIT — see [LICENSE](LICENSE).
