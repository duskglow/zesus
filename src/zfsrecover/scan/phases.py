"""Scan phases. Each is idempotent: it clears its own previous partial output, or skips
completed units."""

from __future__ import annotations

import datetime as _dt
import logging

from ..carve.classify import PoolLimits
from ..carve.scanner import CarveTarget, run_carve
from ..map.db import j
from ..zfs.history import read_history
from ..zfs.objset import Objset
from ..zfs.zap import read_zap
from .pipeline import ScanContext, phase

log = logging.getLogger(__name__)


def _ts(t: int | None) -> str:
    return _dt.datetime.fromtimestamp(t).isoformat(sep=" ") if t else "?"


@phase("history")
def phase_history(ctx: ScanContext) -> None:
    """Decode the pool history log from the newest readable MOS. It names destroyed
    datasets with their dsobj, creation and destruction txgs."""
    for pool in ctx.pools:
        pid = ctx.pool_ids[pool.guid]
        with ctx.db.tx():
            ctx.db.execute("DELETE FROM history_records WHERE pool_id=?", (pid,))
            ctx.db.execute("DELETE FROM datasets WHERE pool_id=? AND origin='history_log'", (pid,))
        hist = None
        for ub in pool.uberblocks:
            try:
                mos = Objset(pool.reader, ub.rootbp, "MOS")
                objdir = read_zap(mos.object(1))
                if "history" not in objdir:
                    log.info("pool %s has no history object", pool.name)
                    break
                hist = read_history(mos.object(objdir["history"]))
                log.info("pool history read via uberblock txg %d: %d records (%d lost to wrap)",
                         ub.txg, len(hist.records), hist.records_lost)
                break
            except Exception as exc:
                log.warning("history via txg %d failed: %s", ub.txg, exc)
        if hist is None:
            continue
        for e in hist.errors:
            log.warning("history: %s", e)
        created: dict[int, dict] = {}
        with ctx.db.tx():
            for r in hist.records:
                f = r.fields
                kind = "command" if "history command" in f else "internal" if "internal_name" in f \
                    else "ioctl" if "ioctl" in f else "other"
                ctx.db.insert("history_records", {
                    "pool_id": pid, "log_offset": r.offset, "time": r.time, "txg": r.txg, "kind": kind,
                    "dsname": r.dsname, "dsobj": r.dsid, "summary": r.summary(), "fields_json": j(f)})
                if r.dsid is None:
                    continue
                ev = r.internal_name
                if ev == "create":
                    created[r.dsid] = {"name": r.dsname, "txg": r.txg, "time": r.time, "props": {}}
                elif ev == "set" and r.dsid in created and r.internal_str:
                    k, _, v = r.internal_str.partition("=")
                    created[r.dsid]["props"][k] = v
                elif ev == "destroy":
                    c = created.pop(r.dsid, {"name": r.dsname, "txg": None, "time": None, "props": {}})
                    ctx.db.insert("datasets", {
                        "pool_id": pid, "dsobj": r.dsid, "name": c["name"] or r.dsname, "kind": "unknown",
                        "origin": "history_log", "status": "destroyed", "creation_txg": c["txg"],
                        "creation_time": c["time"], "destroy_txg": r.txg, "destroy_time": r.time,
                        "props_json": j(c["props"]), "notes": r.internal_str})
                    log.info("history: dataset %s (dsobj %d) created txg %s (%s), DESTROYED txg %s (%s)",
                             c["name"] or r.dsname, r.dsid, c["txg"], _ts(c["time"]), r.txg, _ts(r.time))
            ctx.db.event("info", "history", f"{len(hist.records)} history records decoded",
                         lost=hist.records_lost)


@phase("carve")
def phase_carve(ctx: ScanContext) -> bool:
    """Scan every allocatable byte of each present vdev for ZFS metadata blocks."""
    opts = ctx.options
    partial = opts.get("carve_start") is not None or opts.get("carve_end") is not None
    for pool in ctx.pools:
        pid = ctx.pool_ids[pool.guid]
        for im in pool.vdev_images:
            vt = (im.config or {}).get("vdev_tree", {})
            top = vt.get("id", 0)
            asize = vt.get("asize") or (im.source.size - (4 << 20) - (512 << 10))
            lim = PoolLimits(n_vdevs=max(1, pool.config.get("vdev_children", 1)),
                             vdev_asize=max(t.asize for t in pool.vdevs.top.values()),
                             max_txg=pool.max_txg)
            target = CarveTarget(pool_id=pid, vdev_top=top, source=im.source, base_phys=im.base_offset,
                                 asize=asize, ashift=vt.get("ashift", 9))
            last = [0.0]

            def progress(pos: int, end: int, st, rate: float) -> None:
                import time
                t = time.monotonic()
                if t - last[0] >= opts.get("progress_interval", 30):
                    last[0] = t
                    eta = (end - pos) / rate if rate else 0
                    log.info("carve vdev %d: %5.1f%%  %.0f MB/s  ETA %dm  (chunk: %d candidates, %d hits %s)",
                             top, 100 * pos / end, rate / 1e6, eta // 60, st.candidates, st.hits, st.by_kind)

            ok = run_carve(ctx.db, target, lim, chunk_size=opts.get("chunk_size", 64 << 20),
                           start=opts.get("carve_start") or 0, end=opts.get("carve_end"),
                           stop=ctx.stop, progress=progress)
            if not ok:
                return False
            counts = dict(ctx.db.execute("SELECT kind, count(*) FROM carved WHERE pool_id=? GROUP BY kind",
                                         (pid,)).fetchall())
            log.info("carving of pool %s vdev %d complete: %s", pool.name, top, counts)
            ctx.db.event("info", "carve", f"carving complete for vdev {top}", counts=counts)
    return not partial
