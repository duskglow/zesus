"""Extraction engine: map + source image → output files, with honest gap accounting.

Guarantees:
* the source is only ever read (``RawSource`` + audit-hook guard);
* every block is checksum-verified as it is read; a block that fails is a gap, never
  written as if it were good (unless ``include_unverified``, which is flagged loudly);
* every output gets a gap report (JSON). Images also get a GNU ddrescue mapfile;
* nothing is overwritten unless ``force``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..carve.fastsum import fletcher4_many
from ..carve.verify import add_extents, chosen_blocks, load_spans, windows
from ..io import guard
from ..map.codes import DESCRIPTIONS, RECOVERED, BlockStatus
from ..map.db import MapDB, now
from ..parallel import SETTINGS, ordered_map, prefetch
from ..volume.logical import LogicalVolume
from ..zfs import compress
from ..zfs.checksum import compute
from ..zfs.constants import ChecksumType
from ..zfs.pool import Pool
from .sparse import make_sparse, set_size

log = logging.getLogger(__name__)

GAP_PATTERN = b"<<ZESUS:UNRECOVERABLE>>\n"
WRITING_SUFFIX = ".zesus-writing"      # an output still being written


@dataclass
class Options:
    out_dir: Path
    fill: str = "zero"                  # zero | pattern
    include_unverified: bool = False
    force: bool = False
    partial_suffix: str | None = None   # e.g. ".PARTIAL"
    sparse: bool = True
    hash_outputs: bool = True
    skip_existing: bool = False        # resume: keep files already written with the right size


@dataclass
class GapRecord:
    offset: int
    length: int
    reason: str


@dataclass
class OutputRecord:
    kind: str                            # volume | partition | file | symlink | dir
    path: str
    source: str                          # what it came from (volume name, fs path)
    size: int
    status: str                          # full | partial | none
    gaps: list[GapRecord] = field(default_factory=list)
    sha256: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def lost_bytes(self) -> int:
        return sum(g.length for g in self.gaps)


class Extractor:
    def __init__(self, db: MapDB, pool: Pool, opts: Options, *, progress=None, stop=None) -> None:
        self.db, self.pool, self.opts = db, pool, opts
        self.progress = progress          # zesus.progress.Progress
        self.stop = stop                  # threading.Event (or Stop): checked between windows/files
        self.cancelled = False
        self.records: list[OutputRecord] = []
        self._fs_handles: dict = {}
        self.local_rel: dict[str, Path] = {}          # filesystem path -> path relative to the fs root
        self.last_selection: list = []
        out = Path(opts.out_dir)
        guard.assert_not_protected([out])
        out.mkdir(parents=True, exist_ok=True)
        self.out = out

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _open_out(self, path: Path, size: int):
        """Write *path* under a temporary name, renamed into place only when the block
        finishes. An interrupted run therefore never leaves a full-size but half-written
        file under the real name (outputs are pre-sized, so size alone proves nothing)."""
        guard.assert_not_protected([path])
        if path.exists() and not self.opts.force:
            raise FileExistsError(f"{path} exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + WRITING_SUFFIX)
        guard.assert_not_protected([tmp])
        f = open(tmp, "wb")
        try:
            if self.opts.sparse:
                make_sparse(f)
            set_size(f, size)        # never f.truncate(): on Windows that writes zeros
            yield f
        except BaseException:
            f.close()
            raise
        f.close()
        os.replace(tmp, path)

    def _fill(self, f, offset: int, length: int) -> None:
        if self.opts.fill != "pattern" or length <= 0:
            return            # zeros: leave the sparse region untouched
        pat = GAP_PATTERN
        f.seek(offset)
        chunk = (pat * (1 + (1 << 20) // len(pat)))[: 1 << 20]
        rem = length
        while rem > 0:
            n = min(rem, len(chunk))
            f.write(chunk[:n])
            rem -= n

    # ------------------------------------------------------------------ images
    def extract_volume_range(self, volume_id: int, start: int, length: int, out_name: str,
                             kind: str, source_desc: str) -> OutputRecord:
        """Extract bytes [start, start+length) of a volume as an image file.

        Blocks are read in physical order (the source may be a spinning disk), verified,
        decompressed and written to their logical position.
        """
        v = self.db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        bs = v["volblocksize"]
        end = start + length
        lo, hi = start // bs, -(-end // bs)
        path = self.out / out_name
        rec = OutputRecord(kind=kind, path=str(path), source=source_desc, size=length, status="full")
        spans = load_spans(self.db, volume_id)
        wl = add_extents(self.pool, chosen_blocks(
            self.pool, spans, {BlockStatus.OK, BlockStatus.OK_STALE, BlockStatus.UNKNOWN},
            blk_range=(lo, hi), label="extract", stop=self.stop))
        n = len(wl["blkid"])
        written = np.zeros(hi - lo, dtype=bool)
        total = float(wl["psize"].sum()) if n else 0.0
        lv = LogicalVolume(self.pool, self.db, volume_id)
        log.info("extracting %s → %s: %d data blocks (%.1f GiB on disk)", source_desc, path, n, total / (1 << 30))
        if self.progress:
            self.progress.begin(f"extract {out_name}", total, "bytes")
        with self._open_out(path, length) as f:
            # Pipeline: one thread reads the next window of the physical-order plan while
            # workers verify and decompress earlier ones; blocks are written in plan order.
            reads = prefetch((self._read_window(w) for w in windows(wl)), depth=SETTINGS.io_depth)
            decoded = ordered_map(lambda r: self._decode_window(wl, r), reads,
                                  workers=SETTINGS.workers, ahead=SETTINGS.workers + 1)
            try:
                for sl, results in decoded:
                    if self._stopped():
                        self._cancel(rec)
                        break
                    for k, (data, raw) in enumerate(results):
                        idx = sl.start + k
                        blkid = int(wl["blkid"][idx])
                        if data is None:
                            data = self._fallback_block(lv, wl, idx, raw, rec)
                        if data is None:
                            continue
                        self._write_block(f, blkid, bs, data, start, end)
                        written[blkid - lo] = True
                    if self.progress:
                        self.progress.advance(float(wl["psize"][sl].sum()))
            finally:
                decoded.close()
            if rec.extra.get("cancelled"):
                self._gap_unwritten(rec, written, lo, bs, start, end)
            else:
                self._fill_the_rest(rec, f, volume_id, written, lv, lo, hi, bs, start, end)
            for g in rec.gaps:
                self._fill(f, g.offset, g.length)
        self._finish(rec, path, image=True)
        return rec

    def _fill_the_rest(self, rec, f, volume_id, written, lv, lo, hi, bs, start, end) -> None:
        """Everything the physical pass did not write, driven by the run-length coverage
        table: holes stay sparse, lost runs become gaps in one step, and only
        recoverable-but-unwritten blocks (embedded, fallbacks) are visited one by one."""
        runs = self.db.execute(
            "SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=? AND "
            "first_blkid < ? AND first_blkid + count > ? ORDER BY first_blkid", (volume_id, hi, lo)).fetchall()
        covered_to = lo
        for r in runs:
            a_blk, b_blk = max(lo, r["first_blkid"]), min(hi, r["first_blkid"] + r["count"])
            if a_blk > covered_to:                   # no coverage row at all: no metadata
                self._gap_blocks(rec, covered_to, a_blk, bs, start, end, BlockStatus.NO_METADATA)
            covered_to = max(covered_to, b_blk)
            st = BlockStatus[r["status"].upper()]
            if st in (BlockStatus.HOLE, BlockStatus.DISCARDED):
                continue
            if st in RECOVERED or st == BlockStatus.UNKNOWN:
                todo = np.nonzero(~written[a_blk - lo:b_blk - lo])[0] + a_blk
                for blkid in todo.tolist():
                    data, st2 = lv.read_block(blkid)
                    if data is not None:
                        self._write_block(f, blkid, bs, data, start, end)
                    else:
                        self._gap_blocks(rec, blkid, blkid + 1, bs, start, end, st2)
            else:
                self._gap_blocks(rec, a_blk, b_blk, bs, start, end, st)
        if covered_to < hi:
            # past the tree's highest allocated block the volume reads as zeros: a hole
            tail = min(hi, max(covered_to, tree_end(self.db, volume_id) or hi))
            if tail > covered_to:
                self._gap_blocks(rec, covered_to, tail, bs, start, end, BlockStatus.NO_METADATA)

    def _fallback_block(self, lv, wl, idx, raw, rec) -> bytes | None:
        """Gang blocks, other copies, older versions: go through the full reader."""
        blkid = int(wl["blkid"][idx])
        data, _st = lv.read_block(blkid)
        if data is None and self.opts.include_unverified and raw is not None:
            try:
                data = compress.decompress(int(wl["comp"][idx]), raw, int(wl["lsize"][idx]))
                rec.extra.setdefault("unverified_blocks", []).append(blkid)
                log.warning("block %d written UNVERIFIED (checksum mismatch)", blkid)
            except Exception:
                data = None
        return data

    # ------------------------------------------------------------------ pipeline stages
    def _read_window(self, w):
        """I/O stage: one window of the plan (RAIDZ: all members in parallel)."""
        sl, vdev, w0, w1 = w
        top = self.pool.vdevs.top.get(vdev)
        return sl, (top.read_window(w0, w1) if top is not None and not top.unsupported else None)

    @staticmethod
    def _decode_window(wl, r):
        """CPU stage: verify and decompress each block of a window. Touches no shared state.

        Returns (slice, [(data or None, raw bytes when they failed verification)])."""
        sl, win = r
        psz = wl["psize"][sl]
        buf, offs, avail = b"", np.zeros(len(psz), np.int64), np.zeros(len(psz), bool)
        if win is not None:
            buf, offs, avail = win.gather(wl["offset"][sl], psz)
        ctype = wl["ctype"][sl]
        ok = np.zeros(len(offs), dtype=bool)
        f4 = (ctype == ChecksumType.FLETCHER_4) & ~wl["gang"][sl] & avail
        if f4.any():
            ok[f4] = (fletcher4_many(buf, offs[f4], psz[f4]) == wl["cksum"][sl][f4]).all(axis=1)
        out = []
        for k in range(len(offs)):
            idx = sl.start + k
            o, p = int(offs[k]), int(psz[k])
            raw = buf[o:o + p] if avail[k] else None
            good = bool(ok[k])
            if not f4[k] and raw is not None and not wl["gang"][idx]:
                try:
                    got = compute(int(ctype[k]), raw)
                    good = got is None or tuple(got) == tuple(int(x) for x in wl["cksum"][idx])
                except Exception:
                    good = False
            data = None
            if good and raw is not None:
                try:
                    data = compress.decompress(int(wl["comp"][idx]), raw, int(wl["lsize"][idx]))
                except Exception as exc:
                    log.warning("block %d: decompression failed: %s", int(wl["blkid"][idx]), exc)
            out.append((data, None if good else raw))
        return sl, out

    # ------------------------------------------------------------------ cancellation
    def _stopped(self) -> bool:
        ev = getattr(self.stop, "event", self.stop)
        return bool(ev is not None and ev.is_set())

    def _cancel(self, rec: OutputRecord) -> None:
        rec.extra["cancelled"] = True
        self.cancelled = True
        log.warning("%s: extraction cancelled; unwritten ranges are recorded as gaps", rec.path)

    def _gap_unwritten(self, rec, written, lo, bs, start, end) -> None:
        """After a cancel, every block not yet written is a gap (reason: cancelled)."""
        idx = np.nonzero(~written)[0]
        if not len(idx):
            return
        for run in np.split(idx, np.nonzero(np.diff(idx) != 1)[0] + 1):
            a = max(start, (lo + int(run[0])) * bs)
            b = min(end, (lo + int(run[-1]) + 1) * bs)
            if b > a:
                self._add_gap(rec, a - start, b - a, "extraction cancelled before this range was read")

    @staticmethod
    def _write_block(f, blkid: int, bs: int, data: bytes, start: int, end: int) -> None:
        a = blkid * bs
        s0 = max(a, start)
        e0 = min(a + bs, end)
        if e0 <= s0:
            return
        chunk = data[s0 - a:e0 - a]
        if not any(chunk):
            return          # keep zero blocks sparse
        f.seek(s0 - start)
        f.write(chunk)

    def _gap_blocks(self, rec: OutputRecord, b0: int, b1: int, bs: int, start: int, end: int,
                    st: BlockStatus) -> None:
        a = max(start, b0 * bs)
        b = min(end, b1 * bs)
        if b > a:
            self._add_gap(rec, a - start, b - a, DESCRIPTIONS[st])

    @staticmethod
    def _add_gap(rec: OutputRecord, off: int, length: int, reason: str) -> None:
        if rec.gaps and rec.gaps[-1].offset + rec.gaps[-1].length == off and rec.gaps[-1].reason == reason:
            rec.gaps[-1].length += length
        else:
            rec.gaps.append(GapRecord(off, length, reason))

    def _finish(self, rec: OutputRecord, path: Path, image: bool) -> None:
        lost = rec.lost_bytes
        rec.status = "full" if lost == 0 else ("none" if lost >= rec.size and rec.size else "partial")
        if self.opts.hash_outputs and not rec.extra.get("cancelled"):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    b = f.read(8 << 20)
                    if not b:
                        break
                    h.update(b)
            rec.sha256 = h.hexdigest()
        if rec.gaps:
            gp = Path(str(path) + ".gaps.json")
            gp.write_text(json.dumps({"file": str(path), "size": rec.size, "lost_bytes": lost,
                                      "gaps": [g.__dict__ for g in rec.gaps]}, indent=1), encoding="utf-8")
        if image:
            write_ddrescue_map(Path(str(path) + ".mapfile"), rec.size, rec.gaps)
        if rec.status != "full" and self.opts.partial_suffix:
            newp = Path(str(path) + self.opts.partial_suffix)
            os.replace(path, newp)
            rec.path = str(newp)
        self.records.append(rec)
        log.info("%s: %s (%s lost of %s)", rec.path, rec.status.upper(), _h(lost), _h(rec.size))

    # ------------------------------------------------------------------ files
    def extract_files(self, fs_id: int, patterns: list[str] | None = None, statuses: set[str] | None = None,
                      include_deleted: bool = True, *, entries: list | None = None,
                      local_rel: dict[str, Path] | None = None) -> None:
        """Extract matching entries. *entries* (already selected rows) and *local_rel* (their
        local names) let a caller extract a large selection in batches with names that stay
        consistent across batches."""
        fs = self.db.execute("SELECT * FROM filesystems WHERE id=?", (fs_id,)).fetchone()
        if fs is None:
            raise KeyError(f"no filesystem {fs_id}")
        if fs["dataset_id"]:
            src_reader = self._zpl(fs)
        else:
            src_reader = LogicalVolume(self.pool, self.db, fs["volume_id"])
        root = self.fs_root(fs)
        sel = entries if entries is not None else self.select(fs_id, patterns, statuses, include_deleted)
        self.last_selection = sel
        if local_rel is not None:
            self.local_rel = local_rel
        else:
            namer = LocalNamer()
            for r in sel:                              # path order: deterministic local names
                self.local_rel[r["path"]] = namer.local(r["path"])
        log.info("extracting %d entries from filesystem %d into %s", len(sel), fs_id, root)
        # order files by their first data extent to reduce seeking
        firsts = {r["id"]: (self.db.execute("SELECT min(volume_offset) FROM fs_extents WHERE entry_id=?",
                                            (r["id"],)).fetchone()[0] or 0) for r in sel if r["type"] == "file"}
        dirs_meta = []
        if self.progress:
            self.progress.begin(f"extract files from filesystem {fs_id}",
                                float(sum((r["size"] or 0) for r in sel if r["type"] == "file")), "bytes")
        for r in sorted(sel, key=lambda r: (r["type"] != "dir", firsts.get(r["id"], 0))):
            if self._stopped():
                self.cancelled = True
                log.warning("file extraction cancelled; %d entries were not extracted",
                            sum(1 for x in sel if x["type"] == "file") - sum(1 for o in self.records if o.kind == "file"))
                break
            if self.progress and r["type"] == "file":
                self.progress.advance(float(r["size"] or 0), message=r["path"])
            target = root / self.local_rel[r["path"]]
            try:
                if r["type"] == "dir":
                    target.mkdir(parents=True, exist_ok=True)
                    dirs_meta.append((target, r))
                elif r["type"] == "file":
                    if fs["dataset_id"]:
                        self._extract_zpl_file(src_reader, r, target)
                    else:
                        self._extract_file(src_reader, r, target)
                elif r["type"] == "symlink":
                    self._extract_symlink(r, target)
            except FileExistsError as exc:
                log.warning("%s", exc)
            except Exception as exc:
                log.error("%s: extraction failed: %s", r["path"], exc)
        for target, r in reversed(dirs_meta):
            _set_times(target, r)

    def select(self, fs_id: int, patterns: list[str] | None = None, statuses: set[str] | None = None,
               include_deleted: bool = True) -> list:
        """Entries matching glob/prefix *patterns* and (for files and symlinks) *statuses*."""
        return select_entries(self.db, fs_id, patterns, statuses, include_deleted)

    def fs_root(self, fs) -> Path:
        return self.out / f"fs{fs['id']}-{fs['fstype']}{'-' + safe_name(fs['label']) if fs['label'] else ''}"

    def _already_there(self, r, target: Path) -> bool:
        if self.opts.skip_existing and target.is_file() and target.stat().st_size == (r["size"] or 0):
            self.records.append(OutputRecord(kind="file", path=str(target), source=r["path"], size=r["size"] or 0,
                                             status=r["status"] or "full", extra={"skipped_existing": True}))
            return True
        return False

    def _extract_file(self, lv: LogicalVolume, r, target: Path) -> None:
        if self._already_there(r, target):
            return
        size = r["size"] or 0
        rec = OutputRecord(kind="file", path=str(target), source=r["path"], size=size, status="full",
                           extra={"inode": r["inode"], "mode": r["mode"], "uid": r["uid"], "gid": r["gid"],
                                  "mtime": r["mtime"], "deleted": bool(r["deleted"])})
        exts = self.db.execute("SELECT * FROM fs_extents WHERE entry_id=? ORDER BY file_offset", (r["id"],)).fetchall()
        with self._open_out(target, size) as f:
            for x in exts:
                if x["kind"] in ("sparse", "unwritten"):
                    continue
                if x["kind"] == "inline":
                    data = self._inline_bytes(r)
                    if data is None:
                        self._add_gap(rec, 0, x["length"], "inline data could not be re-read")
                    else:
                        f.seek(0)
                        f.write(data[: x["length"]])
                    continue
                data, gaps = lv.read(x["volume_offset"], x["length"])
                if any(data):
                    f.seek(x["file_offset"])
                    f.write(data)
                for g in gaps:
                    self._add_gap(rec, x["file_offset"] + (g.offset - x["volume_offset"]), g.length, g.reason)
            for g in rec.gaps:
                self._fill(f, g.offset, g.length)
        self._finish(rec, target, image=False)
        _set_times(target, r)

    def _inline_bytes(self, r) -> bytes | None:
        """Inline data lives inside filesystem metadata, not in the map: ask the plugin."""
        from ..fs.api import DeviceSlice, Entry
        from ..fs.registry import filesystem_plugins
        fs = self.db.execute("SELECT * FROM filesystems WHERE id=?", (r["fs_id"],)).fetchone()
        key = fs["id"]
        if key not in self._fs_handles:
            plugin = next((p for p in filesystem_plugins() if p.name == fs["plugin"]), None)
            if plugin is None:
                return None
            lv = LogicalVolume(self.pool, self.db, fs["volume_id"])
            self._fs_handles[key] = plugin.open(DeviceSlice(lv, fs["start"], fs["length"]))
        h = self._fs_handles[key]
        e = Entry(inode=r["inode"], parent_inode=r["parent_inode"], name=r["name"], path=r["path"],
                  type=r["type"], size=r["size"], link_target=r["link_target"])
        for x in h.extents(e):
            if x.kind == "inline":
                return x.inline
        return None

    def _zpl(self, fs):
        from ..zfs import blkptr
        from ..zfs.objset import Objset
        from ..zfs.zpl import ZplFilesystem
        ds = self.db.execute("SELECT * FROM datasets WHERE id=?", (fs["dataset_id"],)).fetchone()
        return ZplFilesystem(Objset(self.pool.reader, blkptr.parse(ds["objset_bp"]), ds["name"]), ds["name"])

    def _extract_zpl_file(self, zfs, r, target: Path) -> None:
        if self._already_there(r, target):
            return
        size = r["size"] or 0
        rec = OutputRecord(kind="file", path=str(target), source=r["path"], size=size, status="full",
                           extra={"object": r["inode"], "mode": r["mode"], "uid": r["uid"], "gid": r["gid"],
                                  "mtime": r["mtime"], "deleted": bool(r["deleted"])})
        chunk = 8 << 20
        with self._open_out(target, size) as f:
            for off in range(0, size, chunk):
                data, gaps = zfs.read_object(r["inode"], off, min(chunk, size - off))
                if any(data):
                    f.seek(off)
                    f.write(data)
                for g in gaps:
                    self._add_gap(rec, g.offset, g.length, DESCRIPTIONS[g.status])
            for g in rec.gaps:
                self._fill(f, g.offset, g.length)
        self._finish(rec, target, image=False)
        _set_times(target, r)

    def _extract_symlink(self, r, target: Path) -> None:
        link = r["link_target"] or ""
        target.parent.mkdir(parents=True, exist_ok=True)
        rec = OutputRecord(kind="symlink", path=str(target), source=r["path"], size=len(link), status="full",
                           extra={"target": link})
        if sys.platform != "win32":
            if target.is_symlink() or target.exists():
                if not self.opts.force:
                    raise FileExistsError(f"{target} exists")
                target.unlink()
            os.symlink(link, target)
        else:
            # Windows symlinks need privileges: store the target in a small stub file
            p = target.with_name(target.name + ".symlink")
            p.write_text(link, encoding="utf-8", errors="surrogateescape")
            rec.path = str(p)
        self.records.append(rec)

    # ------------------------------------------------------------------ manifest
    def write_manifest(self, extra: dict | None = None) -> Path:
        path = self.out / "manifest.json"
        old = []
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8")).get("outputs", [])
            except Exception:
                old = []
        outs = old + [{**r.__dict__, "gaps": [g.__dict__ for g in r.gaps], "lost_bytes": r.lost_bytes}
                      for r in self.records]
        summary = {s: sum(1 for o in outs if o["status"] == s) for s in ("full", "partial", "none")}
        head = {"tool": "zesus", "written_at": now(), "summary": summary}
        if self.cancelled:
            head["cancelled"] = True
        path.write_text(json.dumps({**head, **(extra or {}), "outputs": outs}, indent=1), encoding="utf-8")
        return path


def select_entries(db: MapDB, fs_id: int, patterns: list[str] | None = None, statuses: set[str] | None = None,
                   include_deleted: bool = True) -> list:
    """Entries of a filesystem matching glob/prefix *patterns* and (for files and symlinks)
    *statuses*. A pattern selects an exact path, everything under a directory, or an
    fnmatch glob. Needs only the map."""
    sel = []
    for r in db.execute("SELECT * FROM fs_entries WHERE fs_id=? ORDER BY path", (fs_id,)):
        if not include_deleted and r["deleted"]:
            continue
        if statuses and r["status"] not in statuses and r["type"] in ("file", "symlink"):
            continue
        if patterns and not any(fnmatch.fnmatchcase(r["path"], p) or r["path"].startswith(p.rstrip("/") + "/")
                                or r["path"] == p for p in patterns):
            continue
        sel.append(r)
    return sel


def estimate_files(db: MapDB, fs_id: int, patterns: list[str] | None = None, statuses: set[str] | None = None,
                   include_deleted: bool = True) -> dict:
    """What extracting this selection would write: counts, sizes, and bytes that cannot
    be recovered (they become gaps)."""
    sel = select_entries(db, fs_id, patterns, statuses, include_deleted)
    files = [r for r in sel if r["type"] == "file"]
    size = sum(r["size"] or 0 for r in files)
    rec = sum((r["recoverable_bytes"] if r["recoverable_bytes"] is not None else (r["size"] or 0)) for r in files)
    by = {}
    for r in files:
        b = by.setdefault(r["status"] or "unknown", {"files": 0, "bytes": 0})
        b["files"] += 1
        b["bytes"] += r["size"] or 0
    return {"entries": len(sel), "files": len(files), "dirs": sum(1 for r in sel if r["type"] == "dir"),
            "symlinks": sum(1 for r in sel if r["type"] == "symlink"), "bytes": size,
            "recoverable_bytes": rec, "lost_bytes": max(0, size - rec), "by_status": by}


def tree_end(db: MapDB, volume_id: int) -> int | None:
    """First block id past the highest block the volume's tree ever allocated, or None when
    the map does not record it (older maps: the tail is then treated as unmapped)."""
    r = db.execute("SELECT notes FROM volumes WHERE id=?", (volume_id,)).fetchone()
    try:
        m = json.loads(r[0] or "{}").get("tree_maxblkid") if r else None
    except ValueError:
        m = None
    return None if m is None else int(m) + 1


def estimate_volume_range(db: MapDB, volume_id: int, start: int, length: int) -> dict:
    """Output size, bytes to read, and bytes that will be gaps, from the coverage table."""
    v = db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
    if v is None:
        raise KeyError(volume_id)
    bs = v["volblocksize"]
    lo, hi = start // bs, -(-(start + length) // bs)
    counts: dict[str, int] = {}
    for r in db.execute("SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=? AND "
                        "first_blkid < ? AND first_blkid + count > ?", (volume_id, hi, lo)):
        n = min(hi, r["first_blkid"] + r["count"]) - max(lo, r["first_blkid"])
        counts[r["status"]] = counts.get(r["status"], 0) + n
    covered = sum(counts.values())
    uncovered = max(0, (hi - lo) - covered)
    te = tree_end(db, volume_id)
    never = max(0, hi - max(lo, te)) if te is not None else 0     # past the tree's last block
    never = min(never, uncovered)
    counts["hole"] = counts.get("hole", 0) + never
    counts["no_metadata"] = counts.get("no_metadata", 0) + uncovered - never
    good = {"ok", "ok_stale", "embedded", "unknown"}
    sparse = {"hole", "discarded"}
    return {"bytes": length, "blocks": hi - lo, "block_size": bs, "by_status": counts,
            "read_bytes": sum(n for s, n in counts.items() if s in good) * bs,
            "sparse_bytes": sum(n for s, n in counts.items() if s in sparse) * bs,
            "lost_bytes": sum(n for s, n in counts.items() if s not in good | sparse) * bs}


def write_ddrescue_map(path: Path, size: int, gaps: Iterable[GapRecord]) -> None:
    lines = ["# Mapfile. Created by zesus", "# current_pos  current_status  current_pass",
             "0x00000000     +               1", "#      pos        size  status"]
    pos = 0
    for g in sorted(gaps, key=lambda g: g.offset):
        if g.offset > pos:
            lines.append(f"0x{pos:08X}  0x{g.offset - pos:08X}  +")
        lines.append(f"0x{g.offset:08X}  0x{g.length:08X}  -")
        pos = g.offset + g.length
    if pos < size:
        lines.append(f"0x{pos:08X}  0x{size - pos:08X}  +")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


_WIN_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """Make one path component safe for the local filesystem, reversibly (%XX escapes)."""
    if sys.platform == "win32":
        name = _WIN_BAD.sub(lambda m: f"%{ord(m.group()):02X}", name)
        if name.split(".")[0].upper() in _WIN_RESERVED or name.endswith((" ", ".")):
            name = name + "%"
    return name.replace("/", "%2F") or "%"


class LocalNamer:
    """Map filesystem paths to local paths that are safe *and unique* on the output
    filesystem. Names are escaped with safe_name(). On case-insensitive platforms
    (Windows, macOS), names that differ only in case get a '%~N' suffix instead of
    silently overwriting each other."""

    def __init__(self, case_insensitive: bool | None = None) -> None:
        self.ci = sys.platform in ("win32", "darwin") if case_insensitive is None else case_insensitive
        self._map: dict[str, Path] = {"/": Path(".")}
        self._taken: dict[tuple[str, str], str] = {}

    def local(self, fs_path: str) -> Path:
        fs_path = "/" + fs_path.strip("/") if fs_path.strip("/") else "/"
        if fs_path in self._map:
            return self._map[fs_path]
        parent, _, name = fs_path.rpartition("/")
        lp = self.local(parent or "/")
        cand = safe_name(name) if name not in (".", "..") else "%" + name
        n = 1
        while True:
            key = (str(lp), cand.casefold() if self.ci else cand)
            owner = self._taken.get(key)
            if owner is None or owner == fs_path:
                break
            n += 1
            cand = f"{safe_name(name)}%~{n}"
        self._taken[key] = fs_path
        self._map[fs_path] = lp / cand
        return self._map[fs_path]


def to_local_path(fs_path: str) -> Path:
    parts = [safe_name(p) for p in fs_path.strip("/").split("/") if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path(".")


def _set_times(target: Path, r) -> None:
    try:
        if r["mtime"]:
            os.utime(target, (r["atime"] or r["mtime"], r["mtime"]), follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass


def _h(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)
