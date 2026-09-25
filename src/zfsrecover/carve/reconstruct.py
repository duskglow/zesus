"""Rebuild a volume's block map from every surviving version of its block tree.

ZFS never overwrites live data in place. Each txg that modified a zvol wrote a new copy
of every indirect block on the path to the changed data, then freed the old copies.
After a destroy, the disk holds many *generations* of the tree, each partially
overwritten by later allocations. This module merges them:

1. **Roots.** Top-level block pointers for the volume's data object come from:
   * objset roots still reachable through ring uberblocks (``dataset_roots``);
   * carved ``objset_phys_t`` blocks of type ZVOL;
   * carved meta-dnode blocks holding a ZVOL dnode.

   Roots must share the volume's geometry (levels, max block id, block size).
2. **Top-down walk.** At each level, every readable candidate block contributes its
   children as candidates at known positions (verified through their parent's checksum).
3. **Placement of orphans.** Carved indirect blocks nobody points to are placed at a
   position when their children coincide with those of blocks already known there
   (same slot, same DVA). Copy-on-write changes only the modified children, so
   generations of the same position share most pointers. Placed blocks are flagged as
   carved, and their children are still verified individually.
4. **Choice.** For every logical block, candidates are ordered newest first by birth
   txg. The verify phase then walks that order until a copy passes its checksum.

The result is written to ``volume_spans``, one row per level-1 span.
"""

from __future__ import annotations

import logging
import struct
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..map.codes import BlockStatus
from ..map.db import MapDB
from ..zfs import blkptr
from ..zfs.blkptr import BlockPointer
from ..zfs.constants import Compression, ChecksumType, DmuType
from ..zfs.dnode import Dnode, parse_dnode
from ..zfs.objset import Objset
from ..zfs.pool import Pool
from ..zfs.reader import BlockRead
from ..zfs.zap import parse_micro, read_zap

log = logging.getLogger(__name__)

NO_CHOICE = 255
MAX_CANDIDATES = 254


def bp_key(bp: BlockPointer) -> tuple:
    if bp.is_hole:
        return ("hole", bp.birth)
    if bp.embedded:
        return ("emb", bp.raw)
    d = bp.dvas[0]
    return (d.vdev, d.offset, bp.cksum[0])


@dataclass
class Root:
    txg: int
    top_bp: BlockPointer
    dnode: Dnode
    provenance: str
    volsize: int | None = None

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        d = self.dnode
        return (d.nlevels, d.maxblkid, d.datablksz, d.indblkshift)


@dataclass
class Cand:
    bp: BlockPointer
    carved: bool                 # placed by matching rather than pointed to by a parent
    source: str

    @property
    def birth(self) -> int:
        return self.bp.birth


@dataclass
class ReconstructStats:
    roots: int = 0
    per_level: dict[int, dict[str, int]] = field(default_factory=dict)
    placed: dict[int, int] = field(default_factory=dict)
    unplaced: dict[int, int] = field(default_factory=dict)
    ambiguous: dict[int, int] = field(default_factory=dict)


