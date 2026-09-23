"""The day in numbers, printed as JSON blocks: what was pushed and what became of it, who read
the site and the bot, and how the software is. Runs on the server inside the app's venv:

    ssh $RADAR_HOST 'cd /opt/fomoradar/app && set -a && . ./.env && set +a && \
        /opt/fomoradar/venv/bin/python /tmp/daily_report.py'

Every block is one line, `KEY {json}`, so a caller can pick what it needs.
"""
import collections, json, re, subprocess, sys, time
sys.path.insert(0, "/opt/fomoradar/app")
from fomo_agent import db  # noqa: E402
from fomo_agent.config import settings  # noqa: E402
from fomo_agent.pipeline import hot  # noqa: E402
from fomo_agent.pipeline.digest import _pool_candles  # noqa: E402
from fomo_agent.pipeline.provenance import seeded  # noqa: E402
from fomo_agent.pipeline.health import report as health_report  # noqa: E402
from fomo_agent.pipeline.collect_api import spent_this_month  # noqa: E402

conn = db.connect(settings.db_path)
import os  # noqa: E402
HOURS = int(os.environ.get("HOURS", "24"))   # the window; the name says a day, a longer look uses the same blocks
now = db.now(); since = now - HOURS * 3600
hhmm = lambda ts: time.strftime("%H:%M", time.gmtime(ts))  # noqa: E731
n1 = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
out = lambda k, v: print(k, json.dumps(v, ensure_ascii=False, default=str))  # noqa: E731

