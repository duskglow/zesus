"""Scan phases. Each is idempotent: it clears its own previous partial output, or skips
completed units."""

from __future__ import annotations

import datetime as _dt
import json
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


@phase("datasets")
def phase_datasets(ctx: ScanContext) -> None:
    """Walk the DSL from every uberblock in the ring.

    Datasets reachable from the newest readable uberblock are 'present'. Datasets only
    reachable from older uberblocks have vanished since (destroyed or renamed), but their
    objset roots are still readable, and those roots are prime material for recovery.
    """
    from ..zfs.constants import ZVOL_OBJ, ZVOL_ZAP_OBJ, ObjsetType
    from ..zfs.dsl import Dsl

    for pool in ctx.pools:
        pid = ctx.pool_ids[pool.guid]
        with ctx.db.tx():
            ctx.db.execute("DELETE FROM dataset_roots WHERE dataset_id IN "
                           "(SELECT id FROM datasets WHERE pool_id=? AND origin!='history_log')", (pid,))
            ctx.db.execute("DELETE FROM datasets WHERE pool_id=? AND origin!='history_log'", (pid,))
            ctx.db.execute("UPDATE datasets SET kind='unknown', seen_txg=NULL, objset_bp=NULL, guid=NULL "
                           "WHERE pool_id=?", (pid,))
        newest_ok_txg: int | None = None
        seen: dict[tuple[int, int], dict] = {}          # (dsobj, creation_txg) -> info
        for ub in pool.uberblocks:                      # newest first
            try:
                mos = Objset(pool.reader, ub.rootbp, "MOS")
                infos = list(Dsl(mos, pool.name).walk())
            except Exception as exc:
                log.info("uberblock txg %d: MOS not readable (%s)", ub.txg, exc)
                ctx.db.execute("UPDATE uberblocks SET mos_ok=0 WHERE pool_id=? AND txg=?", (pid, ub.txg))
                continue
            ctx.db.execute("UPDATE uberblocks SET mos_ok=1 WHERE pool_id=? AND txg=?", (pid, ub.txg))
            if newest_ok_txg is None:
                newest_ok_txg = ub.txg
            for di in infos:
                if di.ds is None:
                    log.warning("txg %d: %s: %s", ub.txg, di.name, di.error)
                    continue
                key = (di.dsobj, di.ds.creation_txg)
                info = seen.setdefault(key, {"di": di, "first_seen": ub.txg, "roots": {}})
                info["last_seen"] = ub.txg
                info["roots"].setdefault(di.ds.bp.birth, (ub.txg, di.ds.bp))
        with ctx.db.tx():
            for (dsobj, ctxg), info in seen.items():
                di = info["di"]
                present = info["first_seen"] == newest_ok_txg
                newest_bp = max(info["roots"].values(), key=lambda r: r[1].birth)[1]
                kind, props = "snapshot" if di.is_snapshot else "unknown", dict(di.props)
                try:
                    os_ = Objset(pool.reader, newest_bp, di.name)
                    if not di.is_snapshot:
                        kind = {ObjsetType.ZVOL: "volume", ObjsetType.ZFS: "filesystem"}.get(os_.type, "unknown")
                    if os_.type == ObjsetType.ZVOL:
                        props["volblocksize"] = os_.dnode(ZVOL_OBJ).datablksz
                        props.update(read_zap(os_.object(ZVOL_ZAP_OBJ)))
                except Exception as exc:
                    log.warning("%s: objset at txg %d unreadable: %s", di.name, newest_bp.birth, exc)
                row = ctx.db.execute("SELECT id FROM datasets WHERE pool_id=? AND dsobj=? AND origin='history_log' "
                                     "AND (creation_txg=? OR creation_txg IS NULL)", (pid, dsobj, ctxg)).fetchone()
                fields = {"kind": kind, "guid": str(di.ds.guid), "seen_txg": info["first_seen"],
                          "objset_bp": newest_bp.raw, "creation_txg": ctxg,
                          "creation_time": di.ds.creation_time}
                if row:
                    dsid = row[0]
                    old = ctx.db.execute("SELECT props_json FROM datasets WHERE id=?", (dsid,)).fetchone()[0]
                    merged = {**(json.loads(old) if old else {}), **props}
                    ctx.db.execute("UPDATE datasets SET kind=?, guid=?, seen_txg=?, objset_bp=?, creation_txg=?, "
                                   "creation_time=?, props_json=?, status=? WHERE id=?",
                                   (*fields.values(), j(merged), "present" if present else "destroyed", dsid))
                else:
                    dsid = ctx.db.insert("datasets", {
                        "pool_id": pid, "dsobj": dsobj, "name": di.name,
                        "origin": "live" if present else "historical",
                        "status": "present" if present else "unknown", "props_json": j(props), **fields})
                for birth, (seen_txg, bp) in info["roots"].items():
                    ctx.db.execute("INSERT OR IGNORE INTO dataset_roots(dataset_id,seen_txg,bp_birth,objset_bp) "
                                   "VALUES(?,?,?,?)", (dsid, seen_txg, birth, bp.raw))
                state = "present" if present else f"NOT in newest pool state; last seen at txg {info['first_seen']}"
                log.info("dataset %-40s %-10s dsobj=%d, %d root version(s) (newest txg %d), %s",
                         di.name, kind, dsobj, len(info["roots"]), newest_bp.birth, state)


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

            def progress(pos: int, end: int, st, rate: float, last=last, top=top) -> None:
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


