"""Verified block reads: blkptr → bytes.

Every read tries each DVA (and each mirror child) in turn. It checks the checksum
recorded in the block pointer *before* decompressing, so data is never returned
unverified. The only exception is callers that explicitly ask for ``verify=False``
(carving, diagnostics). Those get the verification outcome back in ``BlockRead.status``.
"""

from __future__ import annotations

import logging
import struct
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ..errors import ChecksumMismatch, CorruptStructure, DecompressionError, Unsupported
from . import blkptr as bpmod
from . import checksum, compress
from .blkptr import BlockPointer, Dva
from .constants import ChecksumType
from .vdev import VdevMap

log = logging.getLogger(__name__)

SPA_GANGBLOCKSIZE = 512


class ReadStatus(str, Enum):
    OK = "ok"                        # checksum verified
    UNVERIFIED = "unverified"        # checksum off/unsupported; data returned as-is
    CHECKSUM_MISMATCH = "checksum_mismatch"
    ZEROED = "zeroed"                # every copy read back as zeros (freed + TRIM?)
    DECOMPRESS_FAILED = "decompress_failed"
    UNREADABLE = "unreadable"        # beyond device end, device missing, unsupported vdev
    HOLE = "hole"


@dataclass
class BlockRead:
    status: ReadStatus
    data: bytes | None
    dva: Dva | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (ReadStatus.OK, ReadStatus.HOLE)


BlockObserver = Callable[[BlockPointer, BlockRead], None]


