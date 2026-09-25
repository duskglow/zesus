"""Verify every chosen data block of a reconstructed volume against its checksum.

Blocks are read in physical order, coalesced into large windows, so a spinning disk
streams instead of seeking. Failures then fall back to older candidates (other DVAs,
then older tree generations) one span at a time.

Progress is checkpointed to the map (the status array plus the next index into the
sorted work list), so an interrupted verification resumes where it stopped.
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass

import numpy as np

from ..map.codes import LOST, RECOVERED, BlockStatus
from ..map.db import MapDB, j
from ..zfs import blkptr
from ..zfs.blkptr import BlockPointer
from ..zfs.checksum import compute
from ..zfs.constants import VDEV_LABEL_START_SIZE, ChecksumType
from ..zfs.pool import Pool
from ..zfs.reader import ReadStatus
from .fastsum import fletcher4_many

log = logging.getLogger(__name__)

WINDOW_MAX = 32 << 20
GAP_MAX = 1 << 20
CKPT_EVERY_S = 120.0

_M63 = np.uint64((1 << 63) - 1)


@dataclass
class Span:
    span: int
    first: int
    count: int
    cands: list[tuple[BlockPointer, bool]]
    choice: bytearray
    status: bytearray


def load_spans(db: MapDB, volume_id: int) -> dict[int, Span]:
    out = {}
    for r in db.execute("SELECT span, first_blkid, count, candidates, choice, status FROM volume_spans "
                        "WHERE volume_id=?", (volume_id,)):
        blob = r["candidates"]
        cands = [(blkptr.parse(blob, i * 129), bool(blob[i * 129 + 128])) for i in range(len(blob) // 129)]
        out[r["span"]] = Span(r["span"], r["first_blkid"], r["count"], cands, bytearray(r["choice"]),
                              bytearray(r["status"]))
    return out


def l1_children(pool: Pool, bp: BlockPointer, count: int) -> np.ndarray | None:
    """Return the (count, 16) uint64 words of an L1 block's children, or None."""
    if bp.is_hole:
        return None
    rd = pool.reader.read(bp)
    if not rd.data:
        return None
    return np.frombuffer(rd.data, dtype="<u8", count=16 * count).reshape(count, 16)


def _fields(blkid: np.ndarray, w: np.ndarray) -> dict[str, np.ndarray]:
    prop = w[:, 6]
    return {
        "blkid": blkid.astype(np.int64),
        "vdev": (w[:, 0] >> np.uint64(32)).astype(np.uint32),
        "offset": ((w[:, 1] & _M63) << np.uint64(9)),
        "gang": (w[:, 1] >> np.uint64(63)).astype(bool),
        "embedded": ((prop >> np.uint64(39)) & np.uint64(1)).astype(bool),
        "psize": ((((prop >> np.uint64(16)) & np.uint64(0xFFFF)) + np.uint64(1)) << np.uint64(9)).astype(np.int64),
        "lsize": (((prop & np.uint64(0xFFFF)) + np.uint64(1)) << np.uint64(9)).astype(np.int64),
        "comp": ((prop >> np.uint64(32)) & np.uint64(0x7F)).astype(np.uint8),
        "ctype": ((prop >> np.uint64(40)) & np.uint64(0xFF)).astype(np.uint8),
        "cksum": w[:, 12:16].copy(),
    }


def chosen_blocks(pool: Pool, spans: dict[int, Span], statuses: set[int],
                  blk_range: tuple[int, int] | None = None, label: str = "") -> dict[str, np.ndarray]:
    """Collect the chosen L0 pointers whose status is in *statuses* (optionally only
    for block ids in [lo, hi)), as flat arrays sorted by (vdev, physical offset).

    Level-1 blocks are read in physical order. Slots whose L1 is no longer readable are
    marked CKSUM_MISMATCH in the span so the fallback logic can look for alternatives.
    Embedded and hole pointers are excluded; callers handle those per block.
    """
    need: dict[tuple, list[tuple[Span, int]]] = {}
    for sp in spans.values():
        if blk_range and (sp.first + sp.count <= blk_range[0] or sp.first >= blk_range[1]):
            continue
        used = {sp.choice[s] for s in range(sp.count) if sp.status[s] in statuses}
        for ci in used:
            if ci == 255:
                continue
            bp = sp.cands[ci][0]
            if bp.is_hole:
                continue
            need.setdefault((bp.dvas[0].vdev, bp.dvas[0].offset, bp.cksum[0]), []).append((sp, ci))
    keys = sorted(need)
    log.info("%s: reading %d level-1 blocks to enumerate data blocks", label, len(keys))
    parts: list[dict[str, np.ndarray]] = []
    t0 = time.monotonic()
    for i, k in enumerate(keys):
        users = need[k]
        sp0, ci0 = users[0]
        words = l1_children(pool, sp0.cands[ci0][0], 1 << 10)
        if i and i % 5000 == 0:
            log.info("  %d/%d level-1 blocks (%.0f/s)", i, len(keys), i / (time.monotonic() - t0))
        for sp, ci in users:
            sel = np.array([s for s in range(sp.count) if sp.choice[s] == ci and sp.status[s] in statuses
                            and (not blk_range or blk_range[0] <= sp.first + s < blk_range[1])], dtype=np.int64)
            if not len(sel):
                continue
            if words is None:
                for s in sel:
                    sp.status[s] = BlockStatus.CKSUM_MISMATCH
                continue
            f = _fields(sp.first + sel, words[sel])
            keep = ~f["embedded"] & ((words[sel][:, 0] != 0) | (words[sel][:, 1] != 0))
            parts.append({k2: v[keep] for k2, v in f.items()})
    if not parts:
        return {"blkid": np.zeros(0, np.int64)}
    wl = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    order = np.lexsort((wl["offset"], wl["vdev"]))
    return {k: v[order] for k, v in wl.items()}