@phase("reconstruct")
def phase_reconstruct(ctx: ScanContext) -> bool:
    """Rebuild the block map of every volume (live, historical or destroyed) by merging
    all surviving generations of its block tree.

    Volumes known from the DSL use their ring roots plus matching carved roots. Carved
    zvol roots that match no known volume are grouped by geometry into *carved volumes*.
    This is how a volume that has completely fallen off the uberblock ring is found. Each
    one is matched to a destroyed dataset from the history log where possible.
    """
    from collections import defaultdict

    from ..carve.reconstruct import VolumeReconstructor

    limit = ctx.options.get("limit_blocks")
    ignore_ring = bool(ctx.options.get("ignore_ring"))
    carve_done = ctx.db.is_done("carve")
    if not carve_done:
        log.warning("carving has not completed: reconstruction will use only the tree versions "
                    "reachable from the uberblock ring (re-run 'reconstruct' after 'carve')")
    if ignore_ring:
        log.warning("--ignore-ring: pretending no uberblock can reach any volume (carved roots only)")
    for pool in ctx.pools:
        pid = ctx.pool_ids[pool.guid]
        with ctx.db.tx():
            for vid, in ctx.db.execute("SELECT v.id FROM volumes v JOIN datasets d ON v.dataset_id=d.id "
                                       "WHERE d.pool_id=?", (pid,)).fetchall():
                _drop_volume(ctx, vid)
            ctx.db.execute("DELETE FROM datasets WHERE pool_id=? AND origin='carved'", (pid,))
        known_geoms: set = set()
        if not ignore_ring:
            for ds in ctx.db.execute("SELECT * FROM datasets WHERE pool_id=? AND kind='volume'", (pid,)).fetchall():
                ds = dict(ds)
                rec = VolumeReconstructor(pool, ctx.db, pid, ds)
                roots = rec.roots_from_rings()
                if not roots:
                    log.warning("volume %s: no ring root readable; it will be handled with carved roots", ds["name"])
                    continue
                geom = roots[0].geometry
                known_geoms.add(geom)
                carved = rec.roots_from_carving(geom)
                _reconstruct_one(ctx, rec, ds, roots + carved, len(carved), limit)

        # ---- carved-only volumes
        probe = VolumeReconstructor(pool, ctx.db, pid, {"id": None, "name": "?", "destroy_txg": None})
        groups: dict[tuple, list] = defaultdict(list)
        for r in probe.roots_from_carving(None):
            if r.geometry not in known_geoms:
                groups[r.geometry].append(r)
        for n, (geom, roots) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
            _nlevels, maxblkid, dblksz, _ = geom
            volsize = next((r.volsize for r in roots if r.volsize), None) or (maxblkid + 1) * dblksz
            lo, hi = min(r.txg for r in roots), max(r.txg for r in roots)
            match = _match_history(ctx, pid, lo, hi, volsize)
            name = match["name"] if match else f"carved-zvol-{n + 1}"
            log.info("carved volume group %d: %d root(s), txg %d..%d, volsize %d, blocksize %d -> %s%s",
                     n + 1, len(roots), lo, hi, volsize, dblksz, name,
                     f" (matched history dsobj {match['dsobj']})" if match else " (no history match)")
            with ctx.db.tx():
                dsid = ctx.db.insert("datasets", {
                    "pool_id": pid, "dsobj": match["dsobj"] if match else None, "name": name, "kind": "volume",
                    "origin": "carved", "status": "destroyed" if match else "unknown",
                    "creation_txg": match["creation_txg"] if match else None,
                    "destroy_txg": match["destroy_txg"] if match else None,
                    "destroy_time": match["destroy_time"] if match else None,
                    "props_json": j({"size": volsize, "volblocksize": dblksz}),
                    "notes": "reconstructed from carved blocks only"
                             + ("; name inferred from pool history by txg window and size" if match else "")})
            ds = dict(ctx.db.execute("SELECT * FROM datasets WHERE id=?", (dsid,)).fetchone())
            rec = VolumeReconstructor(pool, ctx.db, pid, ds)
            _reconstruct_one(ctx, rec, ds, roots, len(roots), limit)
    return carve_done and not limit


