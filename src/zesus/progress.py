"""Progress of long operations: done/total, throughput, ETA, elapsed.

A :class:`Progress` is updated by whichever thread does the work, and read from any
thread (the CLI status line, a web job, the map publisher). Throughput is measured over
the last minute, so the ETA follows the current speed, not the average since the start.

:class:`MapPublisher` copies the latest snapshot into the map's ``meta`` table (key
``progress:<pid>``) every few seconds. It uses its own connection and never waits: if
the map is busy, that update is skipped. So a web UI can follow a scan running in
another process without ever slowing it down.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WINDOW_S = 60.0


class Cancelled(Exception):
    """Raised inside an operation when its stop event is set."""


class Progress:
    def __init__(self, name: str = "", *, publish_every: float = 2.0) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._listeners: list[tuple[Callable[[dict[str, Any]], None], float, list[float]]] = []
        self.started = time.time()
        self.state = "running"
        self._final: dict[str, Any] | None = None
        self._reset("", 0, "bytes", "")
        self.publish_every = publish_every

    def _reset(self, stage: str, total: float, unit: str, message: str) -> None:
        self.stage, self.total, self.unit, self.message = stage, float(total or 0), unit, message
        self.done = 0.0
        self.stage_started = time.monotonic()
        self._samples: deque[tuple[float, float]] = deque([(self.stage_started, 0.0)])

    # ------------------------------------------------------------------ updates
    def begin(self, stage: str, total: float = 0, unit: str = "bytes", message: str = "") -> None:
        with self._lock:
            self._reset(stage, total, unit, message)
            self.state, self._final = "running", None
        self._notify(force=True)

    def advance(self, n: float = 1, message: str | None = None) -> None:
        with self._lock:
            self.done += n
            if message is not None:
                self.message = message
            self._sample()
        self._notify()

    def set(self, done: float, total: float | None = None, message: str | None = None) -> None:
        with self._lock:
            self.done = float(done)
            if total is not None:
                self.total = float(total)
            if message is not None:
                self.message = message
            self._sample()
        self._notify()

    def note(self, message: str) -> None:
        with self._lock:
            self.message = message
        self._notify()

    def finish(self, state: str = "done", message: str | None = None) -> None:
        final = self.snapshot()                  # freeze rate/elapsed at the end
        with self._lock:
            self._final = final
            self.state = state
            self.stage = f"{self.name} {state}" if self.name else state
            if message is not None:
                self.message = message
        self._notify(force=True)

    def _sample(self) -> None:
        now = time.monotonic()
        s = self._samples
        if now - s[-1][0] >= 0.5:
            s.append((now, self.done))
            while len(s) > 2 and now - s[1][0] > WINDOW_S:
                s.popleft()

    # ------------------------------------------------------------------ reading
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._final is not None and self.state != "running":
                return {**self._final, "state": self.state, "stage": self.stage, "message": self.message,
                        "eta_s": None, "updated": time.time()}
            now = time.monotonic()
            t0, d0 = self._samples[0]
            dt = now - t0
            rate = (self.done - d0) / dt if dt > 0.5 else 0.0
            elapsed = now - self.stage_started
            if rate <= 0 and elapsed > 0:
                rate = self.done / elapsed
            remaining = max(0.0, self.total - self.done) if self.total else None
            return {
                "name": self.name, "stage": self.stage, "state": self.state, "unit": self.unit,
                "done": self.done, "total": self.total or None,
                "pct": round(100 * self.done / self.total, 2) if self.total else None,
                "rate": rate, "eta_s": (remaining / rate) if (remaining is not None and rate > 0) else None,
                "elapsed_s": elapsed, "total_elapsed_s": time.time() - self.started,
                "message": self.message, "updated": time.time(), "pid": os.getpid(),
            }

    # ------------------------------------------------------------------ listeners
    def listen(self, fn: Callable[[dict[str, Any]], None], every: float) -> None:
        self._listeners.append((fn, every, [0.0]))

    def _notify(self, force: bool = False) -> None:
        if not self._listeners:
            return
        now = time.monotonic()
        snap = None
        for fn, every, last in self._listeners:
            if force or now - last[0] >= every:
                last[0] = now
                snap = snap or self.snapshot()
                try:
                    fn(snap)
                except Exception as exc:          # a listener never breaks the work
                    log.debug("progress listener failed: %s", exc)


def human_rate(snap: dict[str, Any]) -> str:
    r = snap.get("rate") or 0
    return f"{r / 1e6:.0f} MB/s" if snap.get("unit") == "bytes" else f"{r:.0f} {snap.get('unit')}/s"


def human_s(s: float | None) -> str:
    if s is None:
        return "?"
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def log_line(snap: dict[str, Any]) -> str:
    pct = f"{snap['pct']:5.1f}%" if snap.get("pct") is not None else "     "
    return (f"{snap['stage']}: {pct}  {human_rate(snap)}  elapsed {human_s(snap['elapsed_s'])}  "
            f"ETA {human_s(snap.get('eta_s'))}" + (f"  {snap['message']}" if snap.get("message") else ""))


def cli_logger(logger: logging.Logger) -> Callable[[dict[str, Any]], None]:
    def emit(snap: dict[str, Any]) -> None:
        logger.info("%s", log_line(snap))
    return emit


class MapPublisher:
    """Writes snapshots to ``meta['progress:<pid>']`` of a map without ever blocking."""

    def __init__(self, map_path: str | Path) -> None:
        jid = os.environ.get("ZESUS_JOB_ID")
        self.key = f"progress:job:{jid}" if jid else f"progress:{os.getpid()}"
        try:
            self.conn: sqlite3.Connection | None = sqlite3.connect(str(map_path), timeout=0,
                                                                   check_same_thread=False)
        except sqlite3.Error:
            self.conn = None
        self._lock = threading.Lock()

    def __call__(self, snap: dict[str, Any]) -> None:
        if self.conn is None:
            return
        with self._lock:
            try:
                self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                                  "SET value=excluded.value", (self.key, json.dumps(snap)))
                self.conn.commit()
            except sqlite3.Error:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
