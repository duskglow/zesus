"""Translation from DVAs to physical reads.

Each top-level vdev turns ``(offset, psize)`` into *candidate readings*, cheapest first.
The block reader accepts the first one whose checksum matches the parent block pointer,
so nothing here ever decides that data is correct.

* ``disk``/``file``: one reading.
* ``mirror``: one reading per present child, healthiest first (fewest checksum failures
  seen so far, then the order the children were given in).
* ``raidz`` (1-3 parity): the data columns as read; then, if a data column is missing or
  the first reading fails, the missing columns rebuilt from parity. If every column was
  present but the checksum still failed, one then two then three data columns are
  treated as damaged and rebuilt in turn (OpenZFS's "combinatorial reconstruction").
  Asking for more columns than parity allows yields nothing.
* ``draid`` and RAIDZ vdevs that were expanded are reported as unsupported.

For bulk reads, :meth:`TopVdev.read_window` reads a DVA range in physical order. On a
RAIDZ vdev that is the same row range on every child, read one thread per child.
:meth:`Window.gather` then lays each block's bytes out contiguously, rebuilding from
parity inside the window where needed.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..errors import Unsupported
from ..io.source import ReadOnlySource
from . import gf256, raidz
from .constants import VDEV_LABEL_START_SIZE

log = logging.getLogger(__name__)

Fetch = Callable[[int, int, int], bytes]        # (child, child_offset, length) -> bytes


def phys(offset: int) -> int:
    """Byte offset within a leaf device of DVA-space *offset* (past labels and boot area)."""
    return VDEV_LABEL_START_SIZE + offset


@dataclass
class LeafDevice:
    guid: int
    path: str
    source: ReadOnlySource
    child_id: int = 0
    errors: int = 0             # checksum failures attributed to this child

    def read(self, offset: int, length: int) -> bytes:
        return self.source.pread(phys(offset), length)

    def physical(self, offset: int) -> int:
        return phys(offset)


@dataclass
class Candidate:
    data: bytes
    provenance: str                    # "direct", "mirror:<child>", "raidz:rebuilt[1]", ...
    leaf: LeafDevice | None = None     # the child that supplied it (disk/mirror)
    repaired: tuple[int, ...] = ()     # child ids rebuilt from parity


@dataclass
class TopVdev:
    id: int
    type: str
    guid: int
    ashift: int
    asize: int
    children: list[LeafDevice] = field(default_factory=list)       # present leaves
    missing: list[dict[str, Any]] = field(default_factory=list)    # config of absent leaves
    nparity: int = 0
    width: int = 1                     # number of child slots (RAIDZ columns)
    slots: list[LeafDevice | None] = field(default_factory=list)   # by child id (RAIDZ)
    unsupported: str | None = None

    # ------------------------------------------------------------------ geometry
    def extent(self, psize: int) -> int:
        """Bytes of DVA space a block of *psize* occupies (for windowing)."""
        if self.type == "raidz":
            return raidz.asize_of(psize, self.ashift, self.width, self.nparity)
        return psize

    def extents(self, psize: np.ndarray) -> np.ndarray:
        if self.type != "raidz":
            return psize.astype(np.int64)
        s = ((psize.astype(np.int64) - 1) >> self.ashift) + 1
        nd = self.width - self.nparity
        s = s + self.nparity * ((s + nd - 1) // nd)
        s = -(-s // (self.nparity + 1)) * (self.nparity + 1)
        return s << self.ashift

    # ------------------------------------------------------------------ reading
    def readers(self) -> list[LeafDevice]:
        """Present leaves (mirror semantics). Kept for callers that list devices."""
        return self.children

    def _check(self) -> None:
        if self.unsupported:
            raise Unsupported(self.unsupported)

    def candidates(self, offset: int, psize: int, notes: list[str] | None = None,
                   fetch: Fetch | None = None) -> Iterator[Candidate]:
        """Alternative readings of the block at *offset*, cheapest first. Lazy: parity is
        only read once the caller asks for the next candidate."""
        self._check()
        notes = notes if notes is not None else []
        if self.type == "raidz":
            yield from self._raidz_candidates(offset, psize, notes, fetch)
            return
        leaves = sorted(self.children, key=lambda lf: lf.errors) if self.type == "mirror" else self.children
        if not leaves:
            notes.append(f"vdev {self.id}: no device present")
        for lf in leaves:
            data = fetch(lf.child_id, offset, psize) if fetch else lf.read(offset, psize)
            if len(data) == psize:
                prov = f"mirror:{lf.child_id}" if self.type == "mirror" else "direct"
                yield Candidate(data, prov, lf)
            else:
                notes.append(f"{lf.path}: short read at {offset:#x}")

    def note_result(self, cand: Candidate, ok: bool) -> None:
        if cand.leaf is not None and not ok:
            cand.leaf.errors += 1

    # ------------------------------------------------------------------ RAIDZ
    def _raidz_candidates(self, offset: int, psize: int, notes: list[str],
                          fetch: Fetch | None) -> Iterator[Candidate]:
        rm = raidz.map_alloc(offset, psize, self.ashift, self.width, self.nparity)
        rd = fetch or self._fetch

        def col(c: raidz.Column) -> np.ndarray | None:
            b = rd(c.devidx, c.offset, c.size)
            return np.frombuffer(b, np.uint8) if len(b) == c.size else None

        data = [col(c) for c in rm.data_cols]
        sizes = [c.size for c in rm.data_cols]
        miss = [rm.data_cols[i].devidx for i, d in enumerate(data) if d is None]
        if not miss:
            yield Candidate(_join(data, psize), "direct")
        par = [col(c) for c in rm.parity_cols]
        pmiss = [rm.parity_cols[i].devidx for i, x in enumerate(par) if x is None]
        if miss:
            if len(miss) > sum(1 for x in par if x is not None):
                notes.append(f"raidz vdev {self.id} @{offset:#x}: children {miss + pmiss} unavailable, "
                             f"more than parity can rebuild")
                return
            out = gf256.reconstruct(data, par, sizes)
            yield Candidate(_join(out, psize), f"raidz:rebuilt{miss}", repaired=tuple(miss))
            # Rebuilt data failed its checksum: with spare parity, also try treating one
            # more present column as damaged.
            spare = sum(1 for x in par if x is not None) - len(miss)
            yield from self._combrec(rm, data, par, sizes, psize, spare, base=miss)
            return
        # all data present but the direct reading was rejected
        avail = sum(1 for x in par if x is not None)
        if avail == 0:
            notes.append(f"raidz vdev {self.id} @{offset:#x}: no parity available to repair")
            return
        yield from self._combrec(rm, data, par, sizes, psize, avail, base=[])

    def _combrec(self, rm: raidz.RaidzMap, data: list, par: list, sizes: list[int], psize: int,
                 budget: int, base: list[int]) -> Iterator[Candidate]:
        present = [i for i, d in enumerate(data) if d is not None]
        for k in range(1, budget + 1):
            for bad in itertools.combinations(present, k):
                trial = [None if i in bad else d for i, d in enumerate(data)]
                try:
                    out = gf256.reconstruct(trial, par, sizes)
                except ValueError:
                    continue
                kids = base + [rm.data_cols[i].devidx for i in bad]
                yield Candidate(_join(out, psize), f"raidz:combrec{kids}", repaired=tuple(kids))

    def parity_consistent(self, offset: int, psize: int, fetch: Fetch | None = None) -> bool | None:
        """Does the on-disk parity of this RAIDZ block match its data columns?

        None when a column is unavailable (a rebuilt block is consistent by construction,
        so there is nothing to check). Used by carving to tell a real block from a copy
        of it seen through another block's parity column.
        """
        rm = raidz.map_alloc(offset, psize, self.ashift, self.width, self.nparity)
        rd = fetch or self._fetch
        cols = [rd(c.devidx, c.offset, c.size) for c in rm.cols]
        if any(len(b) != c.size for b, c in zip(cols, rm.cols, strict=True)):
            return None
        data = [np.frombuffer(b, np.uint8) for b in cols[self.nparity:]]
        want = gf256.parity(data, self.nparity, rm.cols[0].size)
        return all(w.tobytes() == b for w, b in zip(want, cols[:self.nparity], strict=True))

    def _fetch(self, child: int, offset: int, length: int) -> bytes:
        lf = self.slots[child] if child < len(self.slots) else None
        if lf is None:
            return b""
        return lf.read(offset, length)

    # ------------------------------------------------------------------ bulk windows
    def read_window(self, start: int, end: int) -> Window:
        """Read DVA range [start, end) in physical order (one request per child)."""
        self._check()
        if self.type == "raidz":
            lo, hi = raidz.child_row_range(start, end, self.ashift, self.width)
            present = [lf for lf in self.slots if lf is not None]
            with ThreadPoolExecutor(max_workers=max(1, len(present))) as ex:
                bufs = dict(zip([lf.child_id for lf in present],
                                ex.map(lambda lf: lf.read(lo, hi - lo), present), strict=True))
            return RaidzWindow(self, start, end, lo, bufs)
        leaves = sorted(self.children, key=lambda lf: lf.errors)
        buf = leaves[0].read(start, end - start) if leaves else b""
        return LinearWindow(self, start, end, buf)


def _join(cols: Sequence[np.ndarray], psize: int) -> bytes:
    return b"".join(c.tobytes() for c in cols)[:psize]


class Window:
    """A DVA range read in bulk. :meth:`gather` returns the bytes of many blocks."""

    def __init__(self, top: TopVdev, start: int, end: int) -> None:
        self.top, self.start, self.end = top, start, end
        self.repaired = 0          # blocks rebuilt from parity while gathering

    def gather(self, offs: np.ndarray, psizes: np.ndarray) -> tuple[bytes, np.ndarray, np.ndarray]:
        """Return (buf, local_offsets, available) for blocks at DVA *offs* of *psizes*.

        ``buf[local[i]:local[i]+psizes[i]]`` is block *i* as best read from this window.
        ``available[i]`` is False where it could not be assembled here. Nothing is
        verified: callers checksum the bytes.
        """
        raise NotImplementedError

    def candidates(self, offset: int, psize: int, notes: list[str] | None = None) -> Iterator[Candidate]:
        raise NotImplementedError


class LinearWindow(Window):
    def __init__(self, top: TopVdev, start: int, end: int, buf: bytes) -> None:
        super().__init__(top, start, end)
        self.buf = buf

    def gather(self, offs, psizes):
        local = offs.astype(np.int64) - self.start
        return self.buf, local, (local >= 0) & (local + psizes <= len(self.buf))

    def candidates(self, offset, psize, notes=None):
        o = offset - self.start
        if o >= 0 and o + psize <= len(self.buf):
            yield Candidate(self.buf[o:o + psize], "direct")


class RaidzWindow(Window):
    def __init__(self, top: TopVdev, start: int, end: int, lo: int, bufs: dict[int, bytes]) -> None:
        super().__init__(top, start, end)
        self.lo, self.bufs = lo, bufs

    def fetch(self, child: int, offset: int, length: int) -> bytes:
        b = self.bufs.get(child)
        if b is None:
            return b""
        o = offset - self.lo
        if o < 0 or o + length > len(b):
            return self.top._fetch(child, offset, length)    # outside the window: read it
        return b[o:o + length]

    def candidates(self, offset, psize, notes=None):
        return self.top.candidates(offset, psize, notes, fetch=self.fetch)

    def gather(self, offs, psizes):
        out = bytearray()
        local = np.zeros(len(offs), dtype=np.int64)
        avail = np.zeros(len(offs), dtype=bool)
        for i, (o, p) in enumerate(zip(offs.tolist(), psizes.tolist(), strict=True)):
            local[i] = len(out)
            try:
                c = next(iter(self.candidates(int(o), int(p))), None)
            except (ValueError, Unsupported):
                c = None
            if c is None:
                out += b"\0" * int(p)
                continue
            if c.repaired:
                self.repaired += 1
            out += c.data
            avail[i] = True
        return bytes(out), local, avail


class VdevMap:
    """All top-level vdevs of a pool, with whichever leaf devices we have images for."""

    def __init__(self) -> None:
        self.top: dict[int, TopVdev] = {}

    @classmethod
    def from_config(cls, vdev_tree: dict[str, Any], leaves: dict[int, ReadOnlySource],
                    vdev_children: int = 1) -> VdevMap:  # noqa: ARG003
        m = cls()
        m.add_top(vdev_tree, leaves)
        return m

    def add_top(self, tree: dict[str, Any], leaves: dict[int, ReadOnlySource]) -> TopVdev:
        vid = tree.get("id", 0)
        top = self.top.get(vid)
        if top is None:
            typ = tree.get("type", "?")
            kids = tree.get("children") or []
            top = TopVdev(id=vid, type=typ, guid=tree.get("guid", 0),
                          ashift=tree.get("ashift", 9), asize=tree.get("asize", 0),
                          nparity=tree.get("nparity", 0), width=max(1, len(kids)))
            top.slots = [None] * top.width
            if typ == "draid" or typ.startswith("draid"):
                top.unsupported = f"dRAID vdevs are not supported yet (vdev {vid})"
            elif typ == "raidz" and not 1 <= top.nparity <= 3:
                top.unsupported = f"raidz vdev {vid} has nparity={top.nparity}"
            elif typ == "raidz" and ("raidz_expand_txg" in tree or "raidz_expanding" in tree):
                top.unsupported = f"raidz vdev {vid} was expanded (raidz_expansion): not supported yet"
            self.top[vid] = top
        known = {c.guid for c in top.children}
        kids = tree.get("children") or [tree]
        for pos, child in enumerate(kids):
            cid = child.get("id", pos) if tree.get("children") else 0
            # A child may itself be a replacing/spare vdev: any of its leaves will do.
            for leaf in _leaves(child):
                g = leaf.get("guid", 0)
                if g in known:
                    break
                if g in leaves:
                    lf = LeafDevice(guid=g, path=leaf.get("path", "?"), source=leaves[g], child_id=cid)
                    top.children.append(lf)
                    if cid < len(top.slots) and top.slots[cid] is None:
                        top.slots[cid] = lf
                    known.add(g)
                    top.missing = [m for m in top.missing if m.get("guid") not in _guids(child)]
                    break
            else:
                if top.type == "raidz" and cid < len(top.slots) and top.slots[cid] is not None:
                    continue
                if not any(lf.guid in _guids(child) for lf in top.children) and \
                        not any(m.get("guid") == child.get("guid") for m in top.missing):
                    top.missing.append({**child, "id": cid})
        return top

    def describe(self) -> list[str]:
        out = []
        for vid, t in sorted(self.top.items()):
            extra = f" nparity={t.nparity} width={t.width}" if t.type == "raidz" else ""
            out.append(f"vdev {vid}: {t.type}{extra} ashift={t.ashift} asize={t.asize:#x} "
                       f"present={len(t.children)} missing={len(t.missing)}"
                       + (f" UNSUPPORTED: {t.unsupported}" if t.unsupported else ""))
        return out


def _leaves(tree: dict[str, Any]) -> list[dict[str, Any]]:
    kids = tree.get("children")
    if not kids:
        return [tree]
    out: list[dict[str, Any]] = []
    for c in kids:
        out.extend(_leaves(c))
    return out


def _guids(tree: dict[str, Any]) -> set[int]:
    return {lf.get("guid", 0) for lf in _leaves(tree)} | {tree.get("guid", 0)}
