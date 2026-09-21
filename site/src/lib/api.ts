// Typed access to the FastAPI service. Every page renders on the server, so these calls go over
// loopback and never reach the visitor's browser.
const BASE = import.meta.env.API_BASE ?? process.env.API_BASE ?? 'http://127.0.0.1:8000';

export type Stats = {
  traders: number; scored: number; active: number; watch: number; dropped: number;
  fills: number; positions: number; open_pnl: number; tokens: number; updated_ts: number | null;
};

export type Signal = {
  mint: string; sym: string; liq: number | null; buyers: number; usd: number | null;
  first_ts: number; avg_score: number; conviction: number; who: string[]; scores: number[];
  /** Set when the buying arrived inside minutes rather than over the day. */
  burst: { ts: number; conviction: number } | null;
};

/** A burst: several trusted wallets entering one token inside the window. `best`, `last` and
 *  `now` are multiples of the price the last entrant paid, from the tape; null when there is
 *  nothing yet to measure with, which is not the same as 1.0. */
export type Burst = {
  mint: string; sym: string; liq: number | null; ts: number; conviction: number; wallets: number;
  usd: number; px: number | null; window_s: number; age_s: number | null;
  who: string[]; scores: number[]; age_at_read_h: number;
  best: number | null; last: number | null; now: number | null; fills: number;
  /** Turned out to be pushed into the wallets rather than bought; kept on the record, out of the score. */
  seeded: boolean;
};

export type HotNow = {
  mint: string; sym: string; liq: number | null; conviction: number; wallets: number; usd: number;
  first_ts: number; last_ts: number; age_s: number | null; window_s: number; px: number | null;
  who: string[]; scores: number[]; avg_score: number;
};

/** One launch the cohort is entering. `heat` is conviction weighted by how early each buyer was. */
/** A token the cohort is leaving. `gone` is how many sellers are out entirely. */
export type Exit = {
  mint: string; sym: string; liq: number | null;
  sellers: number; gone: number; usd: number; conviction: number;
  last_sell: number; who: string[]; scores: number[];
};

export type Fresh = {
  mint: string; sym: string; chain: string | null;
  created_at: number | null; age_h: number | null;
  liq: number | null; mcap: number | null; price: number | null;
  buyers: number; avg_score: number | null; conviction: number; heat: number;
  usd: number | null; first_ts: number; last_ts: number; lead_minutes: number | null;
  who: string[]; scores: (number | null)[];
  entries: { handle: string; score: number | null; ts: number; usd: number | null }[];
};

export type TraderRow = {
  handle: string | null; address: string; score: number; status: string;
  summary: string | null; fomo_pnl: number | null; style: string[]; red_flags: string[];
};

/** One name in a trader's book. `state` says whether it is still running and `src` who marked it. */
export type Position = {
  token: string; sym: string; chain: string | null;
  state: 'open' | 'trimmed' | 'held' | 'closed' | 'unknown';
  src: 'chain' | 'fomo';
  pnl: number | null; cost: number | null; value: number | null; held: number | null;
  /** What the position is worth now — the balance at the token's price, or fomo's own mark. */
  worth: number | null; read_at: number | null;
  price: number | null; exit_pct: number | null; realized: number | null;
  bought_usd: number; sold_usd: number; fills: number; buys: number; sells: number;
  first_ts: number | null; last_ts: number | null; marked_at: number | null;
};

export type Trader = {
  address: string; handle: string | null; chain: string; score: number | null; status: string;
  summary: string | null; model: string | null; style: string[]; red_flags: string[];
  stats: Record<string, number | null>; fomo_pnl: number | null;
  positions: Position[]; closed: Position[]; open_pnl: number | null; book_value: number | null;
  realized_usd: number | null; round_trips: number; wins: number;
  win_rate: number | null; pre_tape: number; tape_from: number | null;
  fills: { ts: number; side: string; usd: number | null; sym: string; mint: string; source: string;
          kind: 'trade' | 'dust' | 'direct' | 'seed' | 'flow' }[];
  bought_usd: number; sold_usd: number; hours: number;
  flow_7d: { bought: number; sold: number; distributing: string | null };
  distributing: string | null;
  pnl_gap: { fomo: number; indexer: number; gap: number } | null;
  company: { handle: string; score: number; shared: number }[];
};

