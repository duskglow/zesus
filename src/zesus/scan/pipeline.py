"""Scan pipeline: runs phases in order, each one resumable and recorded in the map."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..io.source import RawSource
from ..io.sourceset import SourceSet, as_sources
from ..map.db import MapDB, j, now
from ..zfs.pool import Pool, open_pools

log = logging.getLogger(__name__)


@dataclass
class ScanContext:
    db: MapDB
    source: RawSource | SourceSet
    pools: list[Pool] = field(default_factory=list)
    pool_ids: dict[int, int] = field(default_factory=dict)      # pool guid -> db id
    options: dict = field(default_factory=dict)
    stop: object | None = None
    progress: object | None = None                               # zesus.progress.Progress
    source_ids: dict[str, int] = field(default_factory=dict)     # evidence path -> sources.id
    new_sources: list[tuple[int, str]] = field(default_factory=list)


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
    """Record each evidence file, refusing files that changed since the map was made.

    A file seen before under the same path must have the same size and mtime. A new file
    is accepted (for example a member disk imaged later); :func:`ensure_pools` then checks
    that it belongs to a pool already in the map.
    """
    ctx.new_sources = []
    rows = ctx.db.execute("SELECT id,path,size,mtime_ns,role FROM sources ORDER BY id").fetchall()
    first_ever = not rows
    stamp = now()
    members = as_sources(ctx.source)
    if len(members) == 1 and all(r["role"] is None for r in rows):
        # single evidence file: exactly the original rule (compare with the last run)
        src, ident = members[0], members[0].identity
        prev = rows[-1] if rows else None
        if prev and (prev["size"] != ident.size or (prev["mtime_ns"] and ident.mtime_ns
                                                    and prev["mtime_ns"] != ident.mtime_ns)):
            raise RuntimeError(
                f"this map was created from a different source (size/mtime differ: {prev['path']}). "
                "Use a new map file.")
        ctx.source_ids[src.name] = ctx.db.insert("sources", {
            "path": src.name, "size": ident.size, "mtime_ns": ident.mtime_ns,
            "is_device": int(ident.is_device), "opened_at": stamp})
        ctx.db.commit()
        return
    for src in members:
        ident = src.identity
        same_path = [r for r in rows if r["path"] == src.name]
        same_id = [r for r in rows if r["size"] == ident.size
                   and (not r["mtime_ns"] or not ident.mtime_ns or r["mtime_ns"] == ident.mtime_ns)]
        for r in same_path:
            if r["size"] != ident.size or (r["mtime_ns"] and ident.mtime_ns and r["mtime_ns"] != ident.mtime_ns):
                raise RuntimeError(
                    f"{src.name} changed since this map was made (size/mtime differ). Use a new map file.")
        sid = ctx.db.insert("sources", {"path": src.name, "size": ident.size, "mtime_ns": ident.mtime_ns,
                                        "is_device": int(ident.is_device), "opened_at": stamp})
        if not first_ever and not same_path and not same_id:
            ctx.new_sources.append((sid, src.name))
        ctx.source_ids[src.name] = sid
    ctx.db.commit()


def ensure_pools(ctx: ScanContext) -> None:
    """Open pools (cheap: labels only) and make sure each has a row in the map."""
    if ctx.pools:
        return
    ctx.pools = open_pools(ctx.source)
    if not ctx.pools:
        log.error("no ZFS pool labels found in %s", ctx.source.name)
    known_pools = {r[0] for r in ctx.db.execute("SELECT guid FROM pools")}
    for p in ctx.pools:
        _record_members(ctx, p)
    if ctx.new_sources and known_pools:
        found = {str(p.guid) for p in ctx.pools}
        for sid, path in ctx.new_sources:
            owner = next((str(p.guid) for p in ctx.pools for im in p.vdev_images + p.duplicates
                          if im.name == path), None)
            if owner is None or owner not in known_pools:
                raise RuntimeError(f"{path} does not belong to a pool already in this map "
                                   f"(map pools: {sorted(known_pools)}, found: {sorted(found)}). "
                                   "Use a new map file.")
            ctx.db.event("info", "sources", f"new member file added to the map: {path}", source_id=sid)
    for p in ctx.pools:
        row = ctx.db.execute("SELECT id FROM pools WHERE guid=?", (str(p.guid),)).fetchone()
        if row:
            ctx.pool_ids[p.guid] = row[0]
            _add_new_vdev_rows(ctx, p, row[0])
            continue
        from ..zfs.constants import POOL_STATE
        c = p.config
        pid = ctx.db.insert("pools", {
            "name": p.name, "guid": str(p.guid), "state": POOL_STATE.get(c.get("state"), str(c.get("state"))),
            "version": c.get("version"), "hostname": c.get("hostname"), "txg": c.get("txg"),
            "ashift": p.ashift, "config_json": j(c)})
        ctx.pool_ids[p.guid] = pid
        _add_new_vdev_rows(ctx, p, pid)
        for u in p.uberblocks:
            ctx.db.insert("uberblocks", {"pool_id": pid, "txg": u.txg, "timestamp": u.timestamp,
                                         "guid_sum": str(u.guid_sum), "checksum_ok": int(u.checksum_ok),
                                         "rootbp": u.rootbp.raw, "phys": None, "mos_ok": None})
        ctx.db.event("info", "pool", f"found pool {p.name}", guid=str(p.guid), txg=p.max_txg,
                     uberblocks=len(p.uberblocks))
        if len(p.vdev_images) > 1 or any(t.type != "disk" for t in p.vdevs.top.values()):
            from ..zfs.members import assess
            rep = assess(p)
            for line in rep.lines():
                log.info("%s", line)
            ctx.db.event("info" if rep.readable else "error", "members", "; ".join(
                t.verdict for t in rep.tops), report=rep.as_dict())
    ctx.db.commit()


def _record_members(ctx: ScanContext, p: Pool) -> None:
    """Note on each sources row which pool member it holds."""
    for im in p.vdev_images + p.duplicates:
        sid = ctx.source_ids.get(im.name)
        if sid is None:
            continue
        vt = (im.config or {}).get("vdev_tree", {})
        ctx.db.execute("UPDATE sources SET pool_guid=?, member_guid=?, vdev_top=?, child_id=?, role=? WHERE id=?",
                       (str(p.guid), str(im.guid), vt.get("id", 0), im.child_id,
                        "duplicate" if im.duplicate_of is not None else "member", sid))


def _add_new_vdev_rows(ctx: ScanContext, p: Pool, pid: int) -> None:
    """One vdevs row per member present (and its labels), added once per member guid."""
    have = {r[0] for r in ctx.db.execute("SELECT guid FROM vdevs WHERE pool_id=?", (pid,))}
    for im in p.vdev_images:
        if str(im.guid) in have:
            continue
        vt = (im.config or {}).get("vdev_tree", {})
        vid = ctx.db.insert("vdevs", {
            "pool_id": pid, "top_id": vt.get("id", 0), "guid": str(im.guid), "type": vt.get("type"),
            "path": vt.get("path") or _leaf_path(vt, im.guid), "base_phys": im.base_offset,
            "size": im.source.size, "asize": vt.get("asize"), "ashift": vt.get("ashift"), "present": 1,
            "child_id": im.child_id, "source_id": ctx.source_ids.get(im.name),
            "state": "stale" if im.max_txg < p.max_txg else "present"})
        for lb in im.labels:
            ctx.db.insert("labels", {"vdev_id": vid, "idx": lb.index, "phys": im.base_offset + lb.offset,
                                     "config_ok": int(lb.config_ok), "n_uberblocks": len(lb.uberblocks),
                                     "error": lb.error})


def _leaf_path(tree: dict, guid: int | None) -> str | None:
    for ch in tree.get("children") or []:
        if ch.get("guid") == guid:
            return ch.get("path")
    return None


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
        if ctx.progress is not None:
            ctx.progress.begin(name, 0, "steps")
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

