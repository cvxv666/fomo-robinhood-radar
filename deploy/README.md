# Deploying FOMO Robinhood Radar

What runs in production, why it is arranged this way, and how to change it. Every file in this
directory is a copy of what is actually live — if you edit the server by hand, copy it back here.

## The machine

Contabo Cloud VPS 4, Ubuntu 26.04 LTS, 4 vCPU, 8 GB, 118 GB. Germany, because the bot has to reach
`api.telegram.org` and some jurisdictions block it at the ISP.

The 8 GB is not for what runs today — that fits in about 700 MB. It is headroom for Chrome under
Xvfb, the one piece that would make fomo collection fully unattended.

## What runs

| unit | what it is | port |
|---|---|---|
| `radar-api` | FastAPI over the sqlite database | 127.0.0.1:8000 |
| `radar-site` | Astro, server-rendered | 127.0.0.1:4321 |
| `radar-bot` | Telegram long-polling bot | — |
| `radar-collect.timer` | resolve → track → name tokens, every 15 min | — |
| `radar-backup.timer` | sqlite `.backup` nightly, 14 kept | — |
| `radar-health.timer` | what is quietly broken, pushed to the bot at 07:40 | — |
| `radar-digest.timer` | the day in one message to every subscriber, 18:00 | — |
| `radar-heartbeat.timer` | pings `HEARTBEAT_URL` every 10 min **while the checks pass** | — |
| `radar-watch` | the chain every 20 s: new blocks, their fills, a burst pushed the moment it forms, and PRO burns credited (`pipeline/pro.py`) | — |
| `radar-fomo.timer` | the 24h fomo board over HTTP, every 2 h, 1 credit | — |
| `radar-fomo-slow.timer` | the 7d board and a page of notes, every 8 h, 6 credits | — |
| `radar-xvfb` / `radar-wm` / `radar-browser` / `radar-crx` | the signed-in Chrome that used to collect fomo — **disabled**, see below | — |
| `caddy` | the only thing listening publicly | 80, 443 |

Nothing but Caddy is reachable from outside. `ufw` allows 22, 80 and 443 and nothing else.

## Layout

```
/opt/fomoradar/
  app/            the repository
  app/.env        secrets — written once at provision, never shipped by a deploy
  venv/           python 3.14
  fomo_agent.db   the database
  backups/        db-YYYYMMDD.gz, fourteen nights
```

The service account `radar` owns all of it and has `nologin` as its shell. Each unit runs with
`ProtectSystem=strict` and `ReadWritePaths=/opt/fomoradar`, so a compromised service can write to
exactly one directory.

## Shipping a change

From the repository root:

```bash
deploy/update.sh
```

It packs the working tree, uploads it, reinstalls dependencies, **runs the test suite on the
server**, rebuilds the site and restarts the three services. It never touches the database and it
preserves `.env` across the deploy — that file belongs to the server, not to the repository.

## Logs

Everything goes to the journal, so rotation is journald's job rather than a logrotate file.
`journald/fomoradar.conf` caps it at a gigabyte and thirty days — about a month of this workload,
and longer than anybody looks back. Copy it to `/etc/systemd/journald.conf.d/` and restart
`systemd-journald`.

## Access

Key-only. Password authentication is off in `/etc/ssh/sshd_config.d/99-fomoradar.conf`, and the root
password was rotated to a value nobody holds. If console access is ever needed, reset the password
from the Contabo panel and use their VNC console.

```bash
ssh -i ~/.ssh/fomoradar root@<server>
```

## Adding a domain

Point an A record at the IP, then change one line in `/etc/caddy/Caddyfile`:

```diff
-:80 {
+fomoradar.xyz, www.fomoradar.xyz {
```

`systemctl reload caddy` and the certificate arrives on its own. Then set `API_CORS_ORIGINS` and
`PUBLIC_SITE_URL` in `.env` so canonical links and previews use the hostname instead of the IP.

## Looking at it

```bash
systemctl status radar-api radar-site radar-bot
journalctl -u radar-bot -f
journalctl -u radar-collect -n 40 --no-pager
systemctl list-timers 'radar-*'
```

## The browser that is no longer running

fomo.family sits behind Cloudflare, which refuses every non-browser client, so for months its data
came from a real logged-in Chrome on this box: Xvfb for a display, a window manager, a VNC server
to sign in through, and a policy-installed extension posting collections to `fomo-radar receive`.

On 2026-09-10 fomo restricted the account it ran as. Nothing clever was being done — three
leaderboards and two dozen wallet lookups every thirty minutes — but that is what a scraper looks
like from the other side, and no proxy fixes an account-level block. Collection moved to fomoapi.io
over HTTP the same day.

The stack is disabled rather than removed. It held about 750 MB of Chrome for a page it was no
longer allowed to read, and it comes back with one command if the account is ever unrestricted:

```
systemctl enable --now radar-xvfb radar-wm radar-browser radar-vnc radar-novnc radar-crx
systemctl enable --now radar-fomo-watchdog.timer
```

## Knowing it fell over

Everything else here watches the pipeline from inside the machine, which cannot report the machine
being unreachable. `radar-heartbeat.timer` closes that: it pings `HEARTBEAT_URL` every ten minutes
**only while every check passes**, and posts to `<url>/fail` when one does not. Silence is the
alarm, so a dead host and a broken collector raise the same one — which is right, because from a
reader's side they are the same event.

Set it up once, from outside:

1. Make a check at healthchecks.io (free) or any service that alerts on a missing ping.
2. Period 20 minutes, grace 10 — one missed run is not an alarm, two are.
3. Put its URL in the server's `.env` as `HEARTBEAT_URL=` and `systemctl restart radar-heartbeat.timer`.

Until that variable is set the unit runs, finds nothing to ping, and says so. Nothing else changes.

