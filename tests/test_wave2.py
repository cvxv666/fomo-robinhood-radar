"""Webhooks, invites, what a chat wants to hear, the call-card and the Discord mirror."""
import json

from fomo_agent import db
from fomo_agent.bot import handle_text, subscribe
from fomo_agent.config import settings
from fomo_agent.pipeline import cards, discord, keys, prefs, pro, referrals, webhooks

MINT = "0x" + "a" * 40


def test_a_chat_chooses_kinds_bar_and_quiet_hours(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    subscribe(conn, 7, "u")
    sub = conn.execute("SELECT * FROM bot_subscribers WHERE chat_id='7'").fetchone()
    assert sub["min_conviction"] is None, "no bar until the chat sets one"
    noon = 12 * 3600
    assert prefs.wants(sub, "burst", 2.8, noon) and prefs.wants(sub, "launch", 2.8, noon)
    assert "Bursts only" in handle_text(conn, "/alerts bursts", chat_id=7, username="u")
    assert "under conviction 5" in handle_text(conn, "/minconv 5", chat_id=7, username="u")
    assert "Quiet from 23:00 to 07:00" in handle_text(conn, "/quiet 23-07", chat_id=7, username="u")
    sub = conn.execute("SELECT * FROM bot_subscribers WHERE chat_id='7'").fetchone()
    assert not prefs.wants(sub, "launch", 9, noon), "launches are off"
    assert not prefs.wants(sub, "burst", 4.5, noon), "under the bar"
    assert prefs.wants(sub, "burst", 5.5, noon)
    assert not prefs.wants(sub, "burst", 5.5, 2 * 3600), "quiet hours wrap midnight"
    assert prefs.wants(sub, "burst", 5.5, 8 * 3600)
    subscribe(conn, 7, "u")   # a /start again keeps the bar
    assert conn.execute("SELECT min_conviction FROM bot_subscribers WHERE chat_id='7'").fetchone()[0] == 5.0
    out = handle_text(conn, "/settings", chat_id=7, username="u")
    assert "bursts only" in out and "5 and up" in out and "23:00-07:00" in out
    assert "No quiet hours" in handle_text(conn, "/quiet off", chat_id=7, username="u")
    assert "/alerts all" in handle_text(conn, "/alerts nonsense", chat_id=7, username="u")


def test_an_invite_rewards_both_once_and_only_a_new_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pro_price_usd", 20.0)   # the gate is on
    monkeypatch.setattr(settings, "pro_referral_days", 7)
    monkeypatch.setattr(settings, "telegram_bot_name", "radarbot")
    conn = db.connect(tmp_path / "w.db")
    subscribe(conn, 1, "inviter")
    link = referrals.link(conn, 1)
    assert link.startswith("https://t.me/radarbot?start=r") and len(link.split("start=r")[1]) == 8
    code = link.split("start=r")[1]
    now = db.now()
    out = handle_text(conn, f"/start r{code}", chat_id=2, username="new")
    assert "7 days of PRO" in out
    assert pro.paid_until(conn, 2) >= now + 7 * 86400 - 5 and pro.paid_until(conn, 1) >= now + 7 * 86400 - 5
    assert referrals.stats(conn, 1) == {"joined": 1, "rewarded": 1}
    # the same chat again, or a chat the bot already knew: nothing more
    assert "7 days of PRO" not in handle_text(conn, f"/start r{code}", chat_id=2, username="new")
    subscribe(conn, 3, "old")
    assert referrals.join(conn, 3, code, was_new=False) is None
    assert referrals.join(conn, 1, code, was_new=True) is None, "not oneself"
    assert referrals.join(conn, 4, "zzzzzzzz", was_new=True) is None, "nobody's code"
    assert "Joined by your link so far: <b>1</b>" in handle_text(conn, "/invite", chat_id=1, username="inviter")


class FakeHttp:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def post(self, url, content=None, headers=None, **kw):
        self.calls.append((url, content, headers))

        class R:
            status_code = self.status
        return R()


def test_a_webhook_is_signed_and_switched_off_after_failures(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "w.db")
    subscribe(conn, 5, "u")
    assert "apikey first" in webhooks.set_url(conn, 5, "https://x.example/hook")[1]
    key = keys.issue(conn, 5)
    assert not webhooks.set_url(conn, 5, "http://plain.example/hook")[0], "https only"
    assert not webhooks.set_url(conn, 5, "https://localhost/hook")[0]
    ok, msg = webhooks.set_url(conn, 5, "https://x.example/hook")
    assert ok and "X-Radar-Signature" in msg
    assert [r["chat_id"] for r in webhooks.targets(conn)] == ["5"]
    body = json.dumps(webhooks.payload("burst", {"mint": MINT, "sym": "T", "conviction": 4.5}, 1)).encode()
    fake = FakeHttp()
    assert webhooks.deliver("https://x.example/hook", key, body, client=fake)
    url, content, headers = fake.calls[0]
    assert headers["x-radar-signature"] == webhooks.sign(key, content) and json.loads(content)["links"]["fomo"].endswith(MINT)
    monkeypatch.setattr(settings, "db_path", tmp_path / "w.db")
    monkeypatch.setattr(webhooks, "deliver", lambda url, key, body, client=None: False)
    for _ in range(webhooks.MAX_FAILURES):
        webhooks.fire(conn, "burst", {"mint": MINT, "sym": "T"}, wait=True)
    assert webhooks.targets(conn) == [], "twenty failures in a row and it is off"
    assert "off after 20 failures" in webhooks.status(conn, 5)
    assert webhooks.set_url(conn, 5, "https://x.example/hook")[0] and len(webhooks.targets(conn)) == 1, "set again to retry"
    assert "Webhook off" in webhooks.set_url(conn, 5, "off")[1]


def test_the_card_and_the_discord_text(monkeypatch):
    monkeypatch.setattr(settings, "card_min_x", 2.0)
    monkeypatch.setattr(settings, "x_handle", "FomoBrainRH")
    row = {"sym": "CPU", "kind": "burst", "mint": MINT, "ts": 1789668797, "wallets": 8, "conviction": 4.45, "heat": None}
    m = {"best": 2.43, "peak_min": 34, "now": 1.45, "vol": 1_294_260}
    assert cards.wanted(m) and not cards.wanted({"best": 1.9})
    d = cards.data(row, m, now=1789672400)
    assert d["called_at"] == "18:13" and d["date"] == "17 SEP 2026"
    assert "window.CARD = {" in cards.html(d)
    t = cards.post_text(d)
    assert t.startswith("Called $CPU at ×1.00 · ×2.43 34 min later · now ×1.45") and "8 trusted wallets" in t and len(t) <= 280
    md = discord.markdown('▲ <a href="https://s/t/1"><b>$CPU</b></a> · burst\n<pre>a   1\nb   2</pre>\n<code>0xabc</code> · <i>seeded</i> &lt;x&gt;')
    assert md == "▲ [**$CPU**](<https://s/t/1>) · burst\n```\na   1\nb   2\n```\n`0xabc` · *seeded* <x>"
    assert discord.send("<b>x</b>") is False, "off without a URL"