def _drop_volume(ctx: ScanContext, vid: int) -> None:
    for fid, in ctx.db.execute("SELECT id FROM filesystems WHERE volume_id=?", (vid,)).fetchall():
        ctx.db.execute("DELETE FROM fs_extents WHERE entry_id IN (SELECT id FROM fs_entries WHERE fs_id=?)", (fid,))
        ctx.db.execute("DELETE FROM fs_entries WHERE fs_id=?", (fid,))
    for t in ("filesystems", "partitions", "volume_spans", "volume_coverage", "volume_roots"):
        ctx.db.execute(f"DELETE FROM {t} WHERE volume_id=?", (vid,))
    ctx.db.execute("DELETE FROM volumes WHERE id=?", (vid,))
    ctx.db.execute("DELETE FROM progress WHERE phase='contents' AND unit=?", (f"volume:{vid}",))


def _match_history(ctx: ScanContext, pid: int, lo: int, hi: int, volsize: int) -> dict | None:
    """Find the destroyed dataset whose lifetime contains [lo, hi]. Prefer one whose
    creation ioctl recorded the same volsize."""
    cands = [dict(r) for r in ctx.db.execute(
        "SELECT * FROM datasets WHERE pool_id=? AND origin='history_log' AND status='destroyed' "
        "AND (creation_txg IS NULL OR creation_txg <= ?) AND destroy_txg > ?", (pid, lo, hi))]
    if not cands:
        return None
    sizes = set()
    for (fields,) in ctx.db.execute("SELECT fields_json FROM history_records WHERE pool_id=? AND kind='ioctl'", (pid,)):
        try:
            vs = json.loads(fields).get("in_nvl", {}).get("props", {}).get("volsize")
            if vs:
                sizes.add(int(vs))
        except Exception:
            continue
    if len(cands) == 1 or volsize in sizes:
        return max(cands, key=lambda c: c["creation_txg"] or 0)
    return None


def _reconstruct_one(ctx: ScanContext, rec, ds: dict, roots: list, n_carved: int, limit) -> None:
    from ..carve.verify import summarize
    log.info("volume %s: %d tree versions (%d from ring, %d carved); newest txg %d",
             ds["name"], len(roots), len(roots) - n_carved, n_carved, max(r.txg for r in roots))
    spans, ref = rec.build(roots, limit_blocks=limit)
    props = json.loads(ds.get("props_json") or "{}")
    volsize = ref.volsize or props.get("size")
    with ctx.db.tx():
        vid = ctx.db.insert("volumes", {
            "dataset_id": ds["id"], "name": ds["name"], "volsize": volsize,
            "volblocksize": ref.dnode.datablksz, "nlevels": ref.dnode.nlevels,
            "root_txg": max(r.txg for r in roots), "n_blocks": rec.maxblkid + 1, "status": "mapped",
            "notes": j({"stats": rec.stats.__dict__, "limit_blocks": limit})})
        for r in roots:
            ctx.db.insert("volume_roots", {"volume_id": vid, "txg": r.txg, "top_bp": r.top_bp.raw,
                                           "provenance": r.provenance, "usable": 1})
        for span, first, count, blob, choice, status, mb in rec.choose(spans, ref):
            ctx.db.execute("INSERT INTO volume_spans(volume_id,span,first_blkid,count,candidates,choice,"
                           "status,max_birth) VALUES(?,?,?,?,?,?,?,?)",
                           (vid, span, first, count, blob, choice, status, mb))
    log.info("volume %s mapped: %s", ds["name"], summarize(ctx.db, vid))


