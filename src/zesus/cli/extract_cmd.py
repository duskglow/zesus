"""`zesus extract` and `zesus ls`."""

from __future__ import annotations

import argparse
import logging
import stat
import sys
import time
from pathlib import Path

from .. import log as logsetup

log = logging.getLogger("zesus")


def add_extract_parser(sub) -> None:
    e = sub.add_parser("extract", help="extract volumes, partitions or files using a map",
                       description="Extract data using a map. The source image/device is read-only and "
                                   "every block is re-verified. Gaps are reported, never hidden.")
    e.add_argument("map")
    e.add_argument("source", help="the same image/device the map was built from")
    e.add_argument("-o", "--out", help="output directory (required unless --list)")
    e.add_argument("--list", action="store_true", help="list what can be extracted and exit")
    e.add_argument("--volume", action="append", default=[], metavar="ID|NAME",
                   help="extract a whole volume as a raw image (repeatable)")
    e.add_argument("--range", metavar="START:LEN",
                   help="with --volume: only this byte range (e.g. 0x100000:64M)")
    e.add_argument("--no-hash", action="store_true", help="skip SHA-256 of outputs (faster for huge images)")
    e.add_argument("--partition", action="append", default=[], metavar="VOL:IDX",
                   help="extract one partition of a volume as an image (repeatable)")
    e.add_argument("--fs-image", type=int, action="append", default=[], metavar="FSID",
                   help="extract a filesystem (inside a volume) as a raw image of exactly its own size")
    e.add_argument("--fs", type=int, action="append", default=[], metavar="FSID",
                   help="extract files from a filesystem (see --list)")
    e.add_argument("--path", action="append", default=[], metavar="GLOB",
                   help="with --fs: only paths matching (fnmatch glob or directory prefix)")
    e.add_argument("--status", default="full,partial,none",
                   help="with --fs: file statuses to extract (default: all)")
    e.add_argument("--no-deleted", action="store_true", help="with --fs: skip deleted/orphaned entries")
    e.add_argument("--fill", choices=["zero", "pattern"], default="zero",
                   help="how to fill unrecoverable gaps (default zero; 'pattern' writes a visible marker)")
    e.add_argument("--include-unverified", action="store_true",
                   help="DANGEROUS: write blocks whose checksum fails instead of leaving a gap")
    e.add_argument("--partial-suffix", default=None, help="append this suffix to incomplete outputs")
    e.add_argument("--force", action="store_true", help="overwrite existing outputs")
    e.add_argument("--no-sparse", action="store_true", help="do not create sparse output files")
    e.set_defaults(func=cmd_extract)

    sd = sub.add_parser("send", help="send recovered files back where they belong (rsync + original metadata)",
                        description="Stage recovered files locally, rsync them to DEST (user@host:/path or a "
                                    "local directory), then restore original names, symlinks, modes, times and "
                                    "(with --sudo) owners on the destination. Existing files are never "
                                    "overwritten unless --overwrite; partially recovered files are held back "
                                    "unless --include-partial.")
    sd.add_argument("map")
    sd.add_argument("source", help="the same image/device the map was built from")
    sd.add_argument("--fs", type=int, required=True, metavar="FSID", help="filesystem id (see extract --list)")
    sd.add_argument("--path", action="append", default=[], metavar="GLOB",
                    help="only these paths (glob or directory prefix); default: everything")
    sd.add_argument("--to", required=True, metavar="DEST", help="user@host:/path, or a local directory")
    sd.add_argument("--staging", help="local staging directory (default: <map dir>/staging)")
    sd.add_argument("--ssh", default="", help='extra ssh options, e.g. "-p 2222 -i ~/.ssh/id_ed25519"')
    sd.add_argument("--sudo", action="store_true",
                    help="run rsync and the restore script as root on the destination (sudo -n); restores owners")
    sd.add_argument("--overwrite", action="store_true", help="replace files that already exist at the destination")
    sd.add_argument("--include-partial", action="store_true",
                    help="also send partially recovered files (their gaps are zero-filled)")
    sd.add_argument("--no-metadata", action="store_true", help="skip restoring names/modes/owners/times")
    sd.add_argument("-n", "--dry-run", action="store_true", help="show what would be sent; change nothing")
    sd.set_defaults(func=cmd_send)

    ls = sub.add_parser("ls", help="browse the file inventory in a map")
    ls.add_argument("map")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("--fs", type=int, help="filesystem id (default: the only/first one)")
    ls.add_argument("-l", "--long", action="store_true")
    ls.add_argument("-R", "--recursive", action="store_true")
    ls.add_argument("--status", help="only entries with these statuses (comma-separated)")
    ls.set_defaults(func=cmd_ls)


