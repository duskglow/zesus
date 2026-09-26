"""Virtual JBD2 journal replay: build an in-memory overlay, never write anything.

A filesystem captured while mounted (or destroyed while in use) has its newest metadata
in the journal, not yet checkpointed to its home location. This module scans the log
the way ``jbd2`` recovery does:

* pass 1 finds the end of the valid log and collects revoke records;
* pass 2 maps each committed, non-revoked journaled block to its home block number.

The ext4 reader consults the resulting overlay before reading metadata blocks.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

JBD2_MAGIC = 0xC03B3998
DESCRIPTOR, COMMIT, SB_V1, SB_V2, REVOKE = 1, 2, 3, 4, 5
FC_BLOCK = 6

INCOMPAT_REVOKE = 0x1
INCOMPAT_64BIT = 0x2
INCOMPAT_ASYNC_COMMIT = 0x4
INCOMPAT_CSUM_V2 = 0x8
INCOMPAT_CSUM_V3 = 0x10
INCOMPAT_FAST_COMMIT = 0x20

FLAG_ESCAPE = 1
FLAG_SAME_UUID = 2
FLAG_DELETED = 4
FLAG_LAST_TAG = 8


@dataclass
class Overlay:
    #: home fs block -> (journal logical block, escaped)
    blocks: dict[int, tuple[int, bool]] = field(default_factory=dict)
    transactions: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    revoked: int = 0
    clean: bool = True
    notes: list[str] = field(default_factory=list)


def replay(read_jblock: Callable[[int], bytes | None], nblocks: int) -> Overlay:
    """*read_jblock(n)* returns journal logical block n (None if unreadable)."""
    ov = Overlay()
    sb = read_jblock(0)
    if not sb or struct.unpack_from(">I", sb, 0)[0] != JBD2_MAGIC:
        ov.notes.append("journal superblock missing or unreadable")
        return ov
    btype = struct.unpack_from(">I", sb, 4)[0]
    bsize, maxlen, first, seq, start = struct.unpack_from(">5I", sb, 12)
    incompat = struct.unpack_from(">I", sb, 40)[0] if btype == SB_V2 else 0
    maxlen = min(maxlen, nblocks)
    if start == 0:
        ov.notes.append("journal is clean (nothing to replay)")
        return ov
    ov.clean = False
    if incompat & INCOMPAT_CSUM_V3:
        tag_bytes = 16
    else:
        tag_bytes = 12 + (2 if incompat & INCOMPAT_CSUM_V2 else 0)
        if not incompat & INCOMPAT_64BIT:
            tag_bytes -= 4
    has_tail = bool(incompat & (INCOMPAT_CSUM_V2 | INCOMPAT_CSUM_V3))
    is64 = bool(incompat & INCOMPAT_64BIT)

    def wrap(n: int) -> int:
        return first + (n - first) % (maxlen - first) if n >= maxlen else n

    def tags(block: bytes):
        end = len(block) - (4 if has_tail else 0)
        off = 12
        while off + tag_bytes <= end:
            if tag_bytes == 16:
                lo, flags, hi, _ = struct.unpack_from(">IIII", block, off)
            else:
                lo, _ck, flags = struct.unpack_from(">IHH", block, off)
                hi = struct.unpack_from(">I", block, off + 8)[0] if is64 else 0
            off += tag_bytes
            if not flags & FLAG_SAME_UUID:
                off += 16
            yield (hi << 32) | lo, flags
            if flags & FLAG_LAST_TAG:
                break

    # ---- pass 1: find the end of the log, collect revokes
    revokes: dict[int, int] = {}
    pos, cur, txns = start, seq, []
    guard = 0
    while guard < maxlen * 2:
        guard += 1
        blk = read_jblock(pos)
        if not blk or len(blk) < 12:
            break
        magic, bt, s = struct.unpack_from(">III", blk, 0)
        if magic != JBD2_MAGIC or s != cur:
            break
        if bt == DESCRIPTOR:
            n = sum(1 for _ in tags(blk))
            pos = wrap(pos + 1 + n)
            continue
        if bt == REVOKE:
            count = struct.unpack_from(">I", blk, 12)[0]
            rsz = 8 if is64 else 4
            for o in range(16, min(count, len(blk)), rsz):
                b = struct.unpack_from(">Q" if is64 else ">I", blk, o)[0]
                revokes[b] = max(revokes.get(b, -1), cur)
            pos = wrap(pos + 1)
            continue
        if bt == COMMIT:
            txns.append(cur)
            cur += 1
            pos = wrap(pos + 1)
            continue
        break          # unknown block type: end of log
    if not txns:
        ov.notes.append("journal marked dirty but holds no committed transaction")
        return ov

    # ---- pass 2: replay committed transactions in order (later ones win)
    pos, cur = start, seq
    last = txns[-1]
    while cur <= last:
        blk = read_jblock(pos)
        if not blk:
            break
        magic, bt, s = struct.unpack_from(">III", blk, 0)
        if magic != JBD2_MAGIC or s != cur:
            break
        if bt == DESCRIPTOR:
            p = pos
            for home, flags in tags(blk):
                p = wrap(p + 1)
                if revokes.get(home, -1) >= cur:
                    ov.revoked += 1
                    continue
                ov.blocks[home] = (p, bool(flags & FLAG_ESCAPE))
            pos = wrap(p + 1)
        elif bt == COMMIT:
            cur += 1
            pos = wrap(pos + 1)
        else:
            pos = wrap(pos + 1)
    ov.transactions = len(txns)
    ov.first_seq, ov.last_seq = txns[0], txns[-1]
    log.info("journal: %d committed transactions (seq %d..%d), %d blocks overlaid, %d revoked",
             ov.transactions, ov.first_seq, ov.last_seq, len(ov.blocks), ov.revoked)
    return ov


def unescape(data: bytes, escaped: bool) -> bytes:
    if escaped:
        return struct.pack(">I", JBD2_MAGIC) + data[4:]
    return data
