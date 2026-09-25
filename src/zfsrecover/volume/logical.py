"""A reconstructed volume presented as a random-access, gap-aware block device.

Reads go map → L1 candidate → L0 pointer → source image, and every block is verified
against its checksum on the way. Regions that cannot be recovered come back as zeros,
and are *always* reported in the returned gap list. Callers (filesystem plugins, the
extractor) decide what to do with gaps. Nothing is silently filled.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from ..map.codes import DESCRIPTIONS, BlockStatus
from ..map.db import MapDB
from ..zfs import blkptr
from ..zfs.blkptr import BlockPointer
from ..zfs.pool import Pool
from ..zfs.reader import ReadStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gap:
    offset: int            # byte offset within the volume
    length: int
    status: BlockStatus

    @property
    def reason(self) -> str:
        return DESCRIPTIONS[self.status]


class LogicalVolume:
    def __init__(self, pool: Pool, db: MapDB, volume_id: int, *, verify: bool = True,
                 cache_blocks: int = 2048) -> None:
        self.pool, self.db, self.volume_id = pool, db, volume_id
        v = db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        if v is None:
            raise KeyError(f"no volume {volume_id} in map")
        self.name = v["name"]
        self.block_size = v["volblocksize"]
        self.n_blocks = v["n_blocks"]
        self.size = v["volsize"] or self.n_blocks * self.block_size
        self.verify = verify
        self._spans: OrderedDict[int, tuple] = OrderedDict()
        self._l1: OrderedDict[tuple[int, int], np.ndarray | None] = OrderedDict()
        self._data: OrderedDict[int, tuple[bytes | None, BlockStatus]] = OrderedDict()
        self._cache_blocks = cache_blocks
        row = db.execute("SELECT count FROM volume_spans WHERE volume_id=? LIMIT 1", (volume_id,)).fetchone()
        self.epb = 1 << 10
        if row:
            span0 = db.execute("SELECT max(count) FROM volume_spans WHERE volume_id=?", (volume_id,)).fetchone()[0]
            self.epb = max(self.epb, span0)
        self.stats = {s.name: 0 for s in BlockStatus}

    # ------------------------------------------------------------------ block level
    def _span(self, span: int):
        if span in self._spans:
            self._spans.move_to_end(span)
            return self._spans[span]
        r = self.db.execute("SELECT first_blkid, count, candidates, choice, status FROM volume_spans "
                            "WHERE volume_id=? AND span=?", (self.volume_id, span)).fetchone()
        if r is None:
            val = None
        else:
            blob = r["candidates"]
            cands = [blkptr.parse(blob, i * 129) for i in range(len(blob) // 129)]
            val = (r["first_blkid"], r["count"], cands, r["choice"], r["status"])
        self._spans[span] = val
        if len(self._spans) > 256:
            self._spans.popitem(last=False)
        return val

    def _l1_words(self, span: int, ci: int, bp: BlockPointer) -> np.ndarray | None:
        key = (span, ci)
        if key in self._l1:
            self._l1.move_to_end(key)
            return self._l1[key]
        if bp.is_hole:
            w = None
        else:
            rd = self.pool.reader.read(bp)
            w = np.frombuffer(rd.data, dtype="<u8").reshape(-1, 16) if rd.data else None
        self._l1[key] = w
        if len(self._l1) > 512:
            self._l1.popitem(last=False)
        return w

    def block_status(self, blkid: int) -> BlockStatus:
        sp = self._span(blkid // self.epb)
        if sp is None:
            return BlockStatus.NO_METADATA
        first, count, _, _, status = sp
        i = blkid - first
        return BlockStatus(status[i]) if 0 <= i < count else BlockStatus.NO_METADATA

    def block_pointer(self, blkid: int) -> BlockPointer | None:
        sp = self._span(blkid // self.epb)
        if sp is None:
            return None
        first, count, cands, choice, status = sp
        i = blkid - first
        if not (0 <= i < count) or choice[i] == 255:
            return None
        ci = choice[i]
        words = self._l1_words(blkid // self.epb, ci, cands[ci])
        if cands[ci].is_hole:
            return cands[ci]
        if words is None:
            return None
        return blkptr.parse(words[i].tobytes())

    def read_block(self, blkid: int) -> tuple[bytes | None, BlockStatus]:
        """Return (data or None, status) for one logical block."""
        if blkid in self._data:
            self._data.move_to_end(blkid)
            return self._data[blkid]
        st = self.block_status(blkid)
        data: bytes | None = None
        if st in (BlockStatus.HOLE, BlockStatus.DISCARDED):
            data = b"\0" * self.block_size
        elif st in (BlockStatus.OK, BlockStatus.OK_STALE, BlockStatus.EMBEDDED, BlockStatus.UNKNOWN):
            bp = self.block_pointer(blkid)
            if bp is None:
                st = BlockStatus.NO_METADATA
            else:
                rd = self.pool.reader.read(bp)
                if rd.status in (ReadStatus.OK, ReadStatus.HOLE) and rd.data is not None:
                    data = rd.data
                    if st == BlockStatus.UNKNOWN:
                        st = BlockStatus.OK
                elif rd.status == ReadStatus.UNVERIFIED and not self.verify and rd.data is not None:
                    data = rd.data
                else:
                    # the map said OK but the data no longer verifies: report, never guess
                    st = {ReadStatus.CHECKSUM_MISMATCH: BlockStatus.CKSUM_MISMATCH,
                          ReadStatus.ZEROED: BlockStatus.ZEROED,
                          ReadStatus.DECOMPRESS_FAILED: BlockStatus.DECOMPRESS_FAIL}.get(rd.status, BlockStatus.UNREADABLE)
                    log.warning("%s block %d: expected recoverable, but read gave %s", self.name, blkid, rd.status.value)
        if data is not None and len(data) < self.block_size:
            data = data + b"\0" * (self.block_size - len(data))
        self.stats[st.name] += 1
        self._data[blkid] = (data, st)
        if len(self._data) > self._cache_blocks:
            self._data.popitem(last=False)
        return data, st

    # ------------------------------------------------------------------ byte level
    def read(self, offset: int, length: int) -> tuple[bytes, list[Gap]]:
        """Read bytes. Unrecoverable ranges are zero-filled AND returned as gaps."""
        if offset >= self.size or length <= 0:
            return b"", []
        length = min(length, self.size - offset)
        bs = self.block_size
        out = bytearray()
        gaps: list[Gap] = []
        pos, end = offset, offset + length
        while pos < end:
            blkid, inner = divmod(pos, bs)
            take = min(bs - inner, end - pos)
            data, st = self.read_block(blkid)
            if data is None:
                out += b"\0" * take
                if gaps and gaps[-1].offset + gaps[-1].length == pos and gaps[-1].status == st:
                    gaps[-1] = Gap(gaps[-1].offset, gaps[-1].length + take, st)
                else:
                    gaps.append(Gap(pos, take, st))
            else:
                out += data[inner:inner + take]
            pos += take
        return bytes(out), gaps

    def pread(self, offset: int, length: int) -> bytes:
        """Plain read for plugins that only need bytes. Gaps read as zeros. Use
        :meth:`read` when the caller must know about gaps."""
        return self.read(offset, length)[0]

    def coverage(self, offset: int, length: int) -> float:
        """Fraction of [offset, offset+length) that is recoverable (holes count as recovered)."""
        if length <= 0:
            return 1.0
        bs = self.block_size
        first, last = offset // bs, (offset + length - 1) // bs
        rows = self.db.execute("SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=? "
                               "AND first_blkid <= ? AND first_blkid + count > ?",
                               (self.volume_id, last, first)).fetchall()
        bad = 0
        for r in rows:
            if r["status"] in ("ok", "ok_stale", "embedded", "hole", "discarded", "unknown"):
                continue
            a = max(first, r["first_blkid"])
            b = min(last + 1, r["first_blkid"] + r["count"])
            bad += max(0, b - a)
        return 1.0 - bad / (last - first + 1)
