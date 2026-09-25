"""Fast "how much of this byte range is recoverable?" queries against a volume's
run-length coverage table."""

from __future__ import annotations

import numpy as np

from ..map.codes import LOST, BlockStatus
from ..map.db import MapDB

_LOST_NAMES = {s.name.lower() for s in LOST} | {"unknown_lost"}


class Coverage:
    def __init__(self, db: MapDB, volume_id: int) -> None:
        rows = db.execute("SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=? "
                          "ORDER BY first_blkid", (volume_id,)).fetchall()
        v = db.execute("SELECT volblocksize, n_blocks FROM volumes WHERE id=?", (volume_id,)).fetchone()
        self.bs = v["volblocksize"]
        self.n_blocks = v["n_blocks"]
        self.starts = np.array([r["first_blkid"] for r in rows], dtype=np.int64)
        self.ends = np.array([r["first_blkid"] + r["count"] for r in rows], dtype=np.int64)
        self.lost = np.array([r["status"] in _LOST_NAMES for r in rows], dtype=bool)
        self.status = [r["status"] for r in rows]
        # cumulative lost blocks, for O(log n) range sums
        lens = np.where(self.lost, self.ends - self.starts, 0)
        self.cum = np.concatenate([[0], np.cumsum(lens)])

    def lost_blocks(self, first: int, last_excl: int) -> int:
        """Lost blocks in block range [first, last_excl).

        Coverage runs tile [0, n_blocks) contiguously (see ``summarize``). Anything past
        the end of the mapped range counts as lost.
        """
        total = 0
        limit = int(self.ends[-1]) if len(self.ends) else 0
        if last_excl > limit:
            total += last_excl - max(first, limit)
            last_excl = limit
        if last_excl <= first:
            return total
        i = int(np.searchsorted(self.ends, first, side="right"))
        j = int(np.searchsorted(self.starts, last_excl, side="left"))
        total += int(self.cum[j] - self.cum[i])
        # trim the parts of the first/last runs that fall outside the range
        if self.lost[i] and self.starts[i] < first:
            total -= first - int(self.starts[i])
        if self.lost[j - 1] and self.ends[j - 1] > last_excl:
            total -= int(self.ends[j - 1]) - last_excl
        return total

    def lost_bytes(self, offset: int, length: int) -> int:
        """Approximate lost bytes in [offset, offset+length) at block granularity."""
        if length <= 0:
            return 0
        bs = self.bs
        first, last = offset // bs, (offset + length - 1) // bs + 1
        lost = self.lost_blocks(first, last)
        if not lost:
            return 0
        return min(length, lost * bs)

    def status_at(self, blkid: int) -> str:
        i = int(np.searchsorted(self.ends, blkid, side="right"))
        if i < len(self.starts) and self.starts[i] <= blkid:
            return self.status[i]
        return BlockStatus.NO_METADATA.name.lower()
