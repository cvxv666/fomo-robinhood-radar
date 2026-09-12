import { defineMiddleware } from 'astro:middleware';

// Every page here is rendered on request, and a render is one or more calls into the api. A
// crawler that walks every token page as fast as it can is not a visitor, it is a load test,
// and the api behind the site cannot tell the two apart because everything the site asks for
// arrives from 127.0.0.1. So the site counts its own callers: a token bucket per address,
// refilled at RATE per second up to BURST, and a 429 with a Retry-After once it runs dry.
//
// The numbers are for a person with several tabs open and a bot that behaves; a scraper doing
// one page every half second all day is under the bar too, and welcome. What is not welcome
// is the one that does twenty a second, and this is what turns that into a polite refusal
// instead of everybody else's slow page.
const RATE = 2;      // tokens per second, so 120 pages a minute sustained
const BURST = 60;    // and sixty at once before the meter starts
const IDLE_MS = 5 * 60_000;

type Bucket = { tokens: number; at: number };
const buckets = new Map<string, Bucket>();
let sweptAt = Date.now();

function who(request: Request, fallback: () => string): string {
  // Caddy puts the connecting address first; anything after it was written by the client
  const forwarded = request.headers.get('x-forwarded-for');
  if (forwarded) return forwarded.split(',')[0].trim();
  try { return fallback(); } catch { return '?'; }
}

function take(ip: string, now: number): number {
  // returns 0 when the request may proceed, else the seconds until a token exists
  let b = buckets.get(ip);
  if (!b) { b = { tokens: BURST, at: now }; buckets.set(ip, b); }
  b.tokens = Math.min(BURST, b.tokens + ((now - b.at) / 1000) * RATE);
  b.at = now;
  if (b.tokens >= 1) { b.tokens -= 1; return 0; }
  return Math.ceil((1 - b.tokens) / RATE);
}

function sweep(now: number) {
  // once a minute, forget addresses that have not been seen for a while; the map stays small
  if (now - sweptAt < 60_000) return;
  sweptAt = now;
  for (const [ip, b] of buckets) if (now - b.at > IDLE_MS) buckets.delete(ip);
}

export const onRequest = defineMiddleware((context, next) => {
  const ip = who(context.request, () => context.clientAddress);
  // the box itself - a health check, a screenshot, the deploy's smoke test - is not a visitor
  if (ip === '127.0.0.1' || ip === '::1' || ip === '?') return next();
  const now = Date.now();
  sweep(now);
  const wait = take(ip, now);
  if (wait === 0) return next();
  return new Response(
    'Too many requests. The api at /api is the way to read this in bulk, at 120 a minute.\n',
    { status: 429, headers: { 'Retry-After': String(wait), 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } },
  );
});