class VolumeReconstructor:
    def __init__(self, pool: Pool, db: MapDB, pool_id: int, dataset: dict) -> None:
        self.pool = pool
        self.r = pool.reader
        self.db = db
        self.pool_id = pool_id
        self.ds = dataset
        self.destroy_txg = dataset.get("destroy_txg") or (pool.max_txg + 1)
        self.stats = ReconstructStats()
        self._carved_by_loc: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ roots
    def roots_from_rings(self) -> list[Root]:
        roots = []
        for row in self.db.execute("SELECT seen_txg, objset_bp FROM dataset_roots WHERE dataset_id=? "
                                   "ORDER BY bp_birth DESC", (self.ds["id"],)):
            bp = blkptr.parse(row["objset_bp"])
            try:
                os_ = Objset(self.r, bp, self.ds["name"])
                roots.append(self._root_from_objset(os_, bp.birth, f"ring-mos:{row['seen_txg']}"))
            except Exception as exc:
                log.info("root from ring txg %d unreadable: %s", row["seen_txg"], exc)
        return [r for r in roots if r]

    def _root_from_objset(self, os_: Objset, txg: int, prov: str) -> Root | None:
        d1 = os_.dnode(1)
        if d1.type != DmuType.ZVOL:
            return None
        volsize = None
        try:
            volsize = read_zap(os_.object(2)).get("size")
        except Exception:
            pass
        return Root(txg=txg, top_bp=d1.blkptrs[0], dnode=d1, provenance=prov, volsize=volsize)

    def roots_from_carving(self, geometry: tuple[int, int, int, int] | None) -> list[Root]:
        roots: list[Root] = []
        rows = self.db.execute(
            "SELECT id, phys, psize_hint, max_birth, kind, info_json FROM carved WHERE pool_id=? AND "
            "((kind='objset' AND os_type=3) OR (kind='dnodes' AND info_json LIKE '%\"slots\": [[1, 23,%')) "
            "AND max_birth < ? ORDER BY max_birth DESC", (self.pool_id, self.destroy_txg)).fetchall()
        for row in rows:
            try:
                raw = self._read_phys(row["phys"], row["psize_hint"])
                if row["kind"] == "objset":
                    os_ = Objset(self.r, raw, f"carved@{row['phys']:#x}")
                    root = self._root_from_objset(os_, row["max_birth"], f"carved-objset@{row['phys']:#x}")
                else:
                    import lz4.block
                    clen = struct.unpack_from(">I", raw, 0)[0]
                    data = lz4.block.decompress(raw[4:4 + clen], uncompressed_size=128 << 10)
                    d1 = parse_dnode(data, 512)
                    root = Root(txg=d1.blkptrs[0].birth, top_bp=d1.blkptrs[0], dnode=d1,
                                provenance=f"carved-metadnode@{row['phys']:#x}")
                    # volsize lives in object 2 (a micro-ZAP) — only reachable via its bp
                    try:
                        d2 = parse_dnode(data, 1024)
                        if d2.type == DmuType.ZVOL_PROP:
                            zr = self.r.read(d2.blkptrs[0])
                            if zr.data:
                                root.volsize = parse_micro(zr.data).get("size")
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("carved root candidate at %#x unusable: %s", row["phys"], exc)
                continue
            if root is None:
                continue
            if geometry and root.geometry != geometry:
                continue
            roots.append(root)
        return roots

    def _read_phys(self, phys: int, n: int) -> bytes:
        im = self.pool.vdev_images[0]
        return im.source.read_exact(phys - im.base_offset, n)

    # ------------------------------------------------------------------ helpers
    def read_many(self, cands: Iterable[Cand]) -> dict[tuple, BlockRead]:
        """Read blocks in physical order (the source may be a spinning disk)."""
        out: dict[tuple, BlockRead] = {}
        uniq = {}
        for c in cands:
            if not c.bp.is_hole:
                uniq.setdefault(bp_key(c.bp), c.bp)
        order = sorted(uniq.items(), key=lambda kv: (kv[1].dvas[0].vdev, kv[1].dvas[0].offset)
                       if not kv[1].embedded else (-1, 0))
        t0 = time.monotonic()
        for i, (k, bp) in enumerate(order):
            out[k] = self.r.read(bp)
            if i and i % 5000 == 0:
                log.info("  read %d/%d indirect blocks (%.0f/s)", i, len(order), i / (time.monotonic() - t0))
        return out

    def carved_at_level(self, level: int) -> list[BlockPointer]:
        """Carved indirect blocks for ZVOL data at *level* (children at level-1), as
        synthetic BPs whose checksum is computed from the block as found."""
        from ..zfs.checksum import fletcher4
        bps = []
        for row in self.db.execute(
                "SELECT dva_offset, vdev_top, phys, psize_hint, lsize, max_birth FROM carved WHERE pool_id=? "
                "AND kind='indirect' AND child_type=? AND child_level=? AND max_birth < ? ORDER BY phys",
                (self.pool_id, DmuType.ZVOL, level - 1, self.destroy_txg)):
            raw = self._read_phys(row["phys"], row["psize_hint"])
            bps.append(blkptr.build(
                vdev=row["vdev_top"], offset=row["dva_offset"], asize=row["psize_hint"],
                psize=row["psize_hint"], lsize=row["lsize"], comp=Compression.LZ4,
                cksum_type=ChecksumType.FLETCHER_4, type=DmuType.ZVOL, level=level,
                birth=row["max_birth"], cksum=fletcher4(raw)))
        return bps

    # ------------------------------------------------------------------ main
    def build(self, roots: list[Root], limit_blocks: int | None = None) -> tuple[dict[int, list[Cand]], Root]:
        """Return (level-1 candidates by span index, reference root).

        *limit_blocks* restricts the walk to the first N logical blocks (for testing).
        """
        if not roots:
            raise ValueError("no usable roots")
        ref = max(roots, key=lambda r: (r.provenance.startswith("ring"), r.txg))
        nlevels, maxblkid, dblksz, ibs = ref.geometry
        if limit_blocks:
            maxblkid = min(maxblkid, limit_blocks - 1)
        self.maxblkid = maxblkid
        epb_shift = ibs - 7
        epb = 1 << epb_shift
        top = nlevels - 1
        self.stats.roots = len(roots)
        cur: dict[int, list[Cand]] = defaultdict(list)
        seen_keys: dict[int, set] = defaultdict(set)
        for r in sorted(roots, key=lambda r: -r.txg):
            k = bp_key(r.top_bp)
            if k not in seen_keys[0]:
                seen_keys[0].add(k)
                cur[0].append(Cand(r.top_bp, False, r.provenance))
        level = top
        while level >= 1:
            n_pos = sum(1 for p in cur if cur[p])
            log.info("level %d: %d positions, %d candidate blocks", level, n_pos, sum(len(v) for v in cur.values()))
            reads = self.read_many(c for v in cur.values() for c in v)
            st = defaultdict(int)
            for rd in reads.values():
                st[rd.status.value] += 1
            self.stats.per_level[level] = dict(st)
            log.info("level %d reads: %s", level, dict(st))

            # --- place carved orphans at this level
            known_locs = {bp_key(c.bp)[:2] for v in cur.values() for c in v if not c.bp.is_hole}
            index: dict[tuple[int, int, int], int] = {}
            for pos, cands in cur.items():
                for c in cands:
                    rd = reads.get(bp_key(c.bp))
                    if rd and rd.data:
                        for s, child in enumerate(blkptr.parse_array(rd.data)):
                            if not child.is_hole and not child.embedded:
                                index[(s, child.dvas[0].vdev, child.dvas[0].offset)] = pos
            placed = unplaced = ambiguous = 0
            for bp in self.carved_at_level(level):
                if (bp.dvas[0].vdev, bp.dvas[0].offset) in known_locs:
                    continue
                rd = self.r.read(bp)
                if not rd.data:
                    continue
                votes = defaultdict(int)
                for s, child in enumerate(blkptr.parse_array(rd.data)):
                    if not child.is_hole and not child.embedded:
                        p = index.get((s, child.dvas[0].vdev, child.dvas[0].offset))
                        if p is not None:
                            votes[p] += 1
                if not votes:
                    unplaced += 1
                    continue
                if len(votes) > 1:
                    ambiguous += 1
                    best, n = max(votes.items(), key=lambda kv: kv[1])
                    if n < 0.9 * sum(votes.values()):
                        continue
                else:
                    best = next(iter(votes))
                cur[best].append(Cand(bp, True, f"carved@{bp.dvas[0].offset:#x}"))
                reads[bp_key(bp)] = rd
                placed += 1
            self.stats.placed[level], self.stats.unplaced[level] = placed, unplaced
            self.stats.ambiguous[level] = ambiguous
            log.info("level %d: placed %d carved blocks (%d unplaceable, %d ambiguous)",
                     level, placed, unplaced, ambiguous)

            if level == 1:
                self._level1_reads = reads
                return cur, ref

            # --- expand to the next level
            nxt: dict[int, list[Cand]] = defaultdict(list)
            nkeys: dict[int, set] = defaultdict(set)
            span_next = 1 << (epb_shift * (level - 1))
            for pos, cands in cur.items():
                for c in cands:
                    rd = reads.get(bp_key(c.bp))
                    if c.bp.is_hole:
                        # a hole at this level covers every child position
                        for s in range(epb):
                            cp = pos * epb + s
                            if cp * span_next > maxblkid:
                                break
                            k = bp_key(c.bp)
                            if k not in nkeys[cp]:
                                nkeys[cp].add(k)
                                nxt[cp].append(Cand(c.bp, False, c.source))
                        continue
                    if not rd or not rd.data:
                        continue
                    for s, child in enumerate(blkptr.parse_array(rd.data)):
                        cp = pos * epb + s
                        if cp * span_next > maxblkid:
                            break
                        k = bp_key(child)
                        if k in nkeys[cp]:
                            continue
                        nkeys[cp].add(k)
                        nxt[cp].append(Cand(child, False, c.source))
            cur = nxt
            level -= 1
        raise AssertionError("unreachable")

    def choose(self, spans: dict[int, list[Cand]], ref: Root) -> Iterable[tuple[int, int, int, bytes, bytes, bytes, int]]:
        """For every span, order candidates newest-first and pick an initial choice per slot.

        Yields (span, first_blkid, count, candidates_blob, choice, status, max_birth).
        """
        nlevels, _, dblksz, ibs = ref.geometry
        maxblkid = self.maxblkid
        epb = 1 << (ibs - 7)
        reads = self._level1_reads
        for span in sorted(spans):
            cands = sorted(spans[span], key=lambda c: (-c.birth, c.carved))[:MAX_CANDIDATES]
            first = span * epb
            count = min(epb, maxblkid + 1 - first)
            if count <= 0:
                continue
            best_birth = [-1] * count
            choice = bytearray([NO_CHOICE]) * count
            status = bytearray([BlockStatus.NO_METADATA]) * count
            max_birth = 0
            for ci, c in enumerate(cands):
                if c.bp.is_hole:
                    children = [c.bp] * count
                else:
                    rd = reads.get(bp_key(c.bp))
                    if not rd or not rd.data:
                        continue
                    children = blkptr.parse_array(rd.data, count)
                for s, ch in enumerate(children):
                    b = ch.birth
                    if b > best_birth[s]:
                        best_birth[s] = b
                        choice[s] = ci
                        if ch.is_hole:
                            status[s] = BlockStatus.DISCARDED if b else BlockStatus.HOLE
                        elif ch.embedded:
                            status[s] = BlockStatus.EMBEDDED
                        else:
                            status[s] = BlockStatus.UNKNOWN
                        max_birth = max(max_birth, b)
            blob = b"".join(c.bp.raw + bytes([1 if c.carved else 0]) for c in cands)
            yield span, first, count, blob, bytes(choice), bytes(status), max_birth
