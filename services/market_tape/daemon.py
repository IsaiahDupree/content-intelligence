"""Single-leader autonomous market-tape daemon."""

from __future__ import annotations

import fcntl
import signal
import time
from pathlib import Path
from typing import Optional, TextIO

from .collector import MarketTapeCollector
from .config import MarketTapeConfig


class MarketTapeDaemon:
    def __init__(self, config: Optional[MarketTapeConfig] = None):
        self.config = config or MarketTapeConfig.from_environment()
        self.collector = MarketTapeCollector(self.config)
        self.running = True
        self._lock_handle: Optional[TextIO] = None

    def run(self, once: bool = False) -> None:
        self._acquire_lock()
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        while self.running:
            elapsed = self.collector.store.seconds_since_discovery()
            mode = "full" if elapsed is None or elapsed >= self.config.discovery_interval_seconds else "recheck"
            self.collector.run_cycle(mode)
            if once:
                break
            self._sleep_interruptibly(self.config.cycle_seconds)

    def _acquire_lock(self) -> None:
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.config.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another market-tape daemon already owns the lock") from error
        self._lock_handle.seek(0)
        self._lock_handle.truncate()
        self._lock_handle.write(str(__import__("os").getpid()))
        self._lock_handle.flush()

    def _sleep_interruptibly(self, seconds: int) -> None:
        remaining = max(1, seconds)
        while self.running and remaining > 0:
            interval = min(5, remaining)
            time.sleep(interval)
            remaining -= interval

    def _stop(self, *_: object) -> None:
        self.running = False
