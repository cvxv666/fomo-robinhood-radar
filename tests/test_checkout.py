"""Buying a key on the site: the order, the burn, the key, the hook - and no Telegram anywhere.

The burn rail is the bot's own; what is tested here is that a web order rides it without a chat,
that the key it issues reads and receives, and that the secret is the only way to it.
"""
import json

import pytest
from fastapi.testclient import TestClient

from fomo_agent import api, db
from fomo_agent.config import settings
from fomo_agent.pipeline import checkout, keys, pro, webhooks

TOKEN = "0x" + "b" * 40


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "checkout.db")
    monkeypatch.setattr(settings, "pro_price_usd", 20.0)
    monkeypatch.setattr(settings, "pro_days", 30)
    monkeypatch.setattr(settings, "pro_token", TOKEN)
    monkeypatch.setattr(settings, "pro_token_symbol", "FOMO")
    c = db.connect()
    with db.tx(c):
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="FOMO", price_usd=0.0004, price_at=db.now())
    return c


def test_an_order_is_quoted_paid_and_answered_with_a_key(conn, monkeypatch):
    order = checkout.start(conn)
    assert order["usd"] == 20.0 and order["tokens"] > 0 and order["status"] == "open"
    secret = order["secret"]
    assert len(secret) == 64 or len(secret) == 32, "a bearer nobody guesses"
    # before the burn: the quote and nothing else
    before = checkout.status(conn, secret)
    assert before["status"] == "open" and "key" not in before
    assert checkout.status(conn, "f" * 32) is None, "another secret is another order"
    assert checkout.status(conn, "not-hex") is None

    # the burn lands: the matcher sees the amount, and the order is a web one
    paid = pro.record(conn, {"tx": "0xburn", "frm": "0x" + "1" * 40, "raw": _units(order["tokens"]), "block": 1},
                              ts=db.now())
    assert paid["web"] is True and paid["chat_id"].startswith("web:")
    after = checkout.status(conn, secret)
    assert after["status"] == "paid" and after["live"] is True
    assert after["paid_until"] > db.now() + 29 * 86400
    key = after["key"]
    assert len(key) == keys.KEY_LEN * 2

    # the key reads as a PRO chat's does, without any chat existing
    assert conn.execute("SELECT COUNT(*) FROM bot_subscribers").fetchone()[0] == 0, "no phantom subscriber"
    ok, account = keys.Keyring().check(conn, key)
    assert ok and account.startswith("web:")

    # a renewal keeps the same key and adds to its end
    end = after["paid_until"]
    again = checkout.renew(conn, secret)
    assert checkout.renew(conn, "a" * 32) is None, "a renewal is for an order that exists"
    pro.record(conn, {"tx": "0xburn2", "frm": "0x" + "1" * 40, "raw": _units(again["tokens"]), "block": 2}, ts=db.now())
    renewed = checkout.status(conn, secret)
    assert renewed["key"] == key, "a key wired into a running bot is not replaced by a renewal"
    assert renewed["paid_until"] == end + 30 * 86400


def _units(tokens: float) -> int:
    """The amount as the chain carries it: whole tokens in base units."""
    return int(round(tokens)) * 10 ** 18


def test_a_bought_key_receives_the_alerts_and_stops_when_it_lapses(conn, monkeypatch):
    order = checkout.start(conn)
    pro.record(conn, {"tx": "0xburn", "frm": "0x" + "1" * 40, "raw": _units(order["tokens"]), "block": 1}, ts=db.now())
    key = checkout.status(conn, order["secret"])["key"]

    ok, why = webhooks.set_for_key(conn, key, "http://example.com/hook")
    assert not ok and "https" in why
    ok, why = webhooks.set_for_key(conn, key, "https://127.0.0.1/hook")
    assert not ok and "not reachable" in why
    ok, why = webhooks.set_for_key(conn, key, "https://example.com/radar")
    assert ok and [r["key"] for r in webhooks.targets(conn)] == [key]

    # the term runs out: the key stops reading and stops receiving, without anything else changing
    with db.tx(conn):
        conn.execute("UPDATE api_keys SET paid_until = ? WHERE key = ?", (db.now() - 1, key))
    assert webhooks.targets(conn) == []
    assert keys.Keyring().check(conn, key)[0] is False

    ok, _ = webhooks.set_for_key(conn, key, "off")
    assert ok and conn.execute("SELECT webhook_url FROM api_keys WHERE key=?", (key,)).fetchone()[0] is None


def test_the_api_sells_a_key_and_takes_the_url(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(webhooks, "deliver", lambda url, key, body, client=None: sent.append((url, json.loads(body))) or True)
    client = TestClient(api.app)
    api.limiter.hits.clear(); api._responses.clear(); api.keyring._seen.clear()

    order = client.post("/api/checkout", headers={"user-agent": "buyer/1.0"}).json()
    assert order["tokens"] > 0 and order["burn_address"]
    secret = order["secret"]
    assert client.get(f"/api/checkout/{secret}", headers={"user-agent": "buyer/1.0"}).json()["status"] == "open"
    assert client.get("/api/checkout/deadbeef", headers={"user-agent": "buyer/1.0"}).status_code == 404

    pro.record(conn, {"tx": "0xburn", "frm": "0x" + "1" * 40, "raw": _units(order["tokens"]), "block": 1}, ts=db.now())
    key = client.get(f"/api/checkout/{secret}", headers={"user-agent": "buyer/1.0"}).json()["key"]

    h = {"x-api-key": key, "user-agent": "buyer/1.0"}
    assert client.post("/api/webhook", json={"url": "ftp://nope"}, headers=h).status_code == 400
    assert client.post("/api/webhook", json={"url": "https://example.com/radar"}, headers=h).json()["ok"] is True
    me = client.get("/api/me", headers=h).json()
    assert me["webhook_url"] == "https://example.com/radar" and me["paid_until"] > db.now()
    assert me["key"].endswith("…") and len(me["key"]) < 10, "the key is not echoed back in full"

    body = client.post("/api/webhook/test", headers=h).json()
    assert body["ok"] and sent and sent[0][0] == "https://example.com/radar"
    assert sent[0][1]["event"] == "test" and sent[0][1]["symbol"] == "TEST"

    assert client.get("/api/me", headers={"x-api-key": "0" * 48, "user-agent": "x/1"}).status_code == 401


def test_a_reader_without_a_key_is_told_the_door_exists(conn, monkeypatch):
    client = TestClient(api.app)
    api.limiter.hits.clear(); api._responses.clear()
    r = client.get("/api/health", headers={"user-agent": "poller/1.0", "x-forwarded-for": "9.9.9.9"})
    assert "webhooks" in r.headers.get("x-radar-webhooks", "")
    monkeypatch.setattr(api.limiter, "per_minute", 1)
    client.get("/api/health", headers={"user-agent": "poller/1.0", "x-forwarded-for": "8.8.8.8"})
    r = client.get("/api/health", headers={"user-agent": "poller/1.0", "x-forwarded-for": "8.8.8.8"})
    assert r.status_code == 429 and "POSTed to you" in r.json()["more"]