class BlockReader:
    def __init__(self, vdevs: VdevMap, cache_blocks: int = 4096) -> None:
        self.vdevs = vdevs
        self._cache: OrderedDict[bytes, BlockRead] = OrderedDict()
        self._cache_max = cache_blocks
        self.observers: list[BlockObserver] = []
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ raw physical
    def read_dva(self, dva: Dva, length: int) -> list[tuple[str, bytes]]:
        """Read *length* bytes at *dva* from every available copy (mirror children)."""
        top = self.vdevs.top.get(dva.vdev)
        if top is None:
            raise Unsupported(f"DVA references unknown vdev {dva.vdev}")
        out = []
        for leaf in top.readers():
            data = leaf.read(dva.offset, length)
            if len(data) == length:
                out.append((leaf.path, data))
        return out

    # ------------------------------------------------------------------ verified reads
    def read(self, bp: BlockPointer, *, verify: bool = True, decompress: bool = True) -> BlockRead:
        key = bp.raw
        if verify and decompress and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        r = self._read_uncached(bp, verify=verify, decompress=decompress)
        self._count(r.status.value)
        for obs in self.observers:
            obs(bp, r)
        if verify and decompress and r.status in (ReadStatus.OK, ReadStatus.HOLE, ReadStatus.UNVERIFIED):
            self._cache[key] = r
            if len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
        return r

    def read_ok(self, bp: BlockPointer) -> bytes:
        """Read and return data, or raise if it cannot be verified."""
        r = self.read(bp)
        if r.status == ReadStatus.HOLE:
            return b"\0" * bp.lsize
        if r.status in (ReadStatus.OK, ReadStatus.UNVERIFIED) and r.data is not None:
            return r.data
        if r.status == ReadStatus.CHECKSUM_MISMATCH:
            raise ChecksumMismatch(f"{r.status.value}: {bp.describe()} {'; '.join(r.notes)}")
        raise CorruptStructure(f"{r.status.value}: {bp.describe()} {'; '.join(r.notes)}")

    def _count(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _read_uncached(self, bp: BlockPointer, verify: bool, decompress: bool) -> BlockRead:
        if bp.embedded:
            return self._read_embedded(bp, decompress)
        if bp.is_hole:
            return BlockRead(ReadStatus.HOLE, b"\0" * bp.lsize if decompress else None)
        if bp.encrypted:
            return BlockRead(ReadStatus.UNREADABLE, None, notes=["block is encrypted"])
        notes: list[str] = []
        saw_zero = False          # a copy read back as all zeros
        saw_mismatch = False      # a copy had non-zero content that failed its checksum
        for dva in bp.valid_dvas:
            try:
                copies = (self._read_gang(bp, dva, notes) if dva.gang
                          else self.read_dva(dva, bp.psize))
            except Unsupported as exc:
                notes.append(str(exc))
                continue
            except CorruptStructure as exc:
                notes.append(f"{dva}: {exc}")
                continue
            if not copies:
                notes.append(f"{dva}: beyond end of device or no device")
            for path, raw in copies:
                status = self._verify(bp, raw)
                if status is ReadStatus.CHECKSUM_MISMATCH:
                    if any(raw):
                        saw_mismatch = True
                        notes.append(f"{dva}@{path}: checksum mismatch (overwritten?)")
                    else:
                        saw_zero = True
                        notes.append(f"{dva}@{path}: reads as zeros (freed and trimmed?)")
                    if verify:
                        continue
                if not decompress:
                    return BlockRead(status, raw, dva, notes)
                try:
                    data = compress.decompress(bp.comp, raw, bp.lsize)
                except (DecompressionError, Unsupported) as exc:
                    notes.append(f"{dva}@{path}: {exc}")
                    if status is ReadStatus.OK:
                        return BlockRead(ReadStatus.DECOMPRESS_FAILED, None, dva, notes)
                    continue
                return BlockRead(status, data, dva, notes)
        if saw_mismatch:
            return BlockRead(ReadStatus.CHECKSUM_MISMATCH, None, None, notes)
        if saw_zero:
            return BlockRead(ReadStatus.ZEROED, None, None, notes)
        return BlockRead(ReadStatus.UNREADABLE, None, None, notes)

    def _verify(self, bp: BlockPointer, raw: bytes) -> ReadStatus:
        ct = bp.cksum_type
        if ct in (ChecksumType.OFF, ChecksumType.NOPARITY, ChecksumType.INHERIT):
            return ReadStatus.UNVERIFIED
        try:
            actual = checksum.compute(ct, raw, byteswap=not bp.little_endian)
        except Unsupported:
            return ReadStatus.UNVERIFIED
        return ReadStatus.OK if actual == bp.cksum else ReadStatus.CHECKSUM_MISMATCH

    def _read_embedded(self, bp: BlockPointer, decompress: bool) -> BlockRead:
        if bp.etype != 0:  # BP_EMBEDDED_TYPE_DATA; others (redacted) carry no data
            return BlockRead(ReadStatus.UNREADABLE, None, notes=[f"embedded type {bp.etype}"])
        payload = bp.embedded_payload()
        if not decompress:
            return BlockRead(ReadStatus.OK, payload)
        try:
            return BlockRead(ReadStatus.OK, compress.decompress(bp.comp, payload, bp.lsize))
        except (DecompressionError, Unsupported) as exc:
            return BlockRead(ReadStatus.DECOMPRESS_FAILED, None, notes=[str(exc)])

    def _read_gang(self, bp: BlockPointer, dva: Dva, notes: list[str]) -> list[tuple[str, bytes]]:
        """Reassemble a gang block's physical data from its gang header's children."""
        hdrs = self.read_dva(Dva(dva.vdev, dva.offset, SPA_GANGBLOCKSIZE, False, 0), SPA_GANGBLOCKSIZE)
        if not hdrs:
            return []
        path, hdr = hdrs[0]
        verifier = (dva.vdev, dva.offset >> 9, bp.birth, 0)
        if not checksum.verify_embedded(hdr, verifier):
            notes.append(f"{dva}: gang header checksum invalid")
        bo = "<" if bp.little_endian else ">"
        parts = []
        for i in range(3):
            child = bpmod.parse(hdr, i * 128, bo)
            if child.is_hole:
                continue
            r = self.read(child, verify=True, decompress=False)
            if r.data is None:
                raise CorruptStructure(f"gang member {i} unreadable ({r.status.value})")
            parts.append(r.data[: child.psize])
        return [(path, b"".join(parts)[: bp.psize])]


def u64s(data: bytes, count: int | None = None, bo: str = "<") -> tuple[int, ...]:
    n = len(data) // 8 if count is None else count
    return struct.unpack_from(f"{bo}{n}Q", data)
