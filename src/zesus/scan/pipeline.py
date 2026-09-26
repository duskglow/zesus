"""Scan pipeline: runs phases in order, each one resumable and recorded in the map."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..io.source import RawSource
from ..map.db import MapDB, j, now
from ..zfs.pool import Pool, open_pools

log = logging.getLogger(__name__)


@dataclass
class ScanContext:
    db: MapDB
    source: RawSource
    pools: list[Pool] = field(default_factory=list)
    pool_ids: dict[int, int] = field(default_factory=dict)      # pool guid -> db id
    options: dict = field(default_factory=dict)
    stop: object | None = None


Phase = Callable[[ScanContext], None]
PHASES: dict[str, Phase] = {}
ORDER: list[str] = []


def phase(name: str) -> Callable[[Phase], Phase]:
    def deco(f: Phase) -> Phase:
        PHASES[name] = f
        ORDER.append(name)
        return f
    return deco


def record_source(ctx: ScanContext) -> None:
    ident = ctx.source.identity
    prev = ctx.db.execute("SELECT path,size,mtime_ns FROM sources ORDER BY id DESC LIMIT 1").fetchone()
    if prev and (prev["size"] != ident.size or (prev["mtime_ns"] and ident.mtime_ns
                                                and prev["mtime_ns"] != ident.mtime_ns)):
        raise RuntimeError(
            f"this map was created from a different source (size/mtime differ: {prev['path']}). "
            "Use a new map file.")
    ctx.db.insert("sources", {"path": ctx.source.name, "size": ident.size, "mtime_ns": ident.mtime_ns,
                              "is_device": int(ident.is_device), "opened_at": now()})
    ctx.db.commit()


def ensure_pools(ctx: ScanContext) -> None:
    """Open pools (cheap: labels only) and make sure each has a row in the map."""
    if ctx.pools:
        return
    ctx.pools = open_pools(ctx.source)
    if not ctx.pools:
        log.error("no ZFS pool labels found in %s", ctx.source.name)
    for p in ctx.pools:
        row = ctx.db.execute("SELECT id FROM pools WHERE guid=?", (str(p.guid),)).fetchone()
        if row:
            ctx.pool_ids[p.guid] = row[0]
            continue
        from ..zfs.constants import POOL_STATE
        c = p.config
        pid = ctx.db.insert("pools", {
            "name": p.name, "guid": str(p.guid), "state": POOL_STATE.get(c.get("state"), str(c.get("state"))),
            "version": c.get("version"), "hostname": c.get("hostname"), "txg": c.get("txg"),
            "ashift": p.ashift, "config_json": j(c)})
        ctx.pool_ids[p.guid] = pid
        for im in p.vdev_images:
            vt = (im.config or {}).get("vdev_tree", {})
            vid = ctx.db.insert("vdevs", {
                "pool_id": pid, "top_id": vt.get("id", 0), "guid": str(im.guid), "type": vt.get("type"),
                "path": vt.get("path"), "base_phys": im.base_offset, "size": im.source.size,
                "asize": vt.get("asize"), "ashift": vt.get("ashift"), "present": 1})
            for lb in im.labels:
                ctx.db.insert("labels", {"vdev_id": vid, "idx": lb.index, "phys": im.base_offset + lb.offset,
                                         "config_ok": int(lb.config_ok), "n_uberblocks": len(lb.uberblocks),
                                         "error": lb.error})
        for u in p.uberblocks:
            ctx.db.insert("uberblocks", {"pool_id": pid, "txg": u.txg, "timestamp": u.timestamp,
                                         "guid_sum": str(u.guid_sum), "checksum_ok": int(u.checksum_ok),
                                         "rootbp": u.rootbp.raw, "phys": None, "mos_ok": None})
        ctx.db.event("info", "pool", f"found pool {p.name}", guid=str(p.guid), txg=p.max_txg,
                     uberblocks=len(p.uberblocks))
    ctx.db.commit()


def run(ctx: ScanContext, phases: list[str]) -> None:
    record_source(ctx)
    ensure_pools(ctx)
    for name in phases:
        if name not in PHASES:
            raise ValueError(f"unknown phase {name!r}; known: {', '.join(ORDER)}")
        if ctx.db.is_done(name) and not ctx.options.get("redo", set()) & {name}:
            log.info("phase %s already complete; skipping", name)
            continue
        log.info("=== phase: %s", name)
        t0 = time.monotonic()
        # Re-running a phase invalidates everything derived from it. (Carving is additive
        # and resumes by chunk, so it only invalidates phases after it.)
        with ctx.db.tx():
            for later in ORDER[ORDER.index(name) + 1:]:
                if later != "carve":
                    ctx.db.reset_phase(later)
        complete = PHASES[name](ctx)
        if ctx.stop is not None and ctx.stop.event.is_set():
            log.warning("stopped during phase %s", name)
            return
        if complete is False:
            log.info("phase %s did not complete (partial range or errors); it will resume on the next run", name)
            continue
        with ctx.db.tx():
            ctx.db.mark(name, "*")
        log.info("phase %s finished in %.1fs", name, time.monotonic() - t0)
    ctx.source.verify_unchanged()


# Register phases (import for side effects)
from . import phases as _phases  # noqa: E402,F401

