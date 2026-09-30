"""Bounded, ordered pipelines, and how much parallelism the evidence can take.

Two rules keep parallel work correct and kind to the disks:

* **Order is preserved.** :func:`ordered_map` returns results in input order, whatever
  order the workers finish in. Checkpoints, map writes and output files are therefore
  exactly what a serial run produces.
* **I/O stays sequential per device.** Reading is its own stage, with at most
  ``io_depth`` requests in flight (default 1 per evidence file). It only reads ahead
  the *next* item of a plan that will be read anyway, never speculatively. Separate
  member disks are read in parallel, because they are separate spindles.

CPU-bound stages (checksums, decompression, parity) may use several threads. The
heavy kernels (numba Fletcher-4, hashlib, lz4, numpy) release the GIL.
"""

from __future__ import annotations

import itertools
import logging
import os
import sys
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")
_END = object()


@dataclass
class Settings:
    workers: int = max(1, min(8, (os.cpu_count() or 2) - 1))   # CPU-bound stages
    io_depth: int = 1                                         # reads in flight per stage
    carve_workers: int = max(1, min(4, (os.cpu_count() or 2) - 1))   # carving processes


SETTINGS = Settings()


def configure(workers: int | None = None, io_depth: int | None = None) -> Settings:
    if workers is not None:
        SETTINGS.workers = max(1, workers)
        SETTINGS.carve_workers = max(1, workers)
    if io_depth is not None:
        SETTINGS.io_depth = max(1, io_depth)
    return SETTINGS


def ordered_map(fn: Callable[[T], R], items: Iterable[T], *, workers: int = 1,
                ahead: int | None = None) -> Iterator[R]:
    """``map(fn, items)`` on *workers* threads, results in input order.

    At most ``ahead`` (default: *workers*) items are in flight. That bounds memory and,
    for a reading stage, the number of outstanding reads. If the consumer stops early,
    pending work is cancelled. Exceptions surface at the item that raised them.
    """
    ahead = max(1, ahead or workers)
    if workers <= 1 and ahead <= 1:
        yield from map(fn, items)
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        yield from ordered_submit(ex, fn, items, ahead)


def ordered_submit(ex: Executor, fn: Callable[[T], R], items: Iterable[T], ahead: int) -> Iterator[R]:
    """Submit ``fn(item)`` to *ex* keeping at most *ahead* in flight; yield in input order.
    Works with thread and process pools (for a process pool, *fn* must be picklable)."""
    it = iter(items)
    q: deque[Future] = deque()
    try:
        for x in itertools.islice(it, max(1, ahead)):
            q.append(ex.submit(fn, x))
        while q:
            r = q.popleft().result()
            nxt = next(it, _END)
            if nxt is not _END:
                q.append(ex.submit(fn, nxt))
            yield r
    finally:
        for f in q:
            f.cancel()


def prefetch(items: Iterable[T], depth: int = 1) -> Iterator[T]:
    """Produce the next *depth* items of *items* on a background thread while the caller
    works on the current one. The source iterator does the I/O."""
    it = iter(items)
    return ordered_map(lambda _: next(it, _END), itertools.repeat(None), workers=1, ahead=depth) \
        if False else _prefetch(it, depth)


def _prefetch(it: Iterator[T], depth: int) -> Iterator[T]:
    import queue
    import threading
    q: queue.Queue = queue.Queue(maxsize=max(1, depth))
    stop = threading.Event()

    def producer() -> None:
        try:
            for x in it:
                while not stop.is_set():
                    try:
                        q.put(("item", x), timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    return
            q.put(("end", None))
        except BaseException as exc:  # surfaced in the consumer
            q.put(("error", exc))

    t = threading.Thread(target=producer, daemon=True, name="zesus-prefetch")
    t.start()
    try:
        while True:
            kind, x = q.get()
            if kind == "end":
                return
            if kind == "error":
                raise x
            yield x
    finally:
        stop.set()


# ---------------------------------------------------------------- media detection

def media_kind(path: str) -> str:
    """Best-effort class of the storage behind *path*: ``ssd``, ``rotational``,
    ``network`` or ``unknown``. Only used to pick defaults and to warn."""
    try:
        if sys.platform == "win32":
            return _win_media_kind(path)
        if sys.platform.startswith("linux"):
            return _linux_media_kind(path)
    except Exception as exc:  # detection is advisory only
        log.debug("media detection failed for %s: %s", path, exc)
    return "unknown"


def _linux_media_kind(path: str) -> str:
    st = os.stat(path)
    dev = os.major(st.st_rdev if path.startswith("/dev/") else st.st_dev), \
        os.minor(st.st_rdev if path.startswith("/dev/") else st.st_dev)
    base = f"/sys/dev/block/{dev[0]}:{dev[1]}"
    for cand in (base + "/queue/rotational", base + "/../queue/rotational"):
        if os.path.exists(cand):
            with open(cand, encoding="utf-8") as f:
                return "rotational" if f.read().strip() == "1" else "ssd"
    return "unknown"


def _win_media_kind(path: str) -> str:
    import ctypes
    from ctypes import wintypes
    full = os.path.abspath(path)
    if full.startswith("\\\\") and not full.startswith("\\\\.\\"):
        return "network"
    drive = os.path.splitdrive(full)[0]
    if not drive:
        return "unknown"
    DRIVE_REMOTE = 4
    if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE:
        return "network"
    # IOCTL_STORAGE_QUERY_PROPERTY / StorageDeviceSeekPenaltyProperty
    GENERIC = 0
    h = ctypes.windll.kernel32.CreateFileW(f"\\\\.\\{drive}", GENERIC, 3, None, 3, 0, None)
    if h in (-1, 0xFFFFFFFF, ctypes.c_void_p(-1).value):
        return "unknown"
    try:
        class Query(ctypes.Structure):
            _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                        ("Extra", ctypes.c_byte * 1)]

        class Seek(ctypes.Structure):
            _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                        ("IncursSeekPenalty", ctypes.c_bool)]
        q, out, n = Query(7, 0), Seek(), wintypes.DWORD()
        ok = ctypes.windll.kernel32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q),
                                                    ctypes.byref(out), ctypes.sizeof(out),
                                                    ctypes.byref(n), None)
        if not ok:
            return "unknown"
        return "rotational" if out.IncursSeekPenalty else "ssd"
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def describe_io(paths: Iterable[str]) -> dict[str, str]:
    kinds = {p: media_kind(p) for p in paths}
    for p, k in kinds.items():
        log.info("evidence %s: %s storage; %d read(s) in flight per file", p, k, SETTINGS.io_depth)
        if k == "ssd" and SETTINGS.io_depth == 1:
            log.info("  (solid-state: --io-depth 2-4 may be faster)")
        if k in ("rotational", "unknown", "network") and SETTINGS.io_depth > 1:
            log.warning("  --io-depth %d on %s storage causes seeking; 1 is usually fastest",
                        SETTINGS.io_depth, k)
    return kinds
