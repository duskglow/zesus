"""Classify a candidate block's (decompressed) contents as a ZFS metadata structure.

The checks are deliberately strict. A false positive costs a wasted verification later,
but a false negative can lose a tree. So every structure must be *entirely* plausible
(for example, every non-empty slot of an indirect block must be a valid block pointer).
Nothing found here is trusted on its own: reconstruction re-verifies every block through
its parent's checksum.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..zfs.constants import (
    DMU_OT_NUMTYPES,
    DNODE_SIZE,
    ZBT_HEADER,
    ZBT_LEAF,
    ZBT_MICRO,
    DmuType,
)

_M63 = np.uint64((1 << 63) - 1)


@dataclass
class PoolLimits:
    """Pool facts used to reject implausible block pointers."""
    n_vdevs: int
    vdev_asize: int                     # largest top-level vdev asize
    max_txg: int
    little_endian: bool = True


@dataclass
class Classified:
    kind: str                           # indirect | dnodes | objset | zap
    lsize: int
    child_type: int | None = None
    child_level: int | None = None
    n_children: int = 0
    n_holes: int = 0
    min_birth: int | None = None
    max_birth: int | None = None
    os_type: int | None = None
    info: dict[str, Any] = field(default_factory=dict)


def _valid_type(t: np.ndarray) -> np.ndarray:
    newtype = (t & 0x80) != 0
    return (t < DMU_OT_NUMTYPES) | (newtype & ((t & 0x1F) <= 9))


def bp_rows_check(words: np.ndarray, lim: PoolLimits) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized plausibility of block pointers.

    *words* is an (n, 16) uint64 array. Returns boolean arrays (valid, hole, zero).
    Rows that are none of these are invalid.
    """
    zero = ~words.any(axis=1)
    prop = words[:, 6]
    embedded = ((prop >> np.uint64(39)) & np.uint64(1)).astype(bool)
    w0, w1 = words[:, 0], words[:, 1]
    dva_empty = (w0 == 0) & (w1 == 0)
    cksum_zero = ~words[:, 12:16].any(axis=1)
    birth = words[:, 10]
    phys_birth = words[:, 9]
    typ = (prop >> np.uint64(48)) & np.uint64(0xFF)
    level = (prop >> np.uint64(56)) & np.uint64(0x1F)
    comp = (prop >> np.uint64(32)) & np.uint64(0x7F)
    le = (prop >> np.uint64(63)).astype(bool)
    max_txg = np.uint64(lim.max_txg)

    # hole (possibly with hole_birth: type/level/lsize/birth recorded, no DVAs)
    hole = ~zero & ~embedded & dva_empty & ~words[:, 2:6].any(axis=1) & cksum_zero & (birth <= max_txg)

    asize = (w0 & np.uint64(0xFFFFFF)) << np.uint64(9)
    vdev = w0 >> np.uint64(32)
    offset = (w1 & _M63) << np.uint64(9)
    lsize = ((prop & np.uint64(0xFFFF)) + np.uint64(1)) << np.uint64(9)
    psize = (((prop >> np.uint64(16)) & np.uint64(0xFFFF)) + np.uint64(1)) << np.uint64(9)
    ck = (prop >> np.uint64(40)) & np.uint64(0xFF)
    normal = (
        ~embedded & ~dva_empty
        & (asize > 0) & (asize <= np.uint64(64 << 20)) & (asize >= psize)
        & (vdev < np.uint64(lim.n_vdevs)) & (offset < np.uint64(lim.vdev_asize))
        & (lsize <= np.uint64(16 << 20)) & (psize <= lsize)
        & (comp >= 2) & (comp <= 16)
        & (ck >= 1) & (ck <= 14) & (ck != 2) & (ck != 3) & (ck != 4)
        & _valid_type(typ) & (level < 10)
        & (birth > 0) & (birth <= max_txg) & (phys_birth <= max_txg)
        & (words[:, 7] == 0) & (words[:, 8] == 0)
        & ~cksum_zero
        & (le == lim.little_endian)
    )
    # embedded data BPs: 112-byte payload in the DVA/cksum words
    e_lsize = (prop & np.uint64(0x1FFFFFF)) + np.uint64(1)
    e_psize = ((prop >> np.uint64(25)) & np.uint64(0x7F)) + np.uint64(1)
    e_type = (prop >> np.uint64(40)) & np.uint64(0xFF)
    emb = (embedded & (e_type == 0) & (e_psize <= 112) & (e_lsize <= np.uint64(128 << 10))
           & (comp >= 2) & (comp <= 16) & _valid_type(typ) & (level == 0)
           & (birth > 0) & (birth <= max_txg))
    return normal | emb, hole, zero


def classify_indirect(d: bytes, lim: PoolLimits) -> Classified | None:
    n = len(d)
    if n < 1024 or n & (n - 1) or n > (128 << 10):
        return None
    words = np.frombuffer(d, dtype="<u8").reshape(-1, 16)
    valid, hole, zero = bp_rows_check(words, lim)
    if not valid.any() or (valid | hole | zero).sum() != len(words):
        return None
    prop = words[valid, 6]
    levels = (prop >> np.uint64(56)) & np.uint64(0x1F)
    types = (prop >> np.uint64(48)) & np.uint64(0xFF)
    if (levels != levels[0]).any():
        return None                      # all children of an indirect share one level
    vals, counts = np.unique(types, return_counts=True)
    if len(vals) > 1:
        return None                      # ...and one object type
    births = words[valid, 10]
    # position of the last used slot tells us how much of the span is populated
    used = np.nonzero(valid | hole)[0]
    return Classified(kind="indirect", lsize=n, child_type=int(vals[0]), child_level=int(levels[0]),
                      n_children=int(valid.sum()), n_holes=int(hole.sum()),
                      min_birth=int(births.min()), max_birth=int(births.max()),
                      info={"first_slot": int(used[0]), "last_slot": int(used[-1])})