@phase("verify")
def phase_verify(ctx: ScanContext) -> bool:
    """Read every mapped data block in physical order and check its checksum."""
    from ..carve.verify import Verifier
    for vid, name in ctx.db.execute("SELECT id, name FROM volumes").fetchall():
        pool = ctx.pools[0]
        log.info("verifying volume %s", name)
        Verifier(pool, ctx.db, vid, stop=ctx.stop).run()
        if ctx.stop is not None and ctx.stop.event.is_set():
            return False
    return True


@phase("contents")
def phase_contents(ctx: ScanContext) -> bool:
    """Inside every mapped volume: partition tables, filesystems and, where a plugin
    supports it, a full file inventory with per-file recovery status."""
    from ..fs.api import DeviceSlice
    from ..fs.registry import detect as detect_fs
    from ..partitions import detect as detect_parts
    from ..volume.coverage import Coverage
    from ..volume.logical import LogicalVolume

    complete = True
    for vol in ctx.db.execute("SELECT * FROM volumes").fetchall():
        vid = vol["id"]
        pool = ctx.pools[0]
        lv = LogicalVolume(pool, ctx.db, vid)
        cov = Coverage(ctx.db, vid)
        unit = f"volume:{vid}"
        if ctx.db.is_done("contents", unit):
            continue
        with ctx.db.tx():
            fs_ids = [r[0] for r in ctx.db.execute("SELECT id FROM filesystems WHERE volume_id=?", (vid,))]
            for fid in fs_ids:
                ctx.db.execute("DELETE FROM fs_extents WHERE entry_id IN (SELECT id FROM fs_entries WHERE fs_id=?)", (fid,))
                ctx.db.execute("DELETE FROM fs_entries WHERE fs_id=?", (fid,))
            ctx.db.execute("DELETE FROM filesystems WHERE volume_id=?", (vid,))
            ctx.db.execute("DELETE FROM partitions WHERE volume_id=?", (vid,))
            ctx.db.execute("DELETE FROM unrecoverable WHERE scope='file' AND ref_id IN "
                           "(SELECT id FROM fs_entries WHERE fs_id IN (SELECT id FROM filesystems WHERE volume_id=?))",
                           (vid,))
        scheme, parts = detect_parts(lv)
        regions: list[tuple[int, int, int | None]] = []
        with ctx.db.tx():
            if scheme:
                log.info("volume %s: %s partition table, %d partitions", vol["name"], scheme, len(parts))
            for p in parts:
                c = 1.0 - cov.lost_bytes(p.start, p.length) / max(1, p.length)
                pid_ = ctx.db.insert("partitions", {
                    "volume_id": vid, "scheme": p.scheme, "idx": p.index, "start": p.start, "length": p.length,
                    "type_id": p.type_id, "type_name": p.type_name, "name": p.name, "uuid": p.uuid, "coverage": c})
                log.info("  partition %d: %s %r at %#x, %.1f GiB, %.3f%% recoverable", p.index, p.type_name,
                         p.name, p.start, p.length / (1 << 30), 100 * c)
                regions.append((p.start, p.length, pid_))
        if not regions:
            regions = [(0, lv.size, None)]
        for start, length, part_id in regions:
            dev = DeviceSlice(lv, start, length)
            plugin, ident = detect_fs(dev)
            if plugin is None:
                if ident:
                    with ctx.db.tx():
                        ctx.db.insert("filesystems", {
                            "volume_id": vid, "partition_id": part_id, "start": start, "length": length,
                            "fstype": ident.fstype, "plugin": None, "label": ident.label, "uuid": ident.uuid,
                            "block_size": ident.block_size, "state": "identified",
                            "info_json": j({"details": ident.details, "warnings": ident.warnings})})
                    log.info("  %#x: %s (identified by signature; no inventory plugin installed)", start, ident.fstype)
                else:
                    log.info("  %#x: no known filesystem signature", start)
                continue
            try:
                inventory_fs(ctx, vid, part_id, start, length, dev, plugin, cov)
            except Exception as exc:
                log.exception("  %#x: %s inventory failed: %s", start, plugin.name, exc)
                complete = False
        if complete:
            with ctx.db.tx():
                ctx.db.mark("contents", unit)
    if not inventory_zpl_datasets(ctx):
        complete = False
    return complete