def windows(wl: dict[str, np.ndarray], window_max: int = WINDOW_MAX, gap_max: int = GAP_MAX):
    """Yield (slice, vdev, start, end): runs of nearby blocks to read in one request."""
    n = len(wl["blkid"])
    i = 0
    while i < n:
        v = wl["vdev"][i]
        w_start = int(wl["offset"][i])
        w_end = w_start
        k = i
        while k < n and wl["vdev"][k] == v:
            o = int(wl["offset"][k])
            e = o + int(wl["psize"][k])
            if (o - w_end > gap_max or e - w_start > window_max) and k > i:
                break
            w_end = max(w_end, e)
            k += 1
        yield slice(i, k), int(v), w_start, w_end
        i = k


class Verifier:
    def __init__(self, pool: Pool, db: MapDB, volume_id: int, stop=None) -> None:
        self.pool, self.db, self.volume_id, self.stop = pool, db, volume_id, stop
        self.spans = load_spans(db, volume_id)
        im = pool.vdev_images[0]
        self.vsrc = im.source            # the vdev slice (DVA space is +4 MiB)
        if len(pool.vdevs.top) > 1:
            log.warning("multi-vdev pool: bulk verification reads vdev 0 directly and falls "
                        "back to per-block reads for other vdevs")

    # ------------------------------------------------------------------ work list
    def build_worklist(self) -> dict[str, np.ndarray]:
        """The chosen, not-yet-verified L0 pointers of every span, in physical order."""
        return chosen_blocks(self.pool, self.spans, {BlockStatus.UNKNOWN}, label="verify")

    # ------------------------------------------------------------------ bulk pass
    def run(self) -> None:
        wl = self.build_worklist()      # may mark slots whose L1 became unreadable
        status = np.zeros(max((sp.first + sp.count for sp in self.spans.values()), default=0), dtype=np.uint8)
        for sp in self.spans.values():
            status[sp.first:sp.first + sp.count] = np.frombuffer(bytes(sp.status), dtype=np.uint8)
        n = len(wl["blkid"])
        ck = self._load_ckpt(len(status))
        start = 0
        if ck is not None:
            start, saved = ck
            status = saved
            log.info("verify: resuming at %d/%d", start, n)
        log.info("verify: %d data blocks to verify (%.1f GiB)", n,
                 float(wl["psize"].sum()) / (1 << 30) if n else 0)
        t0 = time.monotonic()
        last_ck = t0
        done_bytes = 0
        sub = {k: v[start:] for k, v in wl.items()}
        for sl, v, w_start, w_end in windows(sub):
            if self.stop and self.stop.event.is_set():
                self._save_ckpt(start + sl.start, status)
                log.warning("verify: stopped at %d/%d (checkpoint saved)", start + sl.start, n)
                return
            self._verify_window(sub, sl, v, w_start, w_end, status)
            done_bytes += w_end - w_start
            i = start + sl.stop
            now = time.monotonic()
            if now - last_ck > CKPT_EVERY_S:
                self._save_ckpt(i, status)
                last_ck = now
                rate = done_bytes / (now - t0)
                rem = float(wl["psize"][i:].sum()) if i < n else 0
                log.info("verify: %5.1f%%  %.0f MB/s  ETA %dm", 100 * i / n, rate / 1e6, rem / rate // 60 if rate else 0)
        self._save_ckpt(n, status)
        self.fallback(status)
        self.write_back(status)
        self._clear_ckpt()

    def _verify_window(self, wl, sl, vdev: int, start: int, end: int, status: np.ndarray) -> None:
        blkid = wl["blkid"][sl]
        offs = wl["offset"][sl].astype(np.int64) - start
        psz = wl["psize"][sl]
        ctype = wl["ctype"][sl]
        gang = wl["gang"][sl]
        expect = wl["cksum"][sl]
        if vdev == 0 and 0 in self.pool.vdevs.top:
            buf = self.vsrc.pread(VDEV_LABEL_START_SIZE + start, end - start)
        else:
            buf = b""
        ok = np.zeros(len(blkid), dtype=bool)
        f4 = (ctype == ChecksumType.FLETCHER_4) & ~gang & (offs + psz <= len(buf))
        if f4.any():
            got = fletcher4_many(buf, offs[f4], psz[f4])
            ok[f4] = (got == expect[f4]).all(axis=1)
        others = np.nonzero(~f4)[0]
        for idx in others:
            o, p = int(offs[idx]), int(psz[idx])
            if gang[idx] or o + p > len(buf):
                ok[idx] = False       # handled per-block in fallback
                continue
            try:
                got = compute(int(ctype[idx]), buf[o:o + p])
            except Exception:
                got = None
            ok[idx] = got is None or tuple(got) == tuple(int(x) for x in expect[idx])
        status[blkid[ok]] = BlockStatus.OK
        bad = np.nonzero(~ok)[0]
        for idx in bad:
            o, p = int(offs[idx]), int(psz[idx])
            seg = buf[o:o + p] if o + p <= len(buf) else b""
            status[blkid[idx]] = BlockStatus.ZEROED if seg and not any(seg) else BlockStatus.CKSUM_MISMATCH

    # ------------------------------------------------------------------ fallback
    def fallback(self, status: np.ndarray) -> None:
        """For failed blocks, try (a) other DVAs/copies of the same pointer via the full
        reader, then (b) older candidates in the same span, newest first."""
        failed_spans = {}
        for sp in self.spans.values():
            seg = status[sp.first:sp.first + sp.count]
            bad = np.nonzero((seg == BlockStatus.CKSUM_MISMATCH) | (seg == BlockStatus.ZEROED))[0]
            if len(bad):
                failed_spans[sp.span] = bad
        if not failed_spans:
            log.info("verify: no failed blocks, nothing to fall back on")
            return
        total = sum(len(v) for v in failed_spans.values())
        log.info("verify: %d blocks in %d spans failed; trying other copies and older versions",
                 total, len(failed_spans))
        recovered = 0
        for span, bad in sorted(failed_spans.items()):
            sp = self.spans[span]
            kids = [l1_children(self.pool, bp, sp.count) for bp, _ in sp.cands]
            for s in bad:
                cur = sp.choice[s]
                tried = {cur}
                # (a) same pointer, full reader (other DVAs, gang blocks)
                if kids[cur] is not None:
                    bp = blkptr.parse(kids[cur][s].tobytes())
                    rd = self.pool.reader.read(bp, decompress=False)
                    if rd.status in (ReadStatus.OK, ReadStatus.UNVERIFIED):
                        status[sp.first + s] = BlockStatus.OK
                        recovered += 1
                        continue
                # (b) older candidates, newest first (the newest was chosen initially, so
                # every other candidate is the same age or older)
                cur_raw = kids[cur][s].tobytes() if kids[cur] is not None else None
                options = []
                for ci, w in enumerate(kids):
                    if ci in tried or w is None:
                        continue
                    raw = w[s].tobytes()
                    bp = blkptr.parse(raw)
                    if bp.is_hole or raw == cur_raw:
                        continue
                    options.append((bp.birth, ci, bp))
                for birth, ci, bp in sorted(options, key=lambda t: -t[0]):
                    rd = self.pool.reader.read(bp, decompress=False)
                    if rd.status in (ReadStatus.OK, ReadStatus.UNVERIFIED):
                        sp.choice[s] = ci
                        status[sp.first + s] = BlockStatus.OK_STALE
                        recovered += 1
                        break
        log.info("verify: fallback recovered %d of %d failed blocks", recovered, total)

    # ------------------------------------------------------------------ results
    def write_back(self, status: np.ndarray) -> None:
        with self.db.tx():
            for sp in self.spans.values():
                st = status[sp.first:sp.first + sp.count].tobytes()
                self.db.execute("UPDATE volume_spans SET status=?, choice=? WHERE volume_id=? AND span=?",
                                (st, bytes(sp.choice), self.volume_id, sp.span))
        summarize(self.db, self.volume_id)

    def _ckpt_key(self) -> str:
        return f"verify_ckpt:{self.volume_id}"

    def _save_ckpt(self, idx: int, status: np.ndarray) -> None:
        with self.db.tx():
            self.db.set_meta(self._ckpt_key(), j({"next": idx}))
            self.db.execute("DELETE FROM meta WHERE key=?", (self._ckpt_key() + ":status",))
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                            (self._ckpt_key() + ":status", zlib.compress(status.tobytes(), 1)))

    def _load_ckpt(self, n: int) -> tuple[int, np.ndarray] | None:
        import json
        v = self.db.get_meta(self._ckpt_key())
        if not v:
            return None
        blob = self.db.execute("SELECT value FROM meta WHERE key=?", (self._ckpt_key() + ":status",)).fetchone()
        if not blob:
            return None
        arr = np.frombuffer(zlib.decompress(blob[0]), dtype=np.uint8).copy()
        if len(arr) != n:
            return None
        return json.loads(v)["next"], arr

    def _clear_ckpt(self) -> None:
        with self.db.tx():
            self.db.execute("DELETE FROM meta WHERE key LIKE ?", (self._ckpt_key() + "%",))