export type Holder = {
  handle: string | null; address: string; score: number | null; status: string;
  pnl: number | null; cost: number | null; amount: number | null; value: number | null;
};

export type Buyer = {
  handle: string | null; address: string; score: number | null;
  bought: number; sold: number; fills: number; first_ts: number;
};

export type Token = {
  mint: string; symbol: string | null; is_quote: boolean; tracked: boolean;
  buyers: Buyer[]; buyer_conviction: number;
  liquidity_usd: number | null; mcap_usd: number | null;
  holders: Holder[]; trusted_holders: number; avg_score: number | null; conviction: number;
  cohort_pnl: number | null; cohort_cost: number | null; cohort_value: number | null;
  flow: { handle: string | null; score: number | null; side: string; usd: number | null; ts: number;
          kind: 'trade' | 'dust' | 'direct' | 'seed' | 'flow' }[];
  /** Trusted wallets that received dust, outside-key or wave buys of this token inside the window.
   *  Past the threshold the token is out of every feed. */
  seeded: { wallets: number; dust: number; direct: number; seed: number; first_ts: number | null; seeded: boolean };
  /** Can it be sold: 1 yes, 0 no, null not known. A 0 keeps it out of every feed. */
  sellable: 0 | 1 | null; sell_note: string | null;
  /** Who made it, from the chain, and what their other tokens of the week came to. */
  creator: { address: string; via: string | null; tokens: number; seeded: number; unsellable: number; dead: number; bad: number; symbols: string[] } | null;
  /** address -> crew number, for the wallets on this page that buy in a flock. */
  crews: Record<string, number>;
  bought_usd: number; sold_usd: number; first_trusted_buy: number | null;
  trusted_buyers: number; hours: number;
  theses: Thesis[];
};

/** A trader's own note on a position. The one thing on a token page that is said, not inferred. */
export type Thesis = {
  text: string; handle: string | null; address: string | null;
  score: number | null; status: string | null;
  pnl_usd: number | null; cost_usd: number | null;
  is_dev: number; first_seen_at: number | null;
};

/** [ts, open, high, low, close, volume] per candle, oldest first. */
export type Chart = {
  mint: string; span: string; pool: string | null; symbol?: string | null;
  candles: number[][]; source?: string; why?: string;
};

/** Returns null on 404 or on a service that is down, so a page can say so instead of crashing. */
/**
 * A short server-side cache in front of the API. The feeds change on the watcher's twenty-second
 * tick and the fifteen-minute pass; a hundred people opening the home page in the same ten
 * seconds are asking the same three questions, and asking once is enough. Token and trader pages
 * share it too, keyed by the full path. Nothing here outlives ten seconds, so a reader is never
 * more than that behind the API. One request in flight per path: the hundredth reader waits on
 * the first rather than adding a hundredth call.
 */
const TTL_MS = 10_000;
const cache = new Map<string, { at: number; body: unknown }>();
const inflight = new Map<string, Promise<unknown>>();

export async function get<T>(path: string): Promise<T | null> {
  const hit = cache.get(path);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.body as T | null;
  let p = inflight.get(path);
  if (!p) {
    p = (async () => {
      try {
        const r = await fetch(`${BASE}${path}`, { headers: { accept: 'application/json' } });
        if (!r.ok) return null;
        return (await r.json()) as unknown;
      } catch {
        return null;
      }
    })().then((body) => {
      if (body !== null) cache.set(path, { at: Date.now(), body });
      inflight.delete(path);
      if (cache.size > 2000) {
        const cutoff = Date.now() - TTL_MS;
        for (const [k, v] of cache) if (v.at < cutoff) cache.delete(k);
      }
      return body;
    });
    inflight.set(path, p);
  }
  return (await p) as T | null;
}