# ── 1. what went to telegram, and what became of it
rows = conn.execute("SELECT chat_id, mint, ts FROM bot_sent WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
# bot_sent holds everything the bot wrote down as sent: launches (the mint), bursts (hot:mint),
# follow alerts (fill:sig), voids (void:mint). Only the first two are pushes; the day the
# follows went live, forty-one fills read as forty-one failed launches named after a tx hash
pushes = {}; follows = collections.Counter(); voids = set()
for r in rows:
    key = r["mint"]
    if key.startswith("fill:"):
        follows[r["chat_id"]] += 1; continue
    if key.startswith("void:"):
        voids.add(key.split(":", 1)[-1]); continue
    kind = "burst" if key.startswith("hot:") else "launch"; mint = key.split(":", 1)[-1]
    p = pushes.setdefault((kind, mint), {"kind": kind, "mint": mint, "ts": r["ts"], "chats": set()})
    p["chats"].add(r["chat_id"]); p["ts"] = min(p["ts"], r["ts"])
# `bot_sent` also holds the rows a new subscriber's backlog-quieting writes, which read as a push
# to one chat (INFRAETH, 22 Sep). The ledger knows how many were told; it wins where it has a row.
ledger = {(r["kind"], r["mint"]): r["chats"] for r in
          conn.execute("SELECT kind, mint, chats FROM pushes WHERE ts >= ?", (since - 3600,))}
_candles_once = _pool_candles(conn)


def candles_for(mint):
    """Once, and once more after a pause if the screener said no: a pool skipped here is a push
    measured by the tape alone, and the tape misses the peak."""
    c = _candles_once(mint)
    if c:
        return c
    time.sleep(20)
    return _candles_once(mint)
report = []
for p in sorted(pushes.values(), key=lambda p: p["ts"]):
    mint, ts = p["mint"], p["ts"]
    tk = conn.execute("SELECT symbol, sellable, sell_note FROM tokens WHERE mint=?", (mint,)).fetchone()
    sym = (tk["symbol"] if tk else None) or mint[:8]
    b = conn.execute("SELECT px, conviction, wallets FROM bursts WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1", (mint, ts - 600, ts + 60)).fetchone()
    px = b["px"] if b and b["px"] else None
    if px is None:
        last = conn.execute("SELECT usd_value / token_amount p FROM trades WHERE mint=? AND ts<=? AND usd_value>0 AND token_amount>0 ORDER BY ts DESC LIMIT 1", (mint, ts)).fetchone()
        px = last["p"] if last else None
    cds = candles_for(mint)
    o = hot.outcome(conn, mint, ts, px, 86400, now, cds)
    best, nowx = o["best"], o["now"]
    vol_since = round(sum(c[5] for c in cds if len(c) > 5 and c[0] >= ts - 300)) if cds else None
    was_seeded = seeded(conn, mint, now)["seeded"]
    # a pool nobody traded after the push is not flat, it is dead: the price did not move because
    # nothing happened, and NCAT read 1.02x for a day on $995 of trades. And a token at a
    # quarter of the call is not open however young, nor flat however high it went first: FN
    # peaked 1.69x and sat at 0.04x, and read "flat" because failed wanted a peak under 1.5
    down = nowx is not None and nowx < 0.5
    verdict = ("honeypot" if tk and tk["sellable"] == 0 else "seeded" if was_seeded else
               "dead" if cds and now - ts >= 3600 and (vol_since or 0) < 2000 else
               "dumped" if down and (best or 0) >= 1.5 else "failed" if down else
               "open" if now - ts < 6 * 3600 and (best or 0) < 2 else
               "reached 2x" if (best or 0) >= 2 else "flat")
    chats = ledger.get((p["kind"], mint), len(p["chats"]))
    report.append({"t": hhmm(ts), "kind": p["kind"], "sym": sym, "mint": mint, "chats": chats, "best": best, "now": nowx,
                   "vol_since": vol_since, "candles": bool(cds), "seeded": was_seeded, "unsellable": bool(tk and tk["sellable"] == 0),
                   "voided": mint in voids,
                   "conv": b["conviction"] if b else None, "wallets": b["wallets"] if b else None, "age_h": round((now - ts) / 3600, 1), "verdict": verdict})
out("PUSHES", report)
out("PUSH_TOTALS", {"pushes": len(report), "launch": sum(r["kind"] == "launch" for r in report), "burst": sum(r["kind"] == "burst" for r in report),
                    "reached_2x": sum(r["verdict"] == "reached 2x" for r in report), "failed": sum(r["verdict"] == "failed" for r in report),
                    "dumped": sum(r["verdict"] == "dumped" for r in report),
                    "flat": sum(r["verdict"] == "flat" for r in report), "open": sum(r["verdict"] == "open" for r in report),
                    "dead": sum(r["verdict"] == "dead" for r in report), "seeded": sum(r["verdict"] == "seeded" for r in report),
                    "honeypot": sum(r["verdict"] == "honeypot" for r in report), "bursts_recorded": n1("SELECT COUNT(*) FROM bursts WHERE ts >= ?", since)})
out("LAUNCHES_UNSENT", [{"t": hhmm(r["ts"]), "sym": r["sym"], "mint": r["mint"], "heat": r["heat"],
                        "wallets": r["wallets"], "liq": r["liq"], "best": r["best"], "now": r["now_x"]}
                       for r in conn.execute(
                           "SELECT p.*, COALESCE(tk.symbol, substr(p.mint,1,8)) sym FROM pushes p "
                           "LEFT JOIN tokens tk ON tk.mint = p.mint WHERE p.ts >= ? AND COALESCE(p.chats,0) = 0 "
                           "ORDER BY p.ts", (since,))])
out("FOLLOWS", {"alerts": sum(follows.values()), "chats": len(follows), "per_chat": dict(follows.most_common(5)),
                "wallets_followed": n1("SELECT COUNT(DISTINCT address) FROM follows"),
                "chats_following": n1("SELECT COUNT(DISTINCT chat_id) FROM follows"),
                "voids_24h": sorted(voids)})
out("UNSELLABLE_24H", [dict(r) for r in conn.execute("SELECT symbol, mint, sell_note FROM tokens WHERE sellable = 0 AND sell_checked_at >= ?", (since,))])
# the seeding waves provenance.waves caught: one-fill-each buys into a queue of trusted wallets
waves = []
for r in conn.execute("SELECT mint, COUNT(DISTINCT address) wallets, MIN(ts) t0, MAX(ts) t1, COUNT(*) fills FROM trades "
                      "WHERE kind = 'seed' AND ts >= ? GROUP BY mint ORDER BY t0", (since,)):
    sizes = sorted(x[0] or 0 for x in conn.execute("SELECT usd_value FROM trades WHERE kind='seed' AND mint=?", (r["mint"],)))
    tk = conn.execute("SELECT symbol FROM tokens WHERE mint=?", (r["mint"],)).fetchone()
    pushed_before = n1("SELECT COUNT(*) FROM bot_sent WHERE mint IN (?, ?)", r["mint"], "hot:" + r["mint"])
    waves.append({"t": hhmm(r["t0"]), "sym": (tk["symbol"] if tk else None) or r["mint"][:8], "mint": r["mint"], "wallets": r["wallets"],
                  "fills": r["fills"], "span_s": r["t1"] - r["t0"], "median_usd": round(sizes[len(sizes) // 2]) if sizes else None,
                  "pushed_to_chats": pushed_before})
out("WAVES_24H", waves)

# ── 2. the bot's people
subs_by_hour = sorted(collections.Counter(time.strftime("%d %H", time.gmtime(r[0])) for r in conn.execute("SELECT subscribed_at FROM bot_subscribers WHERE subscribed_at >= ?", (since,))).items())
out("BOT", {"active": n1("SELECT COUNT(*) FROM bot_subscribers WHERE active=1"), "inactive": n1("SELECT COUNT(*) FROM bot_subscribers WHERE active=0"),
            "new_24h": n1("SELECT COUNT(*) FROM bot_subscribers WHERE subscribed_at >= ?", since), "messages_24h": len(rows),
            "chats_pushed_24h": n1("SELECT COUNT(DISTINCT chat_id) FROM bot_sent WHERE ts >= ?", since), "subs_by_hour": subs_by_hour})

# ── 2b. PRO: who pays, what burned
try:
    from fomo_agent.pipeline import pro as _pro
    out("PRO", {"enabled": _pro.enabled(), "usd": settings.pro_price_usd, "grace_until": settings.pro_grace_until,
                "paid_now": n1("SELECT COUNT(*) FROM bot_subscribers WHERE active=1 AND paid_until > ?", now),
                "expiring_3d": n1("SELECT COUNT(*) FROM bot_subscribers WHERE active=1 AND paid_until BETWEEN ? AND ?", now, now + 3 * 86400),
                "payments_24h": n1("SELECT COUNT(*) FROM pro_payments WHERE chat_id IS NOT NULL AND ts >= ?", since),
                "burned_24h": {"tokens": n1("SELECT COALESCE(SUM(tokens),0) FROM pro_payments WHERE ts >= ?", since),
                               "usd": n1("SELECT COALESCE(SUM(usd),0) FROM pro_payments WHERE ts >= ?", since)},
                "burned_total": {"tokens": n1("SELECT COALESCE(SUM(tokens),0) FROM pro_payments"), "usd": n1("SELECT COALESCE(SUM(usd),0) FROM pro_payments")},
                "quotes_24h": n1("SELECT COUNT(*) FROM pro_quotes WHERE created_at >= ?", since),
                "unclaimed": [dict(r) for r in conn.execute("SELECT tx, frm, tokens, ts FROM pro_payments WHERE chat_id IS NULL ORDER BY ts DESC LIMIT 5")]})
except Exception as e:  # noqa: BLE001
    out("PRO", {"error": str(e)})

# ── 3. the site
# Streamed, not slurped: a day of Caddy is 725k lines and 760 MB of JSON, and holding it as a
# list of dicts took 6.6 GB and an OOM kill on a box with no swap (23 Sep, 06:52). One pass, one
# line at a time, and nothing kept but the counters.
BOT = re.compile(r"bot|crawl|spider|python|httpx|node|curl|go-http|java|okhttp|FomoPilot|Paper|Tracker|copytrader|fishmice|dime-|wget|axios|scrapy|Headless|LinkPreview", re.I)
# the clouds a distributed crawler rents: one hit per address, a browser's User-Agent, and no
# stylesheet ever fetched. 47.79/47.82 walked /token/* all day on 23 Sep and read as 2,310 people
CLOUD = ("47.79.", "47.82.", "47.74.", "47.76.", "8.208.", "8.209.", "34.", "35.", "52.", "54.", "3.")
ips, api_ips, st = set(), set(), collections.Counter()
paths, uas = collections.Counter(), collections.Counter()
pages = 0; byh = collections.defaultdict(lambda: {"n": 0, "0": 0, "429": 0, "502": 0, "d": []})
# per address: page views, whether a stylesheet or script was ever fetched, 429s, the hours seen
seen = collections.defaultdict(lambda: {"pages": 0, "assets": 0, "429": 0, "hours": collections.Counter(), "browser": False})


def caddy_lines(hours: int):
    """One `journalctl` piped, decoded as it arrives; the process is closed either way."""
    proc = subprocess.Popen(["journalctl", "-u", "caddy", "--since", f"{hours} hours ago", "--no-pager", "-o", "cat"],
                            stdout=subprocess.PIPE, text=True, errors="replace", bufsize=1 << 20)
    try:
        for line in proc.stdout:
            if '"status"' not in line:
                continue
            try:
                m = json.loads(line)
            except Exception:  # noqa: BLE001 - a truncated line is a line
                continue
            if "status" in m and "request" in m:
                yield m
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


first_ts = None
for m in caddy_lines(HOURS):
    r = m["request"]; ip = r.get("client_ip", "?"); ua = (r.get("headers", {}).get("User-Agent") or ["?"])[0]; uri = r.get("uri", "").split("?")[0]
    if first_ts is None:
        first_ts = m["ts"]
    ips.add(ip); st[m["status"]] += 1; h = time.strftime("%H", time.gmtime(m["ts"])); b = byh[h]; b["n"] += 1
    if m["status"] == 0: b["0"] += 1
    if m["status"] == 429: b["429"] += 1; seen[ip]["429"] += 1
    if m["status"] == 502: b["502"] += 1
    is_page = m["status"] == 200 and not uri.startswith(("/api", "/_astro", "/favicon", "/robots", "/sitemap")) and not uri.endswith((".css", ".js", ".png", ".svg", ".ico", ".xml", ".txt", ".webp", ".jpg"))
    if uri.startswith("/_astro") or uri.endswith((".css", ".js", ".webp")):
        seen[ip]["assets"] += 1
    if uri.startswith("/api"): api_ips.add(ip)
    if BOT.search(ua): uas[ua[:40]] += 1
    if is_page:
        pages += 1; paths[re.sub(r"^/(token|trader)/.*", r"/\1/*", uri)] += 1; b["d"].append(m["duration"])
        if not BOT.search(ua) and "Mozilla" in ua and not ip.startswith(CLOUD):
            v = seen[ip]; v["pages"] += 1; v["hours"][h] += 1; v["browser"] = True

# A person's browser fetches the page and then its stylesheet; a crawler takes the page and
# leaves. One hit from an address that never asked for an asset is not a visit, and an address
# the limiter turned away a hundred times is a scanner whatever its User-Agent says.
scanners = sorted(ip for ip, v in seen.items() if v["429"] >= 100)
humans = {ip for ip, v in seen.items() if v["browser"] and v["429"] < 100 and (v["assets"] or v["pages"] > 1)}
human_pages = sum(v["pages"] for ip, v in seen.items() if ip in humans)
human_hours = collections.Counter()
for ip in humans:
    human_hours.update(seen[ip]["hours"])
drive_by = sum(1 for ip, v in seen.items() if v["browser"] and ip not in humans)
hours_tbl = []
for h in sorted(byh):
    b = byh[h]; d = sorted(b["d"]); hours_tbl.append({"h": h, "req": b["n"], "st0": b["0"], "429": b["429"], "502": b["502"], "page_p95_ms": round(d[int(len(d) * .95)] * 1000) if d else None})
out("SITE", {"requests": sum(st.values()), "ips": len(ips), "human_ips_on_pages": len(humans), "pages": pages, "human_pages": human_pages, "api_ips": len(api_ips),
             # addresses with a browser's name that took one page and never asked for its stylesheet:
             # a distributed crawler, counted as people until 23 Sep
             "drive_by_ips": drive_by,
             # the oldest line the journal still has: under the window's start, the counts above are short
             "scanners": scanners[:20], "journal_from": time.strftime("%d %H:%M", time.gmtime(first_ts)) if first_ts else None,
             "status": dict(st.most_common(6)), "top_paths": paths.most_common(8), "peak_human_hour": human_hours.most_common(1), "bots": uas.most_common(6), "hours": hours_tbl})

# ── 4. the software and the data
units = subprocess.run(["systemctl", "show", "radar-api", "radar-site", "radar-bot", "radar-watch", "radar-receive", "caddy", "-p", "Id,ActiveState,NRestarts"], capture_output=True, text=True).stdout
out("UNITS", [dict(kv.split("=", 1) for kv in blk.split("\n") if "=" in kv) for blk in units.strip().split("\n\n")])
out("HEALTH", health_report(conn))
out("FOMOAPI", {"spent": spent_this_month(conn), "cap": settings.fomoapi_monthly_credits})
out("DATA", {"trades_24h": n1("SELECT COUNT(*) FROM trades WHERE ts >= ?", since),
             "kinds_24h": dict(conn.execute("SELECT COALESCE(kind,'trade'), COUNT(*) FROM trades WHERE ts >= ? GROUP BY 1", (since,)).fetchall()),
             "tokens_new_24h": n1("SELECT COUNT(*) FROM tokens WHERE first_seen_at >= ?", since),
             "traders": dict(conn.execute("SELECT status, COUNT(*) FROM traders GROUP BY status").fetchall()),
             "unscored": n1("SELECT COUNT(*) FROM traders WHERE score IS NULL AND status IN ('tracking','active','watch')"),
             "resolution": {"users": n1("SELECT COUNT(*) FROM fomo_users"), "resolved": n1("SELECT COUNT(*) FROM fomo_users WHERE onchain_address IS NOT NULL"),
                            "resolved_24h": n1("SELECT COUNT(*) FROM fomo_users WHERE onchain_address IS NOT NULL AND onchain_at >= ?", since),
                            "with_swaps_to_try": n1("SELECT COUNT(*) FROM fomo_users u WHERE onchain_address IS NULL AND EXISTS (SELECT 1 FROM fomo_swaps s WHERE s.user_id=u.user_id)")},
             "runs_24h": dict(conn.execute("SELECT kind, COUNT(*) FROM runs WHERE started_at >= ? GROUP BY kind", (since,)).fetchall()),
             # fills are dated by block timestamp, which runs a few minutes ahead of this clock: a
             # small negative age is the chain's clock, not a fault
             "last_trade_age_min": round((now - n1("SELECT MAX(ts) FROM trades")) / 60, 1),
             "db_mb": round(__import__("os").path.getsize(settings.db_path) / 1e6, 1)})
logs = {}
for u in ("radar-api", "radar-bot", "radar-watch", "radar-collect", "radar-fomo", "radar-fomo-slow"):
    j = subprocess.run(["journalctl", "-u", u, "--since", f"{HOURS} hours ago", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
    warns = [l for l in j.splitlines() if re.search(r"WARNING|ERROR|Traceback", l)]
    kinds = collections.Counter(re.sub(r"\d[\d:,.\- ]*", "#", re.sub(r"0x[0-9a-f]+", "0x…", w.split("WARNING")[-1].split("ERROR")[-1]))[:70] for w in warns)
    logs[u] = {"lines": len(j.splitlines()), "warn_err": len(warns), "top": kinds.most_common(3)}
out("LOGS", logs)
w = subprocess.run(["journalctl", "-u", "radar-watch", "--since", f"{HOURS} hours ago", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
out("WATCH", {"ticks_with_fills": len(re.findall(r"watch: \{", w)), "fills": sum(int(x) for x in re.findall(r"'fills': (\d+)", w)),
              # a tick that finds nothing writes nothing, so the gaps between lines are not gaps in
              # the scan; the heartbeats and the slow-tick lines are what say whether it stalled
              "heartbeats": len(re.findall(r"watch: alive", w)), "slow_ticks": re.findall(r"watch: tick took [^\n]*", w)[-5:],
              "rate_limited": len(re.findall(r"rate limited", w)), "bursts_pushed": sum(int(x) for x in re.findall(r"'sent': (\d+)", w)),
              "not_pushed_unsellable": len(re.findall(r"not pushed", w)),
              "spared_last": (re.findall(r"'spared': (\d+)", w) or ["0"])[-1],
              "rate_limited_by_hour": dict(collections.Counter(m[:13] for m in re.findall(r"(\d{4}-\d\d-\d\d \d\d):\d\d:\d\d,\d+ WARNING [^\n]*rate limited", w)))})
c = subprocess.run(["journalctl", "-u", "radar-collect", "--since", f"{HOURS} hours ago", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
out("COLLECT", {"passes": len(re.findall(r"track: \{", c)), "trades_written": sum(int(x) for x in re.findall(r"'trades': (\d+), 'errors'", c)),
                "errors": sum(int(x) for x in re.findall(r"'errors': (\d+), 'by_source'", c)), "requests": sum(int(x) for x in re.findall(r"'RobinhoodRPC': (\d+)\}, 'kinds'", c)),
                "holdings_failed": len(re.findall(r"holdings failed", c)),
                # a pass the node cut short kept what it had read; the rest stays stale until the next
                "holdings_cut_short": len(re.findall(r"holdings: after \d+ of \d+ pairs", c)),
                "gecko_429": len(re.findall(r"geckoterminal 429", c)),
                "last_scan": (re.findall(r"rpc scan: [^\n]*", c) or ["—"])[-1][:200]})
out("DISK", {"df": subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()[-1],
             "mem": subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.splitlines()[1]})