def _dnode_ok(b: bytes, off: int) -> tuple[bool, int, int, int]:
    """Return (plausible, type, slots, bonustype) for the dnode at *off*."""
    t, ibs, nlv, nbp, btype, ck, comp, flags = b[off:off + 8]
    if t == 0:
        return True, 0, 1, 0            # free slot (ZFS zeroes freed dnodes)
    extra = b[off + 12]
    if not ((t < DMU_OT_NUMTYPES) or (t & 0x80 and (t & 0x1F) <= 9)):
        return False, t, 1, 0
    if not (1 <= nbp <= 3 + 4 * extra and 1 <= nlv <= 9 and 9 <= ibs <= 17):
        return False, t, 1, 0
    if any(b[off + 13:off + 16]) or any(b[off + 32:off + 64]):
        return False, t, 1, 0
    if ck > 14 or comp > 16 or flags & ~0x0F:
        return False, t, 1, 0
    dbss = struct.unpack_from("<H", b, off + 8)[0]
    if dbss == 0:
        return False, t, 1, 0
    return True, t, 1 + extra, btype


def classify_dnodes(d: bytes, lim: PoolLimits) -> Classified | None:
    n = len(d)
    if n < DNODE_SIZE or n % DNODE_SIZE or n > (128 << 10):
        return None
    types: dict[int, int] = {}
    bonus: dict[int, int] = {}
    slots: list[tuple[int, int, int]] = []
    i = 0
    count = n // DNODE_SIZE
    bps_valid_birth: list[int] = []
    while i < count:
        ok, t, nslots, btype = _dnode_ok(d, i * DNODE_SIZE)
        if not ok:
            return None
        if t:
            types[t] = types.get(t, 0) + 1
            bonus[btype] = bonus.get(btype, 0) + 1
            slots.append((i, t, btype))
            nbp = d[i * DNODE_SIZE + 3]
            words = np.frombuffer(d, dtype="<u8", count=16 * nbp, offset=i * DNODE_SIZE + 64).reshape(-1, 16)
            valid, hole, zero = bp_rows_check(words, lim)
            if (valid | hole | zero).sum() != len(words):
                return None
            if valid.any():
                bps_valid_birth.extend(int(x) for x in words[valid, 10])
        i += max(1, nslots)
    if not types:
        return None
    info: dict[str, Any] = {"types": {str(k): v for k, v in sorted(types.items())},
                            "bonus": {str(k): v for k, v in sorted(bonus.items())}}
    # Record slots that matter for reconstruction (zvol data, datasets, dirs, ZPL master).
    interesting = {DmuType.ZVOL, DmuType.ZVOL_PROP, DmuType.MASTER_NODE, DmuType.DSL_DATASET,
                   DmuType.DSL_DIR, DmuType.OBJECT_DIRECTORY, DmuType.SPA_HISTORY}
    info["slots"] = [[s, t, b] for s, t, b in slots if t in interesting or b in interesting][:64]
    return Classified(kind="dnodes", lsize=n, n_children=len(slots),
                      min_birth=min(bps_valid_birth) if bps_valid_birth else None,
                      max_birth=max(bps_valid_birth) if bps_valid_birth else None, info=info)


def classify_objset(d: bytes, lim: PoolLimits) -> Classified | None:
    if len(d) not in (1024, 2048, 4096):
        return None
    ok, t, _, _ = _dnode_ok(d, 0)
    if not ok or t != DmuType.DNODE:
        return None
    os_type = struct.unpack_from("<Q", d, 704)[0]
    if os_type not in (1, 2, 3):
        return None
    nbp = d[3]
    words = np.frombuffer(d, dtype="<u8", count=16 * nbp, offset=64).reshape(-1, 16)
    valid, hole, zero = bp_rows_check(words, lim)
    if (valid | hole | zero).sum() != len(words) or not valid.any():
        return None
    births = words[valid, 10]
    maxblkid = struct.unpack_from("<Q", d, 16)[0]
    return Classified(kind="objset", lsize=len(d), os_type=int(os_type),
                      min_birth=int(births.min()), max_birth=int(births.max()),
                      n_children=int(valid.sum()),
                      info={"meta_nlevels": d[2], "meta_maxblkid": maxblkid,
                            "meta_dblksz": struct.unpack_from("<H", d, 8)[0] << 9})


def classify_zap(d: bytes, lim: PoolLimits) -> Classified | None:
    if len(d) < 512:
        return None
    t = struct.unpack_from("<Q", d, 0)[0]
    if t == ZBT_MICRO:
        # micro ZAP entries: value u64, cd u32, pad u16, name[50]; require sane names
        names = []
        for off in range(64, min(len(d), 64 * 64), 64):
            nm = d[off + 14:off + 64].split(b"\0", 1)[0]
            if nm:
                if not all(32 <= c < 127 for c in nm):
                    return None
                names.append(nm.decode())
        if not names:
            return None
        return Classified(kind="zap", lsize=len(d), info={"zap": "micro", "names": names[:16]})
    if t == ZBT_HEADER and struct.unpack_from("<Q", d, 8)[0] == 0x2F52AB2AB:
        return Classified(kind="zap", lsize=len(d), info={"zap": "fat"})
    if t == ZBT_LEAF and struct.unpack_from("<I", d, 24)[0] == 0x2AB1EAF:
        return Classified(kind="zap", lsize=len(d), info={"zap": "leaf"})
    return None


def classify(d: bytes, lim: PoolLimits) -> Classified | None:
    for f in (classify_objset, classify_indirect, classify_dnodes, classify_zap):
        c = f(d, lim)
        if c is not None:
            return c
    return None
