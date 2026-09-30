"""Re-read only the lost blocks, with different evidence.

When a member disk that was missing during the scan arrives later (or a better image of a
failing disk), there is no need to scan again. Every lost block is retried through the
full reader with the new member set: direct reads, parity rebuilds, combinatorial repair,
then older tree versions. Blocks that come back are marked recovered, and the file
inventory is rebuilt so file statuses reflect them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .map.codes import BlockStatus

log = logging.getLogger(__name__)

RETRY = (BlockStatus.CKSUM_MISMATCH, BlockStatus.ZEROED, BlockStatus.UNREADABLE)


@dataclass
class RecheckResult:
    volumes: list[dict] = field(default_factory=list)

    @property
    def recovered(self) -> int:
        return sum(v["recovered"] for v in self.volumes)


def recheck(ctx, inventory: bool = True) -> RecheckResult:
    """*ctx* is a ScanContext whose source is the new evidence."""
    from .carve.verify import Verifier
    from .scan import pipeline

    pipeline.record_source(ctx)
    pipeline.ensure_pools(ctx)
    if not ctx.pools:
        raise RuntimeError("no pool found in the evidence")
    pool = ctx.pools[0]
    res = RecheckResult()
    for vid, name in ctx.db.execute("SELECT id, name FROM volumes").fetchall():
        v = Verifier(pool, ctx.db, vid, stop=ctx.stop, progress=ctx.progress)
        n = max((sp.first + sp.count for sp in v.spans.values()), default=0)
        status = np.zeros(n, dtype=np.uint8)
        for sp in v.spans.values():
            status[sp.first:sp.first + sp.count] = np.frombuffer(bytes(sp.status), dtype=np.uint8)
        before = int(np.isin(status, np.array(RETRY, dtype=np.uint8)).sum())
        if not before:
            res.volumes.append({"volume": name, "lost_before": 0, "recovered": 0, "lost_after": 0})
            continue
        log.info("recheck %s: retrying %d lost blocks with %d evidence file(s)", name, before,
                 len(pool.vdev_images))
        v.fallback(status, retry=RETRY)
        after = int(np.isin(status, np.array(RETRY, dtype=np.uint8)).sum())
        v.write_back(status)
        res.volumes.append({"volume": name, "lost_before": before, "recovered": before - after,
                            "lost_after": after})
        log.info("recheck %s: %d of %d lost blocks recovered", name, before - after, before)
        ctx.db.event("info", "recheck", f"{name}: {before - after} of {before} lost blocks recovered",
                     volume_id=vid)
    if inventory and res.recovered:
        log.info("rebuilding the file inventory so file statuses include the recovered blocks")
        ctx.db.reset_phase("contents")
        ctx.db.commit()
        pipeline.run(ctx, ["contents"])
    return res
