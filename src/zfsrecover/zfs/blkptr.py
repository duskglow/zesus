"""blkptr_t (128 bytes): the self-describing block pointer at the heart of ZFS.

Layout, in 64-bit words:

    0-5   dva[3]           (word0: asize | grid | vdev,  word1: offset | gang bit)
    6     blk_prop         (lsize psize comp E cksum type level X D B)
    7-8   blk_pad
    9     physical birth txg
    10    logical birth txg
    11    fill count
    12-15 checksum

Embedded-data BPs (``E`` bit) store up to 112 bytes of payload in every word except
blk_prop (6) and birth (10).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .constants import BLKPTR_SIZE, ChecksumType, Compression, dmu_type_name

_EMBEDDED_WORDS = (0, 1, 2, 3, 4, 5, 7, 8, 9, 11, 12, 13, 14, 15)
BPE_PAYLOAD_SIZE = 112


@dataclass(frozen=True, slots=True)
class Dva:
    vdev: int
    offset: int        # bytes, relative to the start of the vdev's allocatable area
    asize: int         # bytes
    gang: bool
    grid: int

    @property
    def empty(self) -> bool:
        return self.asize == 0 and self.offset == 0 and self.vdev == 0

    def __str__(self) -> str:
        return f"{self.vdev}:{self.offset:x}:{self.asize:x}{'G' if self.gang else ''}"


@dataclass(frozen=True, slots=True)
class BlockPointer:
    dvas: tuple[Dva, Dva, Dva]
    lsize: int
    psize: int
    comp: int
    embedded: bool
    cksum_type: int      # for embedded BPs: etype
    type: int
    level: int
    encrypted: bool
    dedup: bool
    little_endian: bool
    phys_birth: int
    birth: int
    fill: int
    cksum: tuple[int, int, int, int]
    raw: bytes

    # ---------------------------------------------------------------- predicates
    @property
    def is_hole(self) -> bool:
        return not self.embedded and self.dvas[0].empty

    @property
    def valid_dvas(self) -> list[Dva]:
        return [] if self.embedded else [d for d in self.dvas if not d.empty]

    @property
    def is_gang(self) -> bool:
        return any(d.gang for d in self.valid_dvas)

    @property
    def etype(self) -> int:
        return self.cksum_type

    def embedded_payload(self) -> bytes:
        """The (still compressed) payload of an embedded BP, psize bytes long."""
        words = struct.unpack("<16Q" if self.little_endian else ">16Q", self.raw)
        order = "<" if self.little_endian else ">"
        payload = b"".join(struct.pack(order + "Q", words[i]) for i in _EMBEDDED_WORDS)
        return payload[: self.psize]

    def describe(self) -> str:
        if self.is_hole:
            return f"HOLE birth={self.birth}"
        t = dmu_type_name(self.type)
        if self.embedded:
            return (f"EMBEDDED [L{self.level} {t}] etype={self.etype} {_cname(self.comp)} "
                    f"size={self.lsize:x}L/{self.psize:x}P birth={self.birth}")
        dvas = " ".join(f"DVA[{i}]={d}" for i, d in enumerate(self.dvas) if not d.empty)
        return (f"{dvas} [L{self.level} {t}] {_ckname(self.cksum_type)} {_cname(self.comp)} "
                f"{'LE' if self.little_endian else 'BE'} size={self.lsize:x}L/{self.psize:x}P "
                f"birth={self.birth}L/{self.phys_birth or self.birth}P fill={self.fill} "
                f"cksum={':'.join(f'{c:x}' for c in self.cksum)}")


def _cname(c: int) -> str:
    try:
        return Compression(c).name.lower()
    except ValueError:
        return f"comp{c}"


def _ckname(c: int) -> str:
    try:
        return ChecksumType(c).name.lower()
    except ValueError:
        return f"cksum{c}"


def _dva(w0: int, w1: int) -> Dva:
    return Dva(vdev=w0 >> 32, grid=(w0 >> 24) & 0xFF, asize=(w0 & 0xFFFFFF) << 9,
               offset=(w1 & ((1 << 63) - 1)) << 9, gang=bool(w1 >> 63))


def parse(buf: bytes | memoryview, offset: int = 0, byteorder: str = "<") -> BlockPointer:
    raw = bytes(buf[offset:offset + BLKPTR_SIZE])
    w = struct.unpack(byteorder + "16Q", raw)
    prop = w[6]
    embedded = bool((prop >> 39) & 1)
    if embedded:
        lsize = (prop & 0x1FFFFFF) + 1
        psize = ((prop >> 25) & 0x7F) + 1
    else:
        lsize = ((prop & 0xFFFF) + 1) << 9
        psize = (((prop >> 16) & 0xFFFF) + 1) << 9
    return BlockPointer(
        dvas=(_dva(w[0], w[1]), _dva(w[2], w[3]), _dva(w[4], w[5])),
        lsize=lsize, psize=psize,
        comp=(prop >> 32) & 0x7F,
        embedded=embedded,
        cksum_type=(prop >> 40) & 0xFF,
        type=(prop >> 48) & 0xFF,
        level=(prop >> 56) & 0x1F,
        encrypted=bool((prop >> 61) & 1),
        dedup=bool((prop >> 62) & 1),
        little_endian=bool(prop >> 63),
        phys_birth=w[9], birth=w[10], fill=w[11],
        cksum=(w[12], w[13], w[14], w[15]),
        raw=raw,
    )


def parse_array(buf: bytes | memoryview, count: int | None = None, byteorder: str = "<") -> list[BlockPointer]:
    n = len(buf) // BLKPTR_SIZE if count is None else count
    return [parse(buf, i * BLKPTR_SIZE, byteorder) for i in range(n)]


def is_all_zero(raw: bytes) -> bool:
    return not any(raw)