export type RecordRow = {
  id: number; ts: number; kind: 'burst' | 'launch'; mint: string; sym: string; px: number | null;
  wallets: number | null; conviction: number | null; heat: number | null; liq: number | null; chats: number;
  followup_at: number | null; best: number | null; peak_min: number | null; hour: number | null;
  vol_usd: number | null; now: number | null; seeded: boolean; unsellable: boolean;
  verdict: 'honeypot' | 'seeded' | 'open' | 'unmeasured' | 'dead' | '2x' | 'above' | 'below'; paper: number | null;
  trail: number | null; paper_trail: number | null;
};
export type Record = {
  days: number; kind: string; stake: number; followup_min: number;
  totals: { pushes: number; bursts: number; launches: number; measured: number; clean: number; above_entry: number;
            reached_2x: number; median_best: number | null; seeded: number; dead: number; honeypot: number;
            paper: { stake: number; trades: number; staked: number; pnl: number; wins: number; win_rate: number | null;
                     best: number | null; worst: number | null };
            paper_trail: { stake: number; drop: number; trades: number; staked: number; pnl: number; wins: number; win_rate: number | null;
                           best: number | null; worst: number | null } };
  trail_drop: number;
  curve: { ts: number; sym: string; mint: string; kind: string; pnl: number; total: number }[];
  curve_trail: { ts: number; sym: string; mint: string; kind: string; pnl: number; total: number }[];
  pushes: RecordRow[];
};
export const getRecord = (days = 30, kind: 'burst' | 'launch' | null = null) =>
  get<Record>(`/api/record?days=${days}${kind ? `&kind=${kind}` : ''}`);

export const getStats = () => get<Stats>('/api/stats');
export const getSignals = (hours = 24, limit = 40) =>
  get<{ hours: number; count: number; signals: Signal[] }>(`/api/signals?hours=${hours}&limit=${limit}`);
export const getFresh = (hours = 24, maxAgeH = 72, minLiquidity = 5000, minBuyers = 2) =>
  get<{ tokens: Fresh[]; drained: number; hours: number; max_age_h: number;
        min_liquidity: number; min_buyers: number }>(
    `/api/fresh?hours=${hours}&max_age_h=${maxAgeH}&min_liquidity=${minLiquidity}&min_buyers=${minBuyers}`);
export const getHot = (hours = 24) =>
  get<{ delta: number; window_min: number; min_wallets: number; hours: number; now: HotNow[]; recent: Burst[] }>(
    `/api/hot?hours=${hours}`);
export const getExits = (hours = 24, minSellers = 2, minExit = 0.5) =>
  get<{ hours: number; count: number; exits: Exit[] }>(
    `/api/exits?hours=${hours}&min_sellers=${minSellers}&min_exit=${minExit}`);
export const getLeaderboard = (status = 'active', limit = 60) =>
  get<{ status: string; count: number; traders: TraderRow[] }>(`/api/leaderboard?status=${status}&limit=${limit}`);
export const getTrader = (who: string) => get<Trader>(`/api/trader/${encodeURIComponent(who)}`);
export const getToken = (mint: string) => get<Token>(`/api/token/${encodeURIComponent(mint)}`);
export const getChart = (mint: string, span = '7d') =>
  get<Chart>(`/api/token/${encodeURIComponent(mint)}/chart?span=${span}`);
export const search = (q: string) =>
  get<{ kind: string; handle?: string; address?: string; traders?: TraderRow[]; tokens?: { mint: string; symbol: string }[] }>(
    `/api/search?q=${encodeURIComponent(q)}`);

// ---------------------------------------------------------------- formatting

export function usd(v: number | null | undefined, dash = '—'): string {
  if (v === null || v === undefined) return dash;
  const a = Math.abs(v);
  if (a >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (a >= 1_000) return `$${(v / 1_000).toFixed(0)}k`;
  return `$${v.toFixed(0)}`;
}

export function ago(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = Math.max(Math.floor(Date.now() / 1000) - ts, 0);
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

/** What a position is worth against what it cost. Null cost means fomo counts withdrawn profit. */
export function multiple(cost: number | null, pnl: number | null): string {
  if (!cost || cost <= 0 || pnl === null || pnl === undefined) return '—';
  return `${((cost + pnl) / cost).toFixed(1)}×`;
}

/** A token count, which runs from fractions of a coin to billions of a memecoin. */
export function amount(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  if (a >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}B`;
  if (a >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (a >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  if (a >= 1) return v.toFixed(0);
  return v.toPrecision(2);
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`;
}

/** How a position reads at a glance: still whole, part sold, or done. */
export const stateLabel = (s: string, exit: number | null) =>
  s === 'trimmed' ? `trimmed ${pct(exit)}` : s === 'unknown' ? 'unsized' : s;

export const verdictClass = (status: string) =>
  status === 'active' ? 'follow' : status === 'watch' ? 'watch' : 'drop';

export const verdictLabel = (status: string) =>
  status === 'active' ? 'follow' : status === 'dropped' ? 'drop' : status;
