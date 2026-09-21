"""Tiny sliding-window rate limiter shared by API clients.

Two shapes. `RateLimiter` counts inside one process, which is enough for an endpoint keyed by
our own token. `SharedRateLimiter` keeps the same window in a small SQLite file so every process
on the box draws on one allowance - for an endpoint that counts by IP. GeckoTerminal allows
thirty a minute per IP; the collector, the bot, the watcher and three API workers each counting
to twenty on their own were a hundred and twenty against thirty, seven hundred 429s a day and a
fifteen-second back-off in every pass that hit one.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_per_min: int, name: str = "api"):
        self.max = max_per_min
        self.name = name
        self.calls: deque[float] = deque()
        self.total = 0
        # The API shares one client across its worker threads. Sleeping with the lock held is the
        # point: a second caller arriving during the wait queues behind it instead of also
        # deciding there is room.
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()

    def room(self) -> bool:
        """Whether a call could go now, without waiting for one. A request thread serving the
        public asks this; a batch job calls wait()."""
        with self._lock:
            self._trim(time.monotonic())
            return len(self.calls) < self.max

    def take(self) -> bool:
        """Claim a slot if there is one, without waiting: True and the call is counted, or False
        and nothing is. The impatient caller's one call, in place of room() then wait()."""
        with self._lock:
            now = time.monotonic()
            self._trim(now)
            if len(self.calls) >= self.max:
                return False
            self.calls.append(now)
            self.total += 1
            return True

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(now)
            if len(self.calls) >= self.max:
                sleep = 60 - (now - self.calls[0]) + 0.05
                log.info("%s rate limit: sleeping %.1fs", self.name, sleep)
                time.sleep(max(sleep, 0))
            self.calls.append(time.monotonic())
            self.total += 1


class SharedRateLimiter(RateLimiter):
    """The window in a file, one row per call, so that every process sees every other's calls.

    A claim is one short write transaction; a wait sleeps outside it. If the file cannot be used
    (a read-only disk, a locked path) the limiter falls back to counting on its own, which is
    what it did before, and says so once.
    """

    def __init__(self, max_per_min: int, name: str, path: Path | str, reserve: int = 0):
        super().__init__(max_per_min, name)
        self.path = Path(path)
        # calls a minute kept back from the batch jobs for the request threads: a collector
        # sweeping fifteen thousand prices would otherwise take every slot, and the token page
        # asking for one chart would find the box's allowance spent every time
        self.reserve = reserve
        self._conn: sqlite3.Connection | None = None
        self._broken = False

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("CREATE TABLE IF NOT EXISTS calls(name TEXT NOT NULL, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS calls_name_ts ON calls(name, ts)")
            self._conn = c
        return self._conn

    def _claim(self, limit: int | None = None) -> tuple[bool, float]:
        """Inside one write transaction: (True, now) and the call is counted, or (False, the
        oldest call's time) for the caller to sleep on."""
        c = self._db()
        c.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            c.execute("DELETE FROM calls WHERE name = ? AND ts < ?", (self.name, now - 60))
            n, oldest = c.execute("SELECT COUNT(*), MIN(ts) FROM calls WHERE name = ?", (self.name,)).fetchone()
            if n < (self.max if limit is None else limit):
                c.execute("INSERT INTO calls(name, ts) VALUES(?, ?)", (self.name, now))
                self.total += 1
                return True, now
            return False, oldest if oldest is not None else now
        finally:
            c.execute("COMMIT")

    def _fallback(self, e: Exception) -> None:
        if not self._broken:
            log.warning("%s: shared limit at %s unusable (%s); counting in this process only", self.name, self.path, e)
            self._broken = True

    def room(self) -> bool:
        if not self._broken:
            with self._lock:
                try:
                    c = self._db()
                    n = c.execute("SELECT COUNT(*) FROM calls WHERE name = ? AND ts >= ?",
                                  (self.name, time.time() - 60)).fetchone()[0]
                    return n < self.max
                except sqlite3.Error as e:
                    self._fallback(e)
        return super().room()

    def take(self) -> bool:
        if not self._broken:
            with self._lock:
                try:
                    return self._claim()[0]
                except sqlite3.Error as e:
                    self._fallback(e)
        return super().take()

    def wait(self) -> None:
        if not self._broken:
            with self._lock:
                while True:
                    try:
                        claimed, oldest = self._claim(max(0, self.max - self.reserve))
                    except sqlite3.Error as e:
                        self._fallback(e)
                        break
                    if claimed:
                        return
                    sleep = 60 - (time.time() - oldest) + 0.05
                    log.info("%s rate limit (shared): sleeping %.1fs", self.name, sleep)
                    time.sleep(max(sleep, 0))
        super().wait()


def shared(max_per_min: int, name: str, reserve: int = 0) -> RateLimiter:
    """The box-wide limiter for `name`, at the configured path."""
    from .config import settings

    return SharedRateLimiter(max_per_min, name, settings.ratelimit_path, reserve=reserve)