def _resolve_volume(db, spec: str) -> int:
    if spec.isdigit():
        return int(spec)
    r = db.execute("SELECT id FROM volumes WHERE name=?", (spec,)).fetchone()
    if not r:
        raise SystemExit(f"no volume named {spec!r} (see --list)")
    return r[0]


def print_list(db, out=sys.stdout) -> None:
    from .report import _gib
    w = out.write
    for v in db.execute("SELECT * FROM volumes"):
        w(f"volume {v['id']}: {v['name']}  {_gib(v['volsize'])}  status={v['status']}\n")
        for p in db.execute("SELECT * FROM partitions WHERE volume_id=? ORDER BY idx", (v["id"],)):
            w(f"  partition {v['id']}:{p['idx']}  {p['type_name']} {p['name'] or ''}  start={p['start']:#x} "
              f"{_gib(p['length'])}  {100 * (p['coverage'] or 0):.3f}% recoverable\n")
        for f in db.execute("SELECT * FROM filesystems WHERE volume_id=?", (v["id"],)):
            counts = db.execute("SELECT status, count(*) FROM fs_entries WHERE fs_id=? AND type='file' "
                                "GROUP BY status", (f["id"],)).fetchall()
            c = ", ".join(f"{s}={n}" for s, n in counts)
            w(f"  filesystem {f['id']}: {f['fstype']} at {f['start']:#x} uuid={f['uuid']} label={f['label']!r} "
              f"state={f['state']}{'  files: ' + c if c else ''}\n")
    for f in db.execute("SELECT f.*, d.status AS ds_status FROM filesystems f JOIN datasets d ON d.id=f.dataset_id"):
        counts = db.execute("SELECT status, count(*) FROM fs_entries WHERE fs_id=? AND type='file' "
                            "GROUP BY status", (f["id"],)).fetchall()
        c = ", ".join(f"{s}={n}" for s, n in counts)
        w(f"filesystem {f['id']}: ZFS dataset {f['label']} ({f['ds_status']}) state={f['state']}"
          f"{'  files: ' + c if c else '  (no files)'}\n")


def cmd_extract(args: argparse.Namespace) -> int:
    from ..extract.engine import Extractor, Options
    from ..io.source import RawSource
    from ..map.db import MapDB
    from ..zfs.pool import open_pools

    logsetup.setup(args.verbose, args.log)
    db = MapDB(args.map, readonly=True)
    if args.list:
        print_list(db)
        return 0
    if not args.out:
        raise SystemExit("--out is required")
    if not (args.volume or args.partition or args.fs or args.fs_image):
        raise SystemExit("nothing selected: use --volume, --partition or --fs (see --list)")
    src = RawSource(args.source)
    prev = db.execute("SELECT size, mtime_ns FROM sources ORDER BY id DESC LIMIT 1").fetchone()
    if prev and prev["size"] != src.size:
        raise SystemExit(f"source size {src.size} does not match the map's source ({prev['size']})")
    pools = open_pools(src)
    if not pools:
        raise SystemExit("no ZFS pool found in source")
    pool = pools[0]
    ex = Extractor(db, pool, Options(out_dir=Path(args.out), fill=args.fill,
                                     include_unverified=args.include_unverified, force=args.force,
                                     partial_suffix=args.partial_suffix, sparse=not args.no_sparse,
                                     hash_outputs=not args.no_hash))
    if args.include_unverified:
        log.warning("--include-unverified: blocks failing their checksum WILL be written; outputs may be corrupt")
    t0 = time.monotonic()
    for spec in args.volume:
        vid = _resolve_volume(db, spec)
        v = db.execute("SELECT * FROM volumes WHERE id=?", (vid,)).fetchone()
        name = v["name"].replace("/", "_")
        if args.range:
            from .main import _size
            a, _, n = args.range.partition(":")
            start, length = _size(a), _size(n)
            ex.extract_volume_range(vid, start, min(length, v["volsize"] - start), f"{name}@{start:#x}.img",
                                    "volume-range", f"{v['name']} [{start:#x}+{length:#x}]")
        else:
            ex.extract_volume_range(vid, 0, v["volsize"], name + ".img", "volume", v["name"])
    for spec in args.partition:
        vs, _, idx = spec.partition(":")
        vid = _resolve_volume(db, vs)
        p = db.execute("SELECT * FROM partitions WHERE volume_id=? AND idx=?", (vid, int(idx))).fetchone()
        if not p:
            raise SystemExit(f"no partition {spec}")
        vname = db.execute("SELECT name FROM volumes WHERE id=?", (vid,)).fetchone()[0]
        ex.extract_volume_range(vid, p["start"], p["length"], f"{vname.replace('/', '_')}-part{idx}.img",
                                "partition", f"{vname} partition {idx}")
    for fid in args.fs_image:
        import json as _json
        f = db.execute("SELECT * FROM filesystems WHERE id=?", (fid,)).fetchone()
        if not f or f["volume_id"] is None:
            raise SystemExit(f"filesystem {fid} is not inside a volume (ZFS datasets: use --fs)")
        size = (_json.loads(f["info_json"] or "{}").get("details", {}).get("blocks") or 0) * (f["block_size"] or 0)
        length = min(f["length"], size) if size else f["length"]
        vname = db.execute("SELECT name FROM volumes WHERE id=?", (f["volume_id"],)).fetchone()[0]
        ex.extract_volume_range(f["volume_id"], f["start"], length,
                                f"{vname.replace('/', '_')}-fs{fid}-{f['fstype']}.img", "filesystem",
                                f"{vname} {f['fstype']} at {f['start']:#x}")
    for fid in args.fs:
        ex.extract_files(fid, patterns=args.path or None, statuses=set(args.status.split(",")),
                         include_deleted=not args.no_deleted)
    man = ex.write_manifest({"map": str(Path(args.map).resolve()), "source": args.source})
    full = sum(1 for r in ex.records if r.status == "full")
    part = sum(1 for r in ex.records if r.status == "partial")
    none = sum(1 for r in ex.records if r.status == "none")
    log.info("done in %.0fs: %d outputs (%d full, %d partial, %d unrecoverable). Manifest: %s",
             time.monotonic() - t0, len(ex.records), full, part, none, man)
    src.verify_unchanged()
    return 0 if not (part or none) else 2


