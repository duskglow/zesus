"""Carving a RAIDZ vdev.

RAIDZ DVA space is not a linear image. A block's bytes are split into *columns*, each a
contiguous run on one child disk, laid out row by row across the children (see
:mod:`zesus.zfs.raidz`). So the carver works on member disks, not on DVA space:

* A chunk is one range of rows, read from every present child in parallel. Each disk
  reads sequentially.
* A metadata block's header (an LZ4 length word, or an uncompressed objset) is at the
  start of its **first data column**. The single-disk pre-filters run over each child's
  buffer. Each hit at (child, offset) gives at most two candidate block origins: the
  normal layout, and for RAIDZ1 the "bit 20" layout where parity and first data column
  trade places.
* A candidate is kept only if the RAIDZ map of that origin, with the psize from the
  header, puts the first data column back exactly where the header was found. The
  block's columns are then assembled from the chunk buffers (a missing child is rebuilt
  from parity), and the block is decompressed and classified as on a single disk.

**Missing member.** A header whose first data column lies on a missing child cannot be
seen directly. For RAIDZ1 it can be rebuilt: the first row of a block is a window of
``acols`` consecutive RAIDZ sectors whose XOR is zero, so the missing sector is the XOR of
the others. The window start is unknown, so every width ``acols`` in 2..N and both layouts
are tried on the first 8 bytes only, using a running XOR. A candidate survives only if the
psize its header implies gives exactly that ``acols`` and places the column back on the
missing child. Everything carved is still unverified, and is checked later against the
parent block pointers that reference it.

Hits are stored by DVA offset, like single-disk hits, so reconstruction does not care
where they came from. Units are row ranges, keyed by the set of children present, so
imaging a missing member later and carving again adds what the new member reveals.
Blocks already in the map are not inserted twice.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import lz4.block
import numpy as np

from ..map.db import MapDB, j
from ..zfs import raidz
from ..zfs.vdev import TopVdev, phys
from .classify import Classified, PoolLimits, classify, classify_objset
from .scanner import MAX_META_LSIZE, PHASE, ChunkStats, Stop, lz4_be, prefilter

log = logging.getLogger(__name__)

TAIL = MAX_META_LSIZE + (64 << 10)


@dataclass
class RaidzHit:
    dva_offset: int
    kind: str
    comp: int
    psize: int
    c: Classified
    member: int              # child holding the first data column
    child_off: int           # its child-relative DVA-space offset
    virtual: bool = False    # found by rebuilding the missing member's sector
    consistent: bool | None = None                  # on-disk parity matches the data
    sectors: frozenset[tuple[int, int]] = frozenset()   # (child, sector) of every column
    digest: bytes = b""                             # hash of the decoded content

    @property
    def strength(self) -> int:
        if self.consistent:
            return 3
        if self.consistent is None:
            return 1 if self.virtual else 2
        return 0


class ChunkReader:
    """Row range [lo, hi) of every present child, plus one leading row and a tail."""

    def __init__(self, top: TopVdev, lo: int, hi: int, pool: ThreadPoolExecutor | None = None) -> None:
        self.top = top
        bs = 1 << top.ashift
        self.lo, self.hi = lo, hi
        self.buf_lo = max(0, lo - bs)
        length = hi + TAIL - self.buf_lo
        present = [lf for lf in top.slots if lf is not None]
        reads = (pool.map if pool else map)(lambda lf: lf.read(self.buf_lo, length), present)
        self.bufs = dict(zip([lf.child_id for lf in present], reads, strict=True))

    def fetch(self, child: int, offset: int, length: int) -> bytes:
        b = self.bufs.get(child)
        if b is None:
            return b""
        o = offset - self.buf_lo
        if o < 0 or o + length > len(b):
            return self.top._fetch(child, offset, length)
        return b[o:o + length]


def carve_rows(top: TopVdev, cr: ChunkReader, lim: PoolLimits) -> tuple[list[RaidzHit], ChunkStats]:
    """Find metadata blocks whose first data column starts in child rows [cr.lo, cr.hi)."""
    bs = 1 << top.ashift
    N, p = top.width, top.nparity
    st = ChunkStats(by_kind={})
    hits: list[RaidzHit] = []
    seen: set[int] = set()

    def assemble(origin: int, psize: int, child: int, child_off: int,
                 acols: int | None = None) -> bytes | None:
        if origin < 0 or origin in seen:
            return None
        rm = raidz.map_alloc(origin, psize, top.ashift, N, p)
        if origin + rm.asize > top.asize or not raidz.locates_first_data(rm, child, child_off):
            return None
        if acols is not None and len(rm.cols) != acols:
            return None
        try:
            cand = next(iter(top.candidates(origin, psize, fetch=cr.fetch)), None)
        except ValueError:
            return None
        return cand.data if cand else None

    def lz4_hit(data: bytes, clen: int, origin: int, psize: int, child: int, child_off: int,
                virtual: bool) -> None:
        try:
            d = lz4.block.decompress(data[4:4 + clen], uncompressed_size=MAX_META_LSIZE)
        except lz4.block.LZ4BlockError:
            return
        st.decompressed += 1
        n = len(d)
        if n < 512 or n & (n - 1):
            return
        c = classify(d, lim)
        if c is None:
            return
        seen.add(origin)
        hits.append(_hit(top, cr, origin, c.kind, 15, psize, c, child, child_off, virtual, d))

    def objset_hit(child: int, child_off: int, origins: list[int], virtual: bool,
                   acols: int | None = None) -> None:
        for size in (4096, 2048, 1024):
            for origin in origins:
                data = assemble(origin, size, child, child_off, acols)
                if data is None:
                    continue
                c = classify_objset(data[:size], lim)
                if c:
                    seen.add(origin)
                    hits.append(_hit(top, cr, origin, "objset", 2, size, c, child, child_off, virtual,
                                     data[:size]))
                    return

    first = (cr.lo - cr.buf_lo) // bs                 # first owned row within the buffers
    n_own = (cr.hi - cr.lo) // bs

    # ---- direct: headers on present children
    for child, buf in cr.bufs.items():
        nb = min(len(buf) // bs, first + n_own)
        if nb <= first:
            continue
        arr = np.frombuffer(buf, dtype=np.uint8, count=nb * bs).reshape(nb, bs)[first:]
        m, be, cand = prefilter(arr)
        st.candidates += len(cand)
        for i in np.nonzero(m)[0]:
            off = cr.lo + int(i) * bs
            objset_hit(child, off, raidz.first_data_origins(child, off, top.ashift, N, p), False)
        for i in cand:
            off = cr.lo + int(i) * bs
            clen = int(be[i])
            psize = -(-(clen + 4) // 512) * 512
            for origin in raidz.first_data_origins(child, off, top.ashift, N, p):
                data = assemble(origin, psize, child, off)
                if data is not None:
                    lz4_hit(data, clen, origin, psize, child, off, False)

    # ---- virtual: headers on a missing child (RAIDZ1, one child missing)
    missing = [c for c in range(N) if top.slots[c] is None]
    if p == 1 and len(missing) == 1 and cr.bufs:
        hits_before = len(hits)
        _virtual(top, cr, first, n_own, missing[0], lz4_hit, objset_hit, assemble, st)
        st.by_kind["virtual"] = len(hits) - hits_before

    hits = _drop_parity_echoes(hits, st)
    st.hits = len(hits)
    for h in hits:
        st.by_kind[h.kind] = st.by_kind.get(h.kind, 0) + 1
    return hits, st


def _hit(top: TopVdev, cr: ChunkReader, origin: int, kind: str, comp: int, psize: int, c: Classified,
         child: int, child_off: int, virtual: bool, content: bytes) -> RaidzHit:
    rm = raidz.map_alloc(origin, psize, top.ashift, top.width, top.nparity)
    sectors = frozenset((col.devidx, (col.offset >> top.ashift) + k)
                        for col in rm.cols for k in range(col.size >> top.ashift))
    return RaidzHit(origin, kind, comp, psize, c, child, child_off, virtual,
                    consistent=top.parity_consistent(origin, psize, cr.fetch), sectors=sectors,
                    digest=hashlib.blake2b(content, digest_size=16).digest())


def _drop_parity_echoes(hits: list[RaidzHit], st: ChunkStats) -> list[RaidzHit]:
    """Remove copies of a block seen at the wrong origin.

    With one data column, RAIDZ1 parity is an exact copy of the data. So the same header
    also appears on the parity disk, and read from there it looks like a block starting
    one sector away. Rebuilding a missing sector through a one-column window likewise
    copies a neighbour's sector. Two hits whose columns share a sector *and* whose decoded
    contents are identical are the same bytes seen twice: two different real blocks can
    never occupy the same sector with the same content. Of such a pair, the better-attested
    one is kept: parity matches, then read directly, then rebuilt, then parity mismatch.
    A real old block whose parity was later overwritten has no identical overlapping
    twin, so it is kept.
    """
    order = sorted(range(len(hits)), key=lambda i: (-hits[i].strength, hits[i].dva_offset))
    owner: dict[tuple[int, int], list[int]] = {}
    drop: set[int] = set()
    for i in order:
        h = hits[i]
        twins = {k for sec in h.sectors for k in owner.get(sec, ()) if hits[k].digest == h.digest}
        if twins:
            drop.add(i)
            continue
        for sec in h.sectors:
            owner.setdefault(sec, []).append(i)
    if drop:
        st.by_kind["parity_echo"] = len(drop)
    return [h for i, h in enumerate(hits) if i not in drop]


def _virtual(top: TopVdev, cr: ChunkReader, first: int, n_own: int, m: int,
             lz4_hit, objset_hit, assemble, st: ChunkStats) -> None:
    bs = 1 << top.ashift
    N = top.width
    R = min(len(b) for b in cr.bufs.values()) // bs
    if first >= R:
        return
    # First 8 bytes of every sector, in RAIDZ (row-major) order; the missing child is 0.
    H = np.zeros((R, N, 8), dtype=np.uint8)
    for c, b in cr.bufs.items():
        H[:, c, :] = np.frombuffer(b, dtype=np.uint8, count=R * bs).reshape(R, bs)[:, :8]
    F = H.reshape(R * N, 8)
    P = np.zeros((R * N + 1, 8), dtype=np.uint8)
    np.bitwise_xor.accumulate(F, axis=0, out=P[1:])
    rows = np.arange(first, min(R, first + n_own))
    s1 = rows * N + m                                   # flat index of the missing sector
    base = (cr.buf_lo >> top.ashift) * N                # absolute RAIDZ sector of flat index 0
    for acols in range(2, N + 1):
        for swapped, u in ((False, s1 - 1), (True, s1)):   # window start: normal, bit-20 swapped
            v = u + acols
            ok = (u >= 0) & (v <= R * N)
            uu, vv, rr = u[ok], v[ok], rows[ok]
            hdr = P[vv] ^ P[uu]
            be = lz4_be(hdr)
            cand = np.nonzero((be >= 8) & (be <= MAX_META_LSIZE - 4) & ((hdr[:, 4] >> 4) != 0))[0]
            st.candidates += len(cand)
            for k in cand:
                off = cr.buf_lo + int(rr[k]) * bs
                origin = (base + int(uu[k])) << top.ashift
                clen = int(be[k])
                psize = -(-(clen + 4) // 512) * 512
                data = assemble(origin, psize, m, off, acols)
                if data is not None:
                    lz4_hit(data, clen, origin, psize, m, off, True)
            if acols == 2:
                # a one-sector block: the missing data sector equals its parity sector,
                # which is column 0 (normal layout) or the column after it (swapped)
                par = uu + 1 if swapped else uu
                objs = np.zeros((len(par), bs), dtype=np.uint8)
                for c, b in cr.bufs.items():
                    sel = (par % N) == c
                    if sel.any():
                        a = np.frombuffer(b, dtype=np.uint8, count=R * bs).reshape(R, bs)
                        objs[sel] = a[par[sel] // N]
                mask, _be, _c = prefilter(objs)
                for k in np.nonzero(mask)[0]:
                    off = cr.buf_lo + int(rr[k]) * bs
                    objset_hit(m, off, [(base + int(uu[k])) << top.ashift], True, acols)


def run_raidz_carve(db: MapDB, pool_id: int, top: TopVdev, base_phys: int, lim: PoolLimits, *,
                    chunk_size: int = 64 << 20, start: int = 0, end: int | None = None,
                    stop: Stop | None = None,
                    progress: Callable[[int, int, ChunkStats, float], None] | None = None) -> bool:
    """Carve RAIDZ DVA range [start, end) of *top*. Returns True if the range completed."""
    bs = 1 << top.ashift
    N = top.width
    end = top.asize if end is None else min(end, top.asize)
    c_start = ((start >> top.ashift) // N) << top.ashift
    c_end = -(-(end >> top.ashift) // N) << top.ashift
    step = max(bs, (chunk_size // N) // bs * bs)
    mask = sum(1 << c for c in range(N) if top.slots[c] is not None)
    prefix = f"{pool_id}:{top.id}:r"
    done = db.done_units(PHASE)
    offs = list(range(c_start - c_start % step, c_end, step))
    remaining = [o for o in offs if f"{prefix}{o:x}:m{mask:x}" not in done]
    log.info("carving raidz%d vdev %d (%d-wide, children present %s): %d row chunks of %d MiB per "
             "child, %d already done", top.nparity, top.id, N,
             [c for c in range(N) if top.slots[c] is not None], len(offs), step >> 20,
             len(offs) - len(remaining))
    t0 = time.monotonic()
    bytes_done = 0
    with ThreadPoolExecutor(max_workers=max(1, mask.bit_count())) as ex:
        for o in remaining:
            if stop and stop.event.is_set():
                log.warning("carving stopped by request; resume with the same command")
                return False
            hi = min(o + step, c_end)
            cr = ChunkReader(top, o, hi, ex)
            hits, st = carve_rows(top, cr, lim)
            lo_dva, hi_dva = (o >> top.ashift) * N * bs - bs * N, ((hi >> top.ashift) + 1) * N * bs
            with db.tx():
                have = {(r[0], r[1]) for r in db.execute(
                    "SELECT dva_offset, kind FROM carved WHERE pool_id=? AND vdev_top=? AND "
                    "dva_offset BETWEEN ? AND ?", (pool_id, top.id, lo_dva, hi_dva))}
                for h in hits:
                    if (h.dva_offset, h.kind) in have:
                        continue
                    c = h.c
                    info = dict(c.info or {})
                    if h.virtual:
                        info["carved_from"] = f"rebuilt sector of missing child {h.member}"
                    db.conn.execute(
                        "INSERT INTO carved(pool_id,vdev_top,dva_offset,phys,kind,comp,psize_hint,lsize,"
                        "child_type,child_level,n_children,n_holes,min_birth,max_birth,os_type,info_json,"
                        "member) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pool_id, top.id, h.dva_offset, base_phys + phys(h.child_off), h.kind, h.comp,
                         h.psize, c.lsize, c.child_type, c.child_level, c.n_children, c.n_holes,
                         c.min_birth, c.max_birth, c.os_type, j(info) if info else None, h.member))
                db.mark(PHASE, f"{prefix}{o:x}:m{mask:x}",
                        detail=j({"hits": st.hits, "cand": st.candidates, "kinds": st.by_kind}))
            bytes_done += (hi - o) * len(cr.bufs)
            if progress:
                progress((hi >> top.ashift) * N * bs, end, st,
                         bytes_done / max(1e-9, time.monotonic() - t0))
    return True