def inventory_zpl_datasets(ctx: ScanContext) -> bool:
    """File inventories of ZFS filesystem datasets (live, historical, destroyed-but-reachable),
    plus snapshots when --snapshots is given. Every data block of every file is verified."""
    from ..zfs import blkptr
    from ..zfs.zpl import ZplFilesystem
    kinds = ("filesystem", "snapshot") if ctx.options.get("snapshots") else ("filesystem",)
    ok = True
    for pool in ctx.pools:
        pid = ctx.pool_ids[pool.guid]
        rows = ctx.db.execute(f"SELECT * FROM datasets WHERE pool_id=? AND objset_bp IS NOT NULL AND kind IN "
                              f"({','.join('?' * len(kinds))})", (pid, *kinds)).fetchall()
        for ds in rows:
            unit = f"dataset:{ds['id']}"
            if ctx.db.is_done("contents", unit):
                continue
            with ctx.db.tx():
                for fid, in ctx.db.execute("SELECT id FROM filesystems WHERE dataset_id=?", (ds["id"],)).fetchall():
                    ctx.db.execute("DELETE FROM fs_extents WHERE entry_id IN (SELECT id FROM fs_entries WHERE fs_id=?)",
                                   (fid,))
                    ctx.db.execute("DELETE FROM fs_entries WHERE fs_id=?", (fid,))
                    ctx.db.execute("DELETE FROM unrecoverable WHERE scope='filesystem' AND ref_id=?", (fid,))
                    ctx.db.execute("DELETE FROM filesystems WHERE id=?", (fid,))
            try:
                os_ = Objset(pool.reader, blkptr.parse(ds["objset_bp"]), ds["name"])
                if os_.type != 2:
                    continue
                fs = ZplFilesystem(os_, ds["name"])
            except Exception as exc:
                log.warning("dataset %s: cannot open as a ZFS filesystem: %s", ds["name"], exc)
                continue
            log.info("dataset %s (%s): building file inventory", ds["name"], ds["status"])
            with ctx.db.tx():
                fs_id = ctx.db.insert("filesystems", {
                    "volume_id": None, "dataset_id": ds["id"], "start": 0, "length": 0, "fstype": "zfs",
                    "plugin": "zpl", "label": ds["name"], "uuid": ds["guid"], "block_size": None,
                    "state": "inventorying",
                    "info_json": j({"details": {"zpl_version": fs.master.get("VERSION")}, "warnings": []})})
            counts = {"full": 0, "partial": 0, "none": 0, "n/a": 0}
            cur = ctx.db.conn.cursor()
            n = 0
            try:
                for e in fs.iter_entries():
                    gaps = []
                    if e.type == "file" and e.size:
                        lost, gaps = fs.check_object(e.inode, e.size)
                        status = "full" if not lost else ("none" if lost >= e.size else "partial")
                    elif e.type in ("file", "dir", "symlink"):
                        lost, status = 0, "full"
                    else:
                        lost, status = 0, "n/a"
                    counts[status] += 1
                    cur.execute(
                        "INSERT INTO fs_entries(fs_id,inode,parent_inode,name,path,type,size,mode,uid,gid,atime,mtime,"
                        "ctime,crtime,deleted,status,recoverable_bytes,link_target,extra_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fs_id, e.inode, e.parent_inode, e.name, e.path, e.type, e.size, e.mode, e.uid, e.gid,
                         e.atime, e.mtime, e.ctime, e.crtime, int(e.deleted), status,
                         (e.size or 0) - lost if e.type == "file" else None, e.link_target,
                         j(e.extra) if e.extra else None))
                    eid = cur.lastrowid
                    for g in gaps:
                        cur.execute("INSERT OR REPLACE INTO fs_extents(entry_id,file_offset,length,volume_offset,kind,"
                                    "status) VALUES(?,?,?,?,?,?)",
                                    (eid, g.offset, g.length, None, "data", g.status.name.lower()))
                    n += 1
                    if n % 20000 == 0:
                        ctx.db.commit()
                        log.info("  %s: %d entries %s", ds["name"], n, counts)
                with ctx.db.tx():
                    for where, what in fs.problems()[:10000]:
                        ctx.db.insert("unrecoverable", {"scope": "filesystem", "ref_id": fs_id, "reason": what,
                                                        "detail": where})
                    ctx.db.execute("UPDATE filesystems SET state='inventoried', info_json=? WHERE id=?",
                                   (j({"details": {"zpl_version": fs.master.get("VERSION")}, "counts": counts,
                                       "problems": len(fs.problems())}), fs_id))
                    ctx.db.mark("contents", unit)
                log.info("dataset %s inventory complete: %d entries %s; %d problems", ds["name"], n, counts,
                         len(fs.problems()))
            except Exception as exc:
                ctx.db.commit()
                log.exception("dataset %s: inventory failed: %s", ds["name"], exc)
                ok = False
    return ok


