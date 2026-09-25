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
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..carve.fastsum import fletcher4_many
from ..carve.verify import chosen_blocks, load_spans, windows
from ..io import guard
from ..map.codes import DESCRIPTIONS, RECOVERED, BlockStatus
from ..map.db import MapDB, now
from ..zfs import compress
from ..zfs.checksum import compute
from ..zfs.constants import VDEV_LABEL_START_SIZE, ChecksumType
from ..zfs.pool import Pool
from ..volume.logical import LogicalVolume
from .sparse import make_sparse

log = logging.getLogger(__name__)

GAP_PATTERN = b"<<ZFSRECOVER:UNRECOVERABLE>>\n"


@dataclass
class Options:
    out_dir: Path
    fill: str = "zero"                  # zero | pattern
    include_unverified: bool = False
    force: bool = False
    partial_suffix: str | None = None   # e.g. ".PARTIAL"
    sparse: bool = True


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
    def __init__(self, db: MapDB, pool: Pool, opts: Options) -> None:
        self.db, self.pool, self.opts = db, pool, opts
        self.records: list[OutputRecord] = []
        self._fs_handles: dict = {}
        out = Path(opts.out_dir)
        guard.assert_not_protected([out])
        out.mkdir(parents=True, exist_ok=True)
        self.out = out

    # ------------------------------------------------------------------ helpers
    def _open_out(self, path: Path, size: int):
        guard.assert_not_protected([path])
        if path.exists() and not self.opts.force:
            raise FileExistsError(f"{path} exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "wb")
        if self.opts.sparse:
            make_sparse(f)
        f.truncate(size)
        return f

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
        wl = chosen_blocks(self.pool, spans, {BlockStatus.OK, BlockStatus.OK_STALE, BlockStatus.UNKNOWN},
                           blk_range=(lo, hi), label="extract")
        n = len(wl["blkid"])
        written = np.zeros(hi - lo, dtype=bool)
        t0 = last_log = time.monotonic()
        total = float(wl["psize"].sum()) if n else 0.0
        done = 0
        vsrc = self.pool.vdev_images[0].source
        lv = LogicalVolume(self.pool, self.db, volume_id)
        log.info("extracting %s → %s: %d data blocks (%.1f GiB on disk)", source_desc, path, n, total / (1 << 30))
        with self._open_out(path, length) as f:
            for sl, vdev, w0, w1 in windows(wl):
                buf = vsrc.pread(VDEV_LABEL_START_SIZE + w0, w1 - w0) if vdev == 0 else b""
                offs = wl["offset"][sl].astype(np.int64) - w0
                psz = wl["psize"][sl]
                ctype = wl["ctype"][sl]
                ok = np.zeros(len(offs), dtype=bool)
                f4 = (ctype == ChecksumType.FLETCHER_4) & ~wl["gang"][sl] & (offs + psz <= len(buf))
                if f4.any():
                    ok[f4] = (fletcher4_many(buf, offs[f4], psz[f4]) == wl["cksum"][sl][f4]).all(axis=1)
                for k in range(len(offs)):
                    idx = sl.start + k
                    blkid = int(wl["blkid"][idx])
                    o, p = int(offs[k]), int(psz[k])
                    raw = buf[o:o + p] if o + p <= len(buf) else None
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
                            log.warning("block %d: decompression failed: %s", blkid, exc)
                    if data is None:
                        # gang blocks, other vdevs, fallbacks: go through the full reader
                        d2, st = lv.read_block(blkid)
                        if d2 is not None:
                            data = d2
                        elif self.opts.include_unverified and raw is not None:
                            try:
                                data = compress.decompress(int(wl["comp"][idx]), raw, int(wl["lsize"][idx]))
                                rec.extra.setdefault("unverified_blocks", []).append(blkid)
                                log.warning("block %d written UNVERIFIED (checksum mismatch)", blkid)
                            except Exception:
                                data = None
                    if data is None:
                        continue
                    self._write_block(f, blkid, bs, data, start, end)
                    written[blkid - lo] = True
                done += w1 - w0
                if time.monotonic() - last_log > 30:
                    last_log = time.monotonic()
                    log.info("  %.1f%% (%.0f MB/s)", 100 * done / max(1, total), done / (last_log - t0) / 1e6)
            # holes, embedded blocks, and anything not in the physical work list
            for blkid in range(lo, hi):
                if written[blkid - lo]:
                    continue
                st = lv.block_status(blkid)
                if st in (BlockStatus.HOLE, BlockStatus.DISCARDED):
                    continue
                data, st2 = lv.read_block(blkid) if st in RECOVERED or st == BlockStatus.UNKNOWN else (None, st)
                if data is not None:
                    self._write_block(f, blkid, bs, data, start, end)
                    continue
                a = max(start, blkid * bs)
                b = min(end, (blkid + 1) * bs)
                self._add_gap(rec, a - start, b - a, DESCRIPTIONS[st2])
            for g in rec.gaps:
                self._fill(f, g.offset, g.length)
        self._finish(rec, path, image=True)
        return rec

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

    @staticmethod
    def _add_gap(rec: OutputRecord, off: int, length: int, reason: str) -> None:
        if rec.gaps and rec.gaps[-1].offset + rec.gaps[-1].length == off and rec.gaps[-1].reason == reason:
            rec.gaps[-1].length += length
        else:
            rec.gaps.append(GapRecord(off, length, reason))

    def _finish(self, rec: OutputRecord, path: Path, image: bool) -> None:
        lost = rec.lost_bytes
        rec.status = "full" if lost == 0 else ("none" if lost >= rec.size and rec.size else "partial")
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
                                      "gaps": [g.__dict__ for g in rec.gaps]}, indent=1))
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
                      include_deleted: bool = True) -> None:
        fs = self.db.execute("SELECT * FROM filesystems WHERE id=?", (fs_id,)).fetchone()
        if fs is None:
            raise KeyError(f"no filesystem {fs_id}")
        lv = LogicalVolume(self.pool, self.db, fs["volume_id"])
        root = self.out / f"fs{fs_id}-{fs['fstype']}{'-' + safe_name(fs['label']) if fs['label'] else ''}"
        rows = self.db.execute("SELECT * FROM fs_entries WHERE fs_id=? ORDER BY path", (fs_id,)).fetchall()
        sel = []
        for r in rows:
            if not include_deleted and r["deleted"]:
                continue
            if statuses and r["status"] not in statuses and r["type"] in ("file", "symlink"):
                continue
            if patterns and not any(fnmatch.fnmatchcase(r["path"], p) or r["path"].startswith(p.rstrip("/") + "/")
                                    or r["path"] == p for p in patterns):
                continue
            sel.append(r)
        log.info("extracting %d entries from filesystem %d into %s", len(sel), fs_id, root)
        # order files by their first data extent to reduce seeking
        firsts = {r["id"]: (self.db.execute("SELECT min(volume_offset) FROM fs_extents WHERE entry_id=?",
                                            (r["id"],)).fetchone()[0] or 0) for r in sel if r["type"] == "file"}
        dirs_meta = []
        for r in sorted(sel, key=lambda r: (r["type"] != "dir", firsts.get(r["id"], 0))):
            target = root / to_local_path(r["path"])
            try:
                if r["type"] == "dir":
                    target.mkdir(parents=True, exist_ok=True)
                    dirs_meta.append((target, r))
                elif r["type"] == "file":
                    self._extract_file(lv, r, target)
                elif r["type"] == "symlink":
                    self._extract_symlink(r, target)
            except FileExistsError as exc:
                log.warning("%s", exc)
            except Exception as exc:
                log.error("%s: extraction failed: %s", r["path"], exc)
        for target, r in reversed(dirs_meta):
            _set_times(target, r)

    def _extract_file(self, lv: LogicalVolume, r, target: Path) -> None:
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
                old = json.loads(path.read_text()).get("outputs", [])
            except Exception:
                old = []
        outs = old + [{**r.__dict__, "gaps": [g.__dict__ for g in r.gaps], "lost_bytes": r.lost_bytes}
                      for r in self.records]
        summary = {s: sum(1 for o in outs if o["status"] == s) for s in ("full", "partial", "none")}
        path.write_text(json.dumps({"tool": "zfs-forensic-recovery", "written_at": now(),
                                    "summary": summary, **(extra or {}), "outputs": outs}, indent=1))
        return path


def write_ddrescue_map(path: Path, size: int, gaps: Iterable[GapRecord]) -> None:
    lines = ["# Mapfile. Created by zfs-forensic-recovery", "# current_pos  current_status  current_pass",
             "0x00000000     +               1", "#      pos        size  status"]
    pos = 0
    for g in sorted(gaps, key=lambda g: g.offset):
        if g.offset > pos:
            lines.append(f"0x{pos:08X}  0x{g.offset - pos:08X}  +")
        lines.append(f"0x{g.offset:08X}  0x{g.length:08X}  -")
        pos = g.offset + g.length
    if pos < size:
        lines.append(f"0x{pos:08X}  0x{size - pos:08X}  +")
    path.write_text("\n".join(lines) + "\n")


_WIN_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """Make one path component safe for the local filesystem, reversibly (%XX escapes)."""
    if sys.platform == "win32":
        name = _WIN_BAD.sub(lambda m: f"%{ord(m.group()):02X}", name)
        if name.split(".")[0].upper() in _WIN_RESERVED or name.endswith((" ", ".")):
            name = name + "%"
    return name.replace("/", "%2F") or "%"


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
