"""The radar on X: a post per push after a delay, no URL in it, the hour-later reply under it,
a daily cap, and a client that rotates its own token. No network: the HTTP is faked."""
import json

from fomo_agent import bot, db
from fomo_agent.config import settings
from fomo_agent.pipeline import pushes, xpost
from fomo_agent.sources import x as xsrc

MINT = "0x" + "e" * 40


class FakeX:
    def __init__(self):
        self.posts = []

    def post(self, text, media_ids=None, reply_to=None):
        self.posts.append((text, reply_to))
        return str(1000 + len(self.posts))


def burst():
    return {"mint": MINT, "sym": "SWARM", "conviction": 4.1, "wallets": 5, "usd": 10_148.0, "px": 0.00027,
            "first_ts": 1_000_000, "last_ts": 1_000_840, "who": ["a", "b", "c", "d", "e"], "scores": [80] * 5}


def test_a_post_carries_the_address_and_no_link():
    text = xpost.fmt_post("burst", burst())
    assert MINT in text and "http" not in text and "$SWARM" in text and "5 trusted wallets in 14 min" in text and "$10.1K in" in text
    launch = {"mint": MINT, "sym": "HOT", "heat": 2.53, "buyers": 4, "lead_minutes": 6.0, "usd": 21_000.0, "who": ["a"]}
    lt = xpost.fmt_post("launch", launch)
    assert "launch" in lt and "first one 6 min after the pool opened" in lt and "heat 2.53" in lt and "http" not in lt


def test_a_push_is_queued_posted_after_the_delay_and_replied_to_an_hour_on(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "x_enabled", True)
    monkeypatch.setattr(settings, "x_client_id", "app")
    monkeypatch.setattr(settings, "x_post_delay_s", 300)
    monkeypatch.setattr(settings, "x_max_per_day", 20)
    conn = db.connect(tmp_path / "x.db")
    now = db.now()
    pid = pushes.record(conn, "burst", burst(), chats=3, now=now)
    row = conn.execute("SELECT * FROM x_posts").fetchone()
    assert row["push_id"] == pid and row["due_ts"] == now + 300 and row["posted_at"] is None
    fx = FakeX()
    assert xpost.send_due(conn, fx, now=now + 100) == 0, "not yet"
    assert xpost.send_due(conn, fx, now=now + 300) == 1 and MINT in fx.posts[0][0]
    assert conn.execute("SELECT tweet_id FROM x_posts").fetchone()[0] == "1001"
    assert xpost.send_due(conn, fx, now=now + 400) == 0, "posted once"
    ok = xpost.reply_followup(conn, pid, {"best": 1.85, "peak_min": 22, "now": 1.45, "vol": 412_000}, client=fx, now=now + 3600)
    assert ok and fx.posts[1][1] == "1001" and "peak \u00d71.85 at +22 min" in fx.posts[1][0] and "traded $412K" in fx.posts[1][0]
    assert xpost.reply_followup(conn, pid, {"best": 2.0}, client=fx) is False, "replied once"


def test_with_an_hour_of_delay_the_read_goes_up_right_under_the_post(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "x_enabled", True)
    monkeypatch.setattr(settings, "x_client_id", "app")
    monkeypatch.setattr(settings, "x_post_delay_s", 3600)
    conn = db.connect(tmp_path / "late.db")
    now = db.now()
    pid = pushes.record(conn, "burst", burst(), chats=3, now=now)
    fx = FakeX()
    # the hour passes: the followup is measured first (nothing on X yet to reply to) ...
    assert xpost.reply_followup(conn, pid, {"best": 1.5, "peak_min": 30, "now": 1.1, "vol": 50_000}, client=fx, now=now + 3600) is False
    pushes.close(conn, pid, {"best": 1.5, "peak_min": 30, "now": 1.1, "vol": 50_000}, now + 3600)
    # ... and the post, when it goes up, carries the read under it at once
    assert xpost.send_due(conn, fx, now=now + 3600) == 1
    assert len(fx.posts) == 2 and fx.posts[1][1] == "1001" and "peak \u00d71.50 at +30 min" in fx.posts[1][0]
    assert "posted 60 min after the bot's alert" in fx.posts[0][0]


def test_the_daily_cap_holds_and_a_failure_is_retried_three_times(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "x_enabled", True)
    monkeypatch.setattr(settings, "x_client_id", "app")
    monkeypatch.setattr(settings, "x_post_delay_s", 0)
    monkeypatch.setattr(settings, "x_max_per_day", 2)
    monkeypatch.setattr(settings, "telegram_realert_hours", 0)
    conn = db.connect(tmp_path / "cap.db")
    now = db.now()
    for i in range(3):
        b = burst(); b["mint"] = "0x" + f"{i:040x}"
        pushes.record(conn, "burst", b, chats=1, now=now + i)
    fx = FakeX()
    assert xpost.send_due(conn, fx, now=now + 10) == 2, "two a day"

    class Broken:
        def post(self, *a, **k):
            raise RuntimeError("503")
    monkeypatch.setattr(settings, "x_max_per_day", 20)
    for _ in range(4):
        xpost.send_due(conn, Broken(), now=now + 20)
    left = conn.execute("SELECT attempts, error FROM x_posts WHERE posted_at IS NULL").fetchone()
    assert left["attempts"] == 3 and "503" in left["error"], "three tries, then it is let go"


def test_the_client_refreshes_and_rotates_its_token(tmp_path, monkeypatch):
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    class Http:
        def post(self, url, headers=None, data=None, json=None, files=None):
            calls.append((url, data or json))
            if url.endswith("/oauth2/token"):
                return R(200, {"access_token": "fresh", "refresh_token": "next", "expires_in": 7200})
            return R(201, {"data": {"id": "42"}})

    f = tmp_path / "tok.json"
    f.write_text(json.dumps({"access_token": "old", "refresh_token": "first", "expires_at": 0}))
    c = xsrc.XClient(client_id="app", client_secret="", token_file=f, http=Http())
    assert c.post("hello") == "42"
    assert calls[0][0].endswith("/oauth2/token") and calls[0][1]["refresh_token"] == "first"
    assert json.loads(f.read_text())["refresh_token"] == "next", "the rotated token is on disk before it is used"
    v, ch = xsrc.pkce_pair()
    url = xsrc.authorize_url("app", "http://127.0.0.1:8765/callback", "st", ch)
    assert "code_challenge_method=S256" in url and "tweet.write" in url and "offline.access" in url