def inventory_fs(ctx: ScanContext, vid: int, part_id, start: int, length: int, dev, plugin, cov) -> None:
    import time as _time

    from ..fs.api import DIR, FILE, SYMLINK
    h = plugin.open(dev)
    info = h.info()
    for w in info.warnings:
        log.warning("  %s: %s", info.fstype, w)
    with ctx.db.tx():
        fs_id = ctx.db.insert("filesystems", {
            "volume_id": vid, "partition_id": part_id, "start": start, "length": length,
            "fstype": info.fstype, "plugin": plugin.name, "label": info.label, "uuid": info.uuid,
            "block_size": info.block_size, "state": "inventorying",
            "info_json": j({"details": info.details, "warnings": info.warnings})})
    log.info("  %#x: %s uuid=%s label=%r: building file inventory", start, info.fstype, info.uuid, info.label)
    counts = {"full": 0, "partial": 0, "none": 0, "n/a": 0}
    n = 0
    t0 = _time.monotonic()
    cur = ctx.db.conn.cursor()
    for e in h.iter_entries():
        ext_rows = []
        total = lost = 0
        if e.type in (FILE, SYMLINK, DIR):
            for x in h.extents(e):
                st = "ok"
                vol_off = None
                if x.kind == "data" and x.dev_offset is not None:
                    vol_off = start + x.dev_offset
                    lb = cov.lost_bytes(vol_off, x.length)
                    total += x.length
                    lost += lb
                    st = "ok" if lb == 0 else ("missing" if lb >= x.length else "partial")
                else:
                    total += x.length
                ext_rows.append((x.file_offset, x.length, vol_off, x.kind, st))
            status = "full" if lost == 0 else ("none" if lost >= total and total else "partial")
        else:
            status = "n/a"
        counts[status] += 1
        cur.execute(
            "INSERT INTO fs_entries(fs_id,inode,parent_inode,name,path,type,size,mode,uid,gid,atime,mtime,ctime,"
            "crtime,deleted,status,recoverable_bytes,link_target,extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fs_id, e.inode, e.parent_inode, e.name, e.path, e.type, e.size, e.mode, e.uid, e.gid, e.atime,
             e.mtime, e.ctime, e.crtime, int(e.deleted), status, total - lost, e.link_target,
             j(e.extra) if e.extra else None))
        eid = cur.lastrowid
        if ext_rows:
            cur.executemany("INSERT OR REPLACE INTO fs_extents(entry_id,file_offset,length,volume_offset,kind,status) "
                            "VALUES(?,?,?,?,?,?)", [(eid, *r) for r in ext_rows])
        if status in ("partial", "none"):
            cur.execute("INSERT INTO unrecoverable(scope,ref_id,start,length,reason,detail) VALUES(?,?,?,?,?,?)",
                        ("file", eid, None, lost, "data blocks lost", e.path))
        n += 1
        if n % 20000 == 0:
            ctx.db.commit()
            log.info("  inventory: %d entries (%.0f/s) %s", n, n / (_time.monotonic() - t0), counts)
    probs = h.problems()
    with ctx.db.tx():
        for where, what in probs[:10000]:
            ctx.db.insert("unrecoverable", {"scope": "filesystem", "ref_id": fs_id, "start": None, "length": None,
                                            "reason": what, "detail": where})
        ctx.db.execute("UPDATE filesystems SET state='inventoried', info_json=? WHERE id=?",
                       (j({"details": info.details, "warnings": info.warnings, "counts": counts,
                           "problems": len(probs)}), fs_id))
    log.info("  %s inventory complete: %d entries: %s; %d metadata problems", info.fstype, n, counts, len(probs))
