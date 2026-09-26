"""Resumable carving pass over a vdev's allocatable area.

The vdev is processed in fixed-size chunks in DVA space. Each chunk's hits are inserted,
and the chunk is marked done, in a single transaction. An interrupted scan therefore
resumes at the first chunk not marked done, with no duplicates and no gaps.

Per chunk:

1. Vectorized pre-filters over every ashift-aligned block:
   * an uncompressed ``objset_phys_t`` (meta-dnode type byte, nblkptr, os_type);
   * a plausible ZFS-LZ4 header (a big-endian compressed length).
2. Each LZ4 candidate is decompressed (as a capacity, since the true lsize is unknown)
   and classified by :mod:`.classify`.
"""

from __future__ import annotations

import logging
import signal
import struct
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import lz4.block
import numpy as np

from ..map.db import MapDB, j
from ..parallel import SETTINGS, ordered_submit, prefetch
from ..zfs.constants import VDEV_LABEL_START_SIZE
from .classify import Classified, PoolLimits, classify, classify_objset

log = logging.getLogger(__name__)

PHASE = "carve"
MAX_META_LSIZE = 128 << 10
TAIL = MAX_META_LSIZE + 8


@dataclass
class CarveTarget:
    pool_id: int
    vdev_top: int
    source: object              # ReadOnlySource covering the vdev
    base_phys: int              # vdev start within the evidence source
    asize: int                  # allocatable bytes (DVA space)
    ashift: int


@dataclass
class ChunkStats:
    candidates: int = 0
    decompressed: int = 0
    hits: int = 0
    by_kind: dict | None = None


class Stop:
    """Cooperative cancellation: Ctrl-C finishes the current chunk, then stops."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def install(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            if self.event.is_set():
                raise KeyboardInterrupt
            log.warning("interrupt received: finishing current chunk, then stopping "
                        "(press Ctrl-C again to abort immediately)")
            self.event.set()
        try:
            signal.signal(signal.SIGINT, handler)
        except ValueError:
            pass  # not in main thread


def prefilter(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized candidate tests over an (nblocks, blocksize) uint8 array.

    Returns (objset_mask, lz4_be_length, lz4_candidate_indices):
    * an uncompressed ``objset_phys_t`` (meta-dnode type byte, nblkptr, os_type);
    * a plausible ZFS-LZ4 header (a big-endian compressed length, then a first token
      whose literal run is non-zero).
    """
    nb, bs = arr.shape
    os_type = np.zeros(nb, dtype=np.uint64)
    if bs >= 1024:
        os_type = arr[:, 704:712].copy().view("<u8").reshape(nb)
    m = ((arr[:, 0] == 10) & (arr[:, 3] >= 1) & (arr[:, 3] <= 3) & (arr[:, 2] >= 1)
         & (arr[:, 2] <= 9) & (arr[:, 1] >= 9) & (arr[:, 1] <= 17)
         & (os_type >= 1) & (os_type <= 3))
    be = lz4_be(arr)
    tok_ok = (arr[:, 4] >> 4) != 0
    cand = np.nonzero((be >= 8) & (be <= MAX_META_LSIZE - 4) & tok_ok & ~m)[0]
    return m, be, cand


def lz4_be(arr: np.ndarray) -> np.ndarray:
    """The big-endian uint32 in bytes 0..3 of each row."""
    return ((arr[:, 0].astype(np.uint32) << 24) | (arr[:, 1].astype(np.uint32) << 16)
            | (arr[:, 2].astype(np.uint32) << 8) | arr[:, 3].astype(np.uint32))


