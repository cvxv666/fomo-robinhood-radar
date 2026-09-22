"""Webhooks: the alerts pushed to a PRO key's own URL the moment they go out.

A thousand addresses read the API and six agents make a third of its traffic - copy traders,
paper bots, terminals - and every one of them polls, because polling was the only door. A PRO
chat with a key can now name a URL, and each burst, launch and hour-later read is POSTed to it
as JSON in the same second the chats get it, signed with the key so the receiver can tell it is
ours. Delivery is best effort and off the hot path: a slow endpoint holds a thread, not the
watcher. Twenty failures in a row and the hook is switched off until it is set again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
from urllib.parse import urlparse

import httpx

from .. import db, links
from ..config import settings
from . import pro

log = logging.getLogger(__name__)

MAX_FAILURES = 20
TIMEOUT_S = 6.0


def set_url(conn: sqlite3.Connection, chat_id, url: str, now: int | None = None) -> tuple[bool, str]:
    """Attach a URL to the chat's live key, or detach it with `off`."""
    from . import keys

    now = now or db.now()
    key = keys.current(conn, chat_id)
    if key is None:
        return False, "No API key on this chat yet: /apikey first, then /webhook <url>."
    if url.strip().lower() in ("off", "none", "stop"):
        with db.tx(conn):
            conn.execute("UPDATE api_keys SET webhook_url=NULL, webhook_failures=0 WHERE key=?", (key["key"],))
        return True, "Webhook off."
    bad = why_not_a_hook(url)
    if bad:
        return False, f"{bad[0].upper()}{bad[1:]}: /webhook https://example.com/radar"
    with db.tx(conn):
        conn.execute("UPDATE api_keys SET webhook_url=?, webhook_failures=0, webhook_last=NULL WHERE key=?", (url.strip(), key["key"]))
    return True, (f"Webhook set. Every burst, launch and hour-later read is POSTed there as JSON, signed in "
                  f"<code>X-Radar-Signature</code> (HMAC-SHA256 of the body with your key). /webhook off stops it. "
                  f"Docs: {settings.public_site_url or ''}/docs#webhooks")


def set_for_key(conn: sqlite3.Connection, key: str, url: str) -> tuple[bool, str]:
    """The same setter for a key bought on the site, where there is no chat to speak to."""
    u = (url or "").strip()
    if u.lower() in ("off", "none", "stop", ""):
        with db.tx(conn):
            conn.execute("UPDATE api_keys SET webhook_url=NULL, webhook_failures=0 WHERE key=?", (key,))
        return True, "webhook off"
    bad = why_not_a_hook(u)
    if bad:
        return False, bad
    with db.tx(conn):
        conn.execute("UPDATE api_keys SET webhook_url=?, webhook_failures=0, webhook_last=NULL WHERE key=?", (u, key))
    return True, "webhook set"


def why_not_a_hook(url: str) -> str | None:
    """Why this cannot be a webhook URL, or None. https only, and not at our own door: a hook
    pointed at localhost is the receiver's own machine as the API sees it, which is ours."""
    u = urlparse((url or "").strip())
    host = u.netloc.split(":")[0].lower()
    if u.scheme != "https" or not u.netloc:
        return "a webhook is an https URL on a host of yours"
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or host.startswith(("10.", "192.168.", "169.254.")):
        return "that host is not reachable from here"
    return None


def sample(now: int | None = None) -> dict:
    """What a test delivery carries: the shape of a real burst, marked as a test."""
    now = now or db.now()
    return payload("test", {"mint": "0x" + "0" * 40, "sym": "TEST", "chain": "robinhood", "conviction": 4.2,
                            "wallets": 5, "usd": 12_000.0, "note": "a test delivery from fomoradar.app"}, now)