def summarize(db: MapDB, volume_id: int) -> dict[str, int]:
    """Recompute the volume's counters and its run-length coverage table."""
    counts = np.zeros(256, dtype=np.int64)
    runs: list[tuple[int, int, int]] = []
    cur_status, cur_start, cur_len = None, 0, 0
    rows = db.execute("SELECT first_blkid, count, status FROM volume_spans WHERE volume_id=? ORDER BY span",
                      (volume_id,)).fetchall()
    vol = db.execute("SELECT n_blocks FROM volumes WHERE id=?", (volume_id,)).fetchone()
    n_blocks = vol[0] if vol else 0
    pos = 0

    def emit(st: int, start: int, length: int) -> None:
        nonlocal cur_status, cur_start, cur_len
        if cur_status == st and cur_start + cur_len == start:
            cur_len += length
        else:
            if cur_status is not None and cur_len:
                runs.append((cur_start, cur_len, cur_status))
            cur_status, cur_start, cur_len = st, start, length

    for r in rows:
        if r["first_blkid"] > pos:
            emit(BlockStatus.NO_METADATA, pos, r["first_blkid"] - pos)
            counts[BlockStatus.NO_METADATA] += r["first_blkid"] - pos
        st = np.frombuffer(r["status"], dtype=np.uint8)
        counts += np.bincount(st, minlength=256)
        change = np.nonzero(np.diff(st.astype(np.int16)))[0] + 1
        bounds = [0, *change.tolist(), len(st)]
        for a, b in zip(bounds[:-1], bounds[1:]):
            emit(int(st[a]), r["first_blkid"] + a, b - a)
        pos = r["first_blkid"] + r["count"]
    if n_blocks and pos < n_blocks:
        emit(BlockStatus.NO_METADATA, pos, n_blocks - pos)
        counts[BlockStatus.NO_METADATA] += n_blocks - pos
    if cur_status is not None and cur_len:
        runs.append((cur_start, cur_len, cur_status))
    from ..map.codes import DESCRIPTIONS
    with db.tx():
        db.execute("DELETE FROM volume_coverage WHERE volume_id=?", (volume_id,))
        db.conn.executemany("INSERT INTO volume_coverage(volume_id,first_blkid,count,status,reason) VALUES(?,?,?,?,?)",
                            [(volume_id, a, n, BlockStatus(s).name.lower(), DESCRIPTIONS[BlockStatus(s)])
                             for a, n, s in runs])
        c = {s: int(counts[s]) for s in BlockStatus}
        db.execute("UPDATE volumes SET n_verified=?, n_holes=?, n_stale=?, n_missing=?, n_damaged=?, status=? "
                   "WHERE id=?",
                   (c[BlockStatus.OK] + c[BlockStatus.EMBEDDED],
                    c[BlockStatus.HOLE] + c[BlockStatus.DISCARDED], c[BlockStatus.OK_STALE],
                    c[BlockStatus.NO_METADATA] + c[BlockStatus.UNKNOWN],
                    sum(c[s] for s in LOST if s != BlockStatus.NO_METADATA),
                    "verified" if c[BlockStatus.UNKNOWN] == 0 else "mapped", volume_id))
    lost = sum(c[s] for s in LOST)
    rec = sum(c[s] for s in RECOVERED)
    log.info("volume %d: %d blocks recovered, %d holes, %d lost, %d unverified", volume_id, rec,
             c[BlockStatus.HOLE] + c[BlockStatus.DISCARDED], lost, c[BlockStatus.UNKNOWN])
    return {BlockStatus(s).name: v for s, v in c.items() if v}