def carve_chunk(raw: bytes, chunk_len: int, dva_base: int, lim: PoolLimits,
                ashift: int) -> tuple[list[tuple[int, str, int, int, Classified]], ChunkStats]:
    """Classify all candidates whose start lies in raw[0:chunk_len].

    Returns (hits, stats). Each hit is (dva_offset, kind, comp, psize_hint, classified).
    """
    bs = 1 << ashift
    nb = chunk_len // bs
    arr = np.frombuffer(raw, dtype=np.uint8, count=nb * bs).reshape(nb, bs)
    st = ChunkStats(by_kind={})
    hits: list[tuple[int, str, int, int, Classified]] = []

    m, be, cand = prefilter(arr)
    for i in np.nonzero(m)[0]:
        off = int(i) * bs
        for size in (4096, 2048, 1024):
            if off + size > len(raw):
                continue
            c = classify_objset(raw[off:off + size], lim)
            if c:
                hits.append((dva_base + off, "objset", 2, size, c))
                break

    st.candidates = len(cand)
    for i in cand:
        off = int(i) * bs
        clen = int(be[i])
        if off + 4 + clen > len(raw):
            continue
        try:
            d = lz4.block.decompress(raw[off + 4:off + 4 + clen], uncompressed_size=MAX_META_LSIZE)
        except lz4.block.LZ4BlockError:
            continue
        st.decompressed += 1
        n = len(d)
        # metadata lsizes are powers of two; ZFS rounds psize up to the sector size
        if n < 512 or n & (n - 1):
            continue
        c = classify(d, lim)
        if c is None:
            continue
        psize = -(-(clen + 4) // 512) * 512
        hits.append((dva_base + off, c.kind, 15, psize, c))

    st.hits = len(hits)
    for h in hits:
        st.by_kind[h[1]] = st.by_kind.get(h[1], 0) + 1
    return hits, st


def run_carve(db: MapDB, target: CarveTarget, lim: PoolLimits, *, chunk_size: int = 64 << 20,
              start: int = 0, end: int | None = None, stop: Stop | None = None,
              progress: Callable[[int, int, ChunkStats, float], None] | None = None) -> bool:
    """Carve DVA range [start, end) of one vdev. Returns True if the range completed."""
    end = target.asize if end is None else min(end, target.asize)
    done = db.done_units(PHASE)
    unit_prefix = f"{target.pool_id}:{target.vdev_top}:"
    total = sum(1 for o in range(start - start % chunk_size, end, chunk_size))
    remaining = [o for o in range(start - start % chunk_size, end, chunk_size)
                 if f"{unit_prefix}{o:x}" not in done]
    log.info("carving vdev %d: %d chunks of %d MiB, %d already done", target.vdev_top, total,
             chunk_size >> 20, total - len(remaining))
    t0 = time.monotonic()
    bytes_done = 0
    bs = 1 << target.ashift

    def read(o: int) -> tuple[int, int, bytes]:
        clen = min(chunk_size, end - o)
        raw = target.source.pread(VDEV_LABEL_START_SIZE + o, clen + TAIL)   # within the vdev slice
        if len(raw) < clen:
            clen = len(raw) - len(raw) % bs
        return o, clen, raw

    # One thread reads the next chunk (sequentially) while chunks are classified, in worker
    # processes when there are several. Chunks are committed strictly in order, so the
    # map is exactly what a serial run writes.
    reads = prefetch((read(o) for o in remaining), depth=SETTINGS.io_depth)
    with carve_executor() as ex:
        results = (ordered_submit(ex, _carve_task, ((raw, clen, o, lim, target.ashift) for o, clen, raw in reads),
                                  SETTINGS.carve_workers + 1)
                   if ex is not None else
                   (_carve_task((raw, clen, o, lim, target.ashift)) for o, clen, raw in reads))
        try:
            for o, clen, hits, st in results:
                if stop and stop.event.is_set():
                    log.warning("carving stopped by request; run the same command again to resume")
                    return False
                with db.tx():
                    for dva_off, kind, comp, psize, c in hits:
                        db.conn.execute(
                            "INSERT INTO carved(pool_id,vdev_top,dva_offset,phys,kind,comp,psize_hint,lsize,"
                            "child_type,child_level,n_children,n_holes,min_birth,max_birth,os_type,info_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (target.pool_id, target.vdev_top, dva_off,
                             target.base_phys + VDEV_LABEL_START_SIZE + dva_off, kind, comp, psize, c.lsize,
                             c.child_type, c.child_level, c.n_children, c.n_holes, c.min_birth, c.max_birth,
                             c.os_type, j(c.info) if c.info else None))
                    db.mark(PHASE, f"{unit_prefix}{o:x}", detail=j({"hits": st.hits, "cand": st.candidates,
                                                                   "kinds": st.by_kind}))
                bytes_done += clen
                if progress:
                    progress(o + clen, end, st, bytes_done / max(1e-9, time.monotonic() - t0))
        finally:
            if hasattr(results, "close"):
                results.close()
    return True


def _carve_task(args) -> tuple[int, int, list, ChunkStats]:
    """Picklable unit of carving work: classify one chunk (runs in a worker process)."""
    raw, clen, o, lim, ashift = args
    hits, st = carve_chunk(raw, clen, o, lim, ashift)
    return o, clen, hits, st


@contextmanager
def carve_executor(initializer=None, initargs=()):
    """A process pool for classifying chunks, or None to classify in this process."""
    n = SETTINGS.carve_workers
    if n <= 1:
        yield None
        return
    ex = ProcessPoolExecutor(max_workers=n, initializer=initializer, initargs=initargs)
    try:
        yield ex
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


def lz4_header(buf: bytes) -> int:
    return struct.unpack_from(">I", buf, 0)[0]