def deliver_sample(conn: sqlite3.Connection, key: str, now: int | None = None) -> tuple[bool, str]:
    """One signed POST to the key's hook, now, and the answer as the setter sees it."""
    import json as _json

    row = conn.execute("SELECT webhook_url FROM api_keys WHERE key=? AND revoked_at IS NULL", (key,)).fetchone()
    if row is None or not row["webhook_url"]:
        return False, "no webhook on this key"
    body = _json.dumps(sample(now), ensure_ascii=False, default=str).encode()
    ok = deliver(row["webhook_url"], key, body)
    return ok, "delivered" if ok else "your endpoint did not answer 2xx inside six seconds"


def status(conn: sqlite3.Connection, chat_id) -> str:
    from . import keys

    key = keys.current(conn, chat_id)
    if key is None or not key["webhook_url"]:
        return "No webhook on this chat. /webhook <https url> sets one (PRO, needs /apikey)."
    fails = key["webhook_failures"] or 0
    state = "off after 20 failures - set it again to retry" if fails >= MAX_FAILURES else f"{fails} failures in a row" if fails else "delivering"
    return f"Webhook: <code>{key['webhook_url']}</code> - {state}."


def targets(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    now = now or db.now()
    rows = conn.execute(
        "SELECT key, chat_id, webhook_url, webhook_failures, paid_until FROM api_keys "
        "WHERE revoked_at IS NULL AND webhook_url IS NOT NULL AND COALESCE(webhook_failures, 0) < ?", (MAX_FAILURES,)).fetchall()
    # a key bought on the site pays for itself; a chat's key follows the chat's PRO
    return [r for r in rows if (r["paid_until"] or 0) > now or pro.entitled(conn, r["chat_id"], now)]


def payload(event: str, item: dict, now: int) -> dict:
    """What the receiver gets. The item is what the chats were told, plus links and the time."""
    mint = item.get("mint")
    body = {"event": event, "ts": now, "chain": item.get("chain") or "robinhood", "mint": mint, "symbol": item.get("sym"),
            "data": {k: v for k, v in item.items() if k not in ("clones", "entries") and not k.startswith("_")},
            "links": links.token_links(mint) if mint else {}, "source": "FOMO Robinhood Radar"}
    return body


def sign(key: str, body: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def deliver(url: str, key: str, body: bytes, client: httpx.Client | None = None) -> bool:
    headers = {"content-type": "application/json", "user-agent": "FOMO-Robinhood-Radar/1.0 (+webhook)",
               "x-radar-signature": sign(key, body)}
    try:
        c = client or httpx.Client(timeout=TIMEOUT_S)
        r = c.post(url, content=body, headers=headers)
        return 200 <= r.status_code < 300
    except Exception as e:  # noqa: BLE001 - their endpoint, their problem, our count
        log.debug("webhook %s: %s", url, e)
        return False


def _record(db_path, key: str, ok: bool, now: int) -> None:
    """Written on the delivery thread with its own connection: the watcher's is not thread-safe."""
    conn = db.connect(db_path)
    try:
        with db.tx(conn):
            if ok:
                conn.execute("UPDATE api_keys SET webhook_failures=0, webhook_last=? WHERE key=?", (now, key))
            else:
                conn.execute("UPDATE api_keys SET webhook_failures=COALESCE(webhook_failures,0)+1 WHERE key=?", (key,))
    finally:
        conn.close()


def fire(conn: sqlite3.Connection, event: str, item: dict, now: int | None = None, wait: bool = False) -> int:
    """POST the event to every live hook, each on its own thread. Returns how many were sent to."""
    now = now or db.now()
    rows = targets(conn, now)
    if not rows:
        return 0
    body = json.dumps(payload(event, item, now), ensure_ascii=False, default=str).encode()
    db_path = settings.db_path
    threads = []
    for r in rows:
        def go(url=r["webhook_url"], key=r["key"]):
            ok = deliver(url, key, body)
            if not ok:
                ok = deliver(url, key, body)   # once more: a blip is not a failure
            try:
                _record(db_path, key, ok, now)
            except Exception as e:  # noqa: BLE001
                log.debug("webhook bookkeeping: %s", e)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        threads.append(t)
    if wait:
        for t in threads:
            t.join(TIMEOUT_S * 2 + 1)
    return len(rows)