def cmd_send(args: argparse.Namespace) -> int:
    import shlex

    from ..extract.send import SendOptions, send
    from ..io.source import RawSource
    from ..map.db import MapDB
    from ..zfs.pool import open_pools

    logsetup.setup(args.verbose, args.log)
    db = MapDB(args.map, readonly=True)
    src = RawSource(args.source)
    pool = open_pools(src)[0]
    staging = Path(args.staging) if args.staging else Path(args.map).resolve().parent / "staging"
    opts = SendOptions(dest=args.to, staging=staging, ssh_args=shlex.split(args.ssh), overwrite=args.overwrite,
                       include_partial=args.include_partial, dry_run=args.dry_run, sudo=args.sudo,
                       metadata=not args.no_metadata)
    res = send(db, pool, args.fs, args.path or None, opts)
    log.info("%s: staged %d, sent %d, restored metadata on %d; held back %d partial, %d unrecoverable%s",
             "DRY RUN" if res.dry_run else "done", res.staged, res.sent, res.restored, res.held_back_partial,
             res.unrecoverable, f"; restore script: {res.script}" if res.script else "")
    for e in res.errors:
        log.error("%s", e)
    src.verify_unchanged()
    return 1 if res.errors else 0


def cmd_ls(args: argparse.Namespace) -> int:
    from ..map.db import MapDB
    logsetup.setup(args.verbose)
    db = MapDB(args.map, readonly=True)
    fid = args.fs or (db.execute("SELECT min(id) FROM filesystems WHERE plugin IS NOT NULL").fetchone()[0])
    if fid is None:
        raise SystemExit("no inventoried filesystem in this map")
    path = "/" + args.path.strip("/")
    q = "SELECT * FROM fs_entries WHERE fs_id=? AND "
    if args.recursive:
        rows = db.execute(q + "(path = ? OR path LIKE ?) ORDER BY path",
                          (fid, path, path.rstrip("/") + "/%")).fetchall()
    else:
        me = db.execute(q + "path=? ORDER BY id LIMIT 1", (fid, path)).fetchone()
        if me is None:
            raise SystemExit(f"{path}: not found")
        if me["type"] == "dir":
            rows = db.execute(q + "parent_inode=? AND deleted=0 AND path LIKE ? ORDER BY name",
                              (fid, me["inode"], path.rstrip("/") + "/%")).fetchall()
        else:
            rows = [me]
    want = set(args.status.split(",")) if args.status else None
    for r in rows:
        if want and r["status"] not in want:
            continue
        name = r["path"] if args.recursive else (r["name"] or r["path"])
        mark = {"full": " ", "partial": "~", "none": "!", "n/a": " "}.get(r["status"] or "", "?")
        if args.long:
            mode = stat.filemode(r["mode"]) if r["mode"] else "??????????"
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])) if r["mtime"] else "?"
            print(f"{mark} {mode} {r['uid'] or 0:>5} {r['gid'] or 0:>5} {r['size'] or 0:>14} {ts} "
                  f"{r['status'] or '':7} {name}{' -> ' + r['link_target'] if r['link_target'] else ''}")
        else:
            print(f"{mark} {name}{'/' if r['type'] == 'dir' else ''}")
    return 0
