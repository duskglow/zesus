"""dnode_phys_t and gap-aware reads of DMU objects through their block-pointer trees."""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import CorruptStructure
from . import blkptr
from .blkptr import BlockPointer
from .constants import (
    BLKPTR_SHIFT,
    BLKPTR_SIZE,
    DNODE_CORE_SIZE,
    DNODE_FLAG_SPILL_BLKPTR,
    DNODE_SIZE,
    dmu_type_name,
    dmu_type_valid,
)
from .reader import BlockRead, BlockReader, ReadStatus

log = logging.getLogger(__name__)


@dataclass
class Dnode:
    type: int
    indblkshift: int
    nlevels: int
    nblkptr: int
    bonustype: int
    checksum: int
    compress: int
    flags: int
    datablkszsec: int
    bonuslen: int
    extra_slots: int
    maxblkid: int
    used: int
    blkptrs: list[BlockPointer]
    bonus: bytes
    spill: BlockPointer | None
    byteorder: str = "<"

    @property
    def datablksz(self) -> int:
        return self.datablkszsec << 9

    @property
    def slots(self) -> int:
        return 1 + self.extra_slots

    @property
    def is_free(self) -> bool:
        return self.type == 0

    @property
    def logical_size(self) -> int:
        return (self.maxblkid + 1) * self.datablksz

    def describe(self) -> str:
        return (f"{dmu_type_name(self.type)} lvls={self.nlevels} dblk={self.datablksz:#x} "
                f"ibs=2^{self.indblkshift} maxblkid={self.maxblkid} bonus={dmu_type_name(self.bonustype)}"
                f"[{self.bonuslen}]")


def parse_dnode(buf: bytes | memoryview, offset: int = 0, byteorder: str = "<",
                strict: bool = True) -> Dnode:
    b = bytes(buf[offset:offset + DNODE_SIZE])
    if len(b) < DNODE_SIZE:
        raise CorruptStructure("dnode truncated")
    (typ, ibs, nlv, nbp, btype, ck, comp, flags, dbss, blen, extra) = struct.unpack_from(
        byteorder + "8BHHB", b, 0)
    maxblkid, used = struct.unpack_from(byteorder + "QQ", b, 16)
    if strict and typ != 0:
        if not dmu_type_valid(typ) or nbp < 1 or nbp > 3 or nlv < 1 or nlv > 10 \
                or not (9 <= ibs <= 17) and ibs != 0:
            raise CorruptStructure(f"implausible dnode (type={typ} nblkptr={nbp} nlevels={nlv} ibs={ibs})")
    total = (1 + extra) * DNODE_SIZE
    if extra:
        b = bytes(buf[offset:offset + total])
    nbp_eff = max(0, min(nbp, 3 + extra * 4))
    bps = [blkptr.parse(b, DNODE_CORE_SIZE + i * BLKPTR_SIZE, byteorder) for i in range(nbp_eff)]
    bonus_off = DNODE_CORE_SIZE + nbp_eff * BLKPTR_SIZE
    spill = None
    if flags & DNODE_FLAG_SPILL_BLKPTR:
        spill = blkptr.parse(b, len(b) - BLKPTR_SIZE, byteorder)
    return Dnode(typ, ibs, nlv, nbp, btype, ck, comp, flags, dbss, blen, extra, maxblkid, used,
                 bps, b[bonus_off:bonus_off + blen], spill, byteorder)


class ObjectReader:
    """Reads a DMU object's data through its dnode's block tree.

    ``read(off, len)`` raises on anything that is not verified. ``read_gapped`` returns
    per-block statuses, so callers can recover what exists around a gap.
    """

    def __init__(self, reader: BlockReader, dnode: Dnode) -> None:
        self.r = reader
        self.dn = dnode
        self.epb_shift = dnode.indblkshift - BLKPTR_SHIFT if dnode.indblkshift else 0

    # -- tree walking --------------------------------------------------------------
    def block_pointer(self, blkid: int) -> tuple[BlockPointer | None, BlockRead | None]:
        """Find the L0 bp for *blkid*. Returns (bp, None). If an indirect block on the
        way is unreadable, returns (None, the failed read). A hole gives (hole bp, None)."""
        dn = self.dn
        if blkid > dn.maxblkid:
            return None, None
        level = dn.nlevels - 1
        top_index = blkid >> (self.epb_shift * level)
        if top_index >= len(dn.blkptrs):
            return None, None
        bp = dn.blkptrs[top_index]
        while level > 0:
            if bp.is_hole:
                return bp, None
            r = self.r.read(bp)
            if not r.ok or r.data is None:
                return None, r
            level -= 1
            idx = (blkid >> (self.epb_shift * level)) & ((1 << self.epb_shift) - 1)
            bp = blkptr.parse(r.data, idx * BLKPTR_SIZE, dn.byteorder)
        return bp, None

    def read_block(self, blkid: int) -> BlockRead:
        bp, failed = self.block_pointer(blkid)
        if failed is not None:
            return BlockRead(failed.status, None, notes=["indirect block unreadable"] + failed.notes)
        if bp is None or bp.is_hole:
            return BlockRead(ReadStatus.HOLE, b"\0" * self.dn.datablksz)
        r = self.r.read(bp)
        if r.data is not None and len(r.data) < self.dn.datablksz and self.dn.maxblkid == 0:
            # single-block objects may have lsize < datablksz only when datablksz grew; pad
            r = BlockRead(r.status, r.data + b"\0" * (self.dn.datablksz - len(r.data)), r.dva, r.notes)
        return r

    def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        bs = self.dn.datablksz
        end = offset + length
        pos = offset
        while pos < end:
            blkid = pos // bs
            r = self.read_block(blkid)
            if r.status not in (ReadStatus.OK, ReadStatus.HOLE, ReadStatus.UNVERIFIED) or r.data is None:
                raise CorruptStructure(f"object block {blkid} unreadable: {r.status.value} {r.notes}")
            inner = pos - blkid * bs
            take = min(bs - inner, end - pos)
            chunk = r.data[inner:inner + take]
            if len(chunk) < take:
                chunk += b"\0" * (take - len(chunk))
            out += chunk
            pos += take
        return bytes(out)

    def read_all(self) -> bytes:
        return self.read(0, self.dn.logical_size)

    def iter_l0(self) -> Iterator[tuple[int, BlockPointer | None, BlockRead | None]]:
        """Yield (blkid, bp, failure) for every L0 slot reachable in the tree, in order.

        Subtrees under an unreadable indirect block yield a single tuple (first_blkid, None,
        failure). Callers can then mark the whole span that subtree covers.
        """
        dn = self.dn
        span_top = 1 << (self.epb_shift * (dn.nlevels - 1))
        for i, bp in enumerate(dn.blkptrs):
            yield from self._walk(bp, dn.nlevels - 1, i * span_top)

    def _walk(self, bp: BlockPointer, level: int, first: int) -> Iterator[tuple[int, BlockPointer | None, BlockRead | None]]:
        if first > self.dn.maxblkid:
            return
        if level == 0 or bp.is_hole:
            yield first, bp, None
            return
        r = self.r.read(bp)
        if not r.ok or r.data is None:
            yield first, None, r
            return
        span = 1 << (self.epb_shift * (level - 1))
        for i in range(len(r.data) // BLKPTR_SIZE):
            child = blkptr.parse(r.data, i * BLKPTR_SIZE, self.dn.byteorder)
            yield from self._walk(child, level - 1, first + i * span)

    def span_of_level(self, level: int) -> int:
        """Number of L0 blocks covered by one block pointer at *level*."""
        return 1 << (self.epb_shift * level)
