"""ZAP (ZFS Attribute Processor) decoding: micro-ZAPs and fat ZAPs.

Fat ZAPs are decoded by walking *every* leaf block of the object instead of following the
pointer table. That way a damaged pointer table does not hide entries, and entries in
blocks orphaned by corruption still turn up.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from ..errors import CorruptStructure
from .constants import (
    MZAP_ENT_LEN,
    ZAP_CHUNK_ARRAY,
    ZAP_CHUNK_ENTRY,
    ZAP_LEAF_ARRAY_BYTES,
    ZAP_LEAF_CHUNKSIZE,
    ZAP_LEAF_MAGIC,
    ZAP_MAGIC,
    ZBT_HEADER,
    ZBT_LEAF,
    ZBT_MICRO,
)
from .dnode import ObjectReader
from .reader import ReadStatus

log = logging.getLogger(__name__)
ZAP_FLAG_UINT64_KEY = 1 << 1
CHAIN_END = 0xFFFF


def block_kind(block: bytes) -> str | None:
    if len(block) < 8:
        return None
    t = struct.unpack_from("<Q", block, 0)[0]
    return {ZBT_MICRO: "micro", ZBT_HEADER: "fat", ZBT_LEAF: "leaf"}.get(t)


def parse_micro(block: bytes) -> dict[str, int]:
    out: dict[str, int] = {}
    for off in range(MZAP_ENT_LEN, len(block) - MZAP_ENT_LEN + 1, MZAP_ENT_LEN):
        value, _cd = struct.unpack_from("<QI", block, off)
        name_raw = block[off + 14: off + MZAP_ENT_LEN]
        if name_raw[0] == 0:
            continue
        name = name_raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        out[name] = value
    return out


def parse_leaf(block: bytes, uint64_key: bool = False) -> dict[Any, Any]:
    """Decode every entry chunk in one fat-ZAP leaf block."""
    bt, _pad, _prefix, magic = struct.unpack_from("<QQQI", block, 0)
    if bt != ZBT_LEAF or magic != ZAP_LEAF_MAGIC:
        raise CorruptStructure("not a ZAP leaf")
    bs = len(block)
    shift = bs.bit_length() - 1
    hash_entries = 1 << (shift - 5)
    chunk_base = 48 + 2 * hash_entries
    nchunks = (bs - 2 * hash_entries) // ZAP_LEAF_CHUNKSIZE - 2

    def chunk(i: int) -> int:
        return chunk_base + i * ZAP_LEAF_CHUNKSIZE

    def read_array(first: int, nbytes: int) -> bytes:
        out = bytearray()
        c, guard = first, 0
        while c != CHAIN_END and len(out) < nbytes and guard <= nchunks:
            if c >= nchunks:
                raise CorruptStructure("ZAP array chunk out of range")
            o = chunk(c)
            if block[o] != ZAP_CHUNK_ARRAY:
                raise CorruptStructure("ZAP array chain hits non-array chunk")
            out += block[o + 1:o + 1 + ZAP_LEAF_ARRAY_BYTES]
            c = struct.unpack_from("<H", block, o + 22)[0]
            guard += 1
        return bytes(out[:nbytes])

    out: dict[Any, Any] = {}
    for i in range(nchunks):
        o = chunk(i)
        if block[o] != ZAP_CHUNK_ENTRY:
            continue
        (_t, vintlen, _next, name_chunk, name_numints, value_chunk,
         value_numints, _cd, _hash) = struct.unpack_from("<BBHHHHHIQ", block, o)
        try:
            if uint64_key:
                raw = read_array(name_chunk, 8 * name_numints)
                name: Any = struct.unpack(f">{name_numints}Q", raw)
                name = name[0] if len(name) == 1 else name
            else:
                name = read_array(name_chunk, name_numints).rstrip(b"\0").decode("utf-8", "replace")
            raw = read_array(value_chunk, vintlen * value_numints)
        except CorruptStructure as exc:
            log.debug("skipping damaged ZAP entry in chunk %d: %s", i, exc)
            continue
        out[name] = _decode_value(raw, vintlen, value_numints)
    return out


def _decode_value(raw: bytes, intlen: int, n: int) -> Any:
    fmt = {1: "B", 2: "H", 4: "I", 8: "Q"}.get(intlen)
    if fmt is None or len(raw) < intlen * n:
        return raw
    if intlen == 1:
        # byte arrays are usually strings (props) or packed data
        s = raw[:n]
        if s.endswith(b"\0") and all(32 <= c < 127 or c in (9, 10) for c in s[:-1]):
            return s[:-1].decode()
        return s
    vals = struct.unpack(f">{n}{fmt}", raw[: intlen * n])
    return vals[0] if n == 1 else list(vals)


def read_zap(obj: ObjectReader) -> dict[Any, Any]:
    """Read every entry of a ZAP object. Unreadable blocks are logged and skipped."""
    first = obj.read_block(0)
    if first.data is None:
        raise CorruptStructure(f"ZAP block 0 unreadable ({first.status.value})")
    kind = block_kind(first.data)
    if kind == "micro":
        return parse_micro(first.data)
    if kind != "fat":
        raise CorruptStructure(f"not a ZAP (block type {struct.unpack_from('<Q', first.data)[0]:#x})")
    magic, = struct.unpack_from("<Q", first.data, 8)
    if magic != ZAP_MAGIC:
        log.warning("fat ZAP header has wrong magic %#x; decoding leaves anyway", magic)
    # zap_phys_t: type, magic, ptrtbl[5], freeblk, num_leafs, num_entries, salt, normflags, flags
    zap_flags = struct.unpack_from("<Q", first.data, 8 * 12)[0] if len(first.data) >= 104 else 0
    uint64_key = bool(zap_flags & ZAP_FLAG_UINT64_KEY)
    out: dict[Any, Any] = {}
    for blkid in range(1, obj.dn.maxblkid + 1):
        r = obj.read_block(blkid)
        if r.status == ReadStatus.HOLE or r.data is None:
            if r.status != ReadStatus.HOLE:
                log.warning("ZAP leaf block %d unreadable: %s", blkid, r.status.value)
            continue
        if block_kind(r.data) != "leaf":
            continue
        try:
            out.update(parse_leaf(r.data, uint64_key))
        except CorruptStructure as exc:
            log.warning("ZAP leaf block %d damaged: %s", blkid, exc)
    return out
