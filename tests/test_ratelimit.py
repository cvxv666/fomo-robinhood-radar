"""One allowance per box: the limiter in a file, shared by every process that names it."""
import sqlite3
import threading
import time

from fomo_agent.ratelimit import RateLimiter, SharedRateLimiter


def test_take_claims_without_waiting_and_counts():
    rl = RateLimiter(2, "t")
    assert rl.take() and rl.take() and not rl.take()
    assert rl.total == 2 and not rl.room()


def test_two_limiters_on_one_file_share_the_window(tmp_path):
    path = tmp_path / "rl.db"
    a, b = SharedRateLimiter(3, "gecko", path), SharedRateLimiter(3, "gecko", path)
    assert a.take() and b.take() and a.take()
    assert not b.take() and not a.take(), "three between them is the three"
    assert not a.room() and not b.room()
    other = SharedRateLimiter(3, "codex", path)
    assert other.take(), "a different name is a different window"
    n = sqlite3.connect(path).execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    assert n == 4


def test_wait_sleeps_until_the_oldest_call_is_a_minute_old(tmp_path, monkeypatch):
    path = tmp_path / "rl.db"
    rl = SharedRateLimiter(1, "gecko", path)
    rl.wait()
    # the one call is made to look fifty-nine and a half seconds old, so the wait is short
    sqlite3.connect(path).execute("UPDATE calls SET ts = ts - 59.5").connection.commit()
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s) or sqlite3.connect(path).execute("UPDATE calls SET ts = ts - 1").connection.commit())
    rl.wait()
    assert len(slept) == 1 and 0 < slept[0] < 1
    assert rl.total == 2


def test_an_unusable_file_falls_back_to_counting_alone(tmp_path):
    rl = SharedRateLimiter(2, "gecko", tmp_path / "no" / "such" / "dir" / "rl.db")
    assert rl.take() and rl.take() and not rl.take()
    assert rl._broken and rl.total == 2


def test_threads_never_exceed_the_window(tmp_path):
    rl = SharedRateLimiter(5, "gecko", tmp_path / "rl.db")
    got = []

    def go():
        got.append(rl.take())
    ts = [threading.Thread(target=go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(got) == 5


def test_the_reserve_is_kept_for_the_callers_that_do_not_wait(tmp_path, monkeypatch):
    rl = SharedRateLimiter(4, "gecko", tmp_path / "rl.db", reserve=2)
    rl.wait(); rl.wait()
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s) or sqlite3.connect(rl.path).execute("DELETE FROM calls").connection.commit())
    rl.wait()
    assert len(slept) == 1, "the batch job stops at two of four and waits"
    rl2 = SharedRateLimiter(4, "gecko", tmp_path / "rl.db", reserve=2)
    rl2.wait()
    assert rl2.take() and rl2.take() and not rl2.take(), "the request thread has the other two"


def test_batch_calls_keep_the_gap_box_wide_and_the_impatient_ones_do_not(tmp_path, monkeypatch):
    path = tmp_path / "rl.db"
    a, b = SharedRateLimiter(30, "gecko", path, min_gap_s=2.0), SharedRateLimiter(30, "gecko", path, min_gap_s=2.0)
    a.wait()
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s) or sqlite3.connect(path).execute("UPDATE calls SET ts = ts - 2").connection.commit())
    b.wait()
    assert len(slept) == 1 and 0 < slept[0] <= 2.0, "the other process waited out the gap"
    assert b.take(), "a request thread is counted, not spaced"
