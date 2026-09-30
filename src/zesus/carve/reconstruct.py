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

import numpy as np

from ..map.codes import BlockStatus
from ..map.db import MapDB
from ..zfs import blkptr
from ..zfs.blkptr import BlockPointer
from ..zfs.constants import ChecksumType, Compression, DmuType
from ..zfs.dnode import Dnode, parse_dnode
from ..zfs.objset import Objset
from ..zfs.pool import Pool
from ..zfs.reader import BlockRead, ReadStatus
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
            "SELECT id, phys, vdev_top, dva_offset, psize_hint, max_birth, kind, info_json FROM carved "
            "WHERE pool_id=? AND "
            "((kind='objset' AND os_type=3) OR (kind='dnodes' AND info_json LIKE '%\"slots\": [[1, 23,%')) "
            "AND max_birth < ? ORDER BY max_birth DESC", (self.pool_id, self.destroy_txg)).fetchall()
        for row in rows:
            try:
                raw = self._read_carved(row["vdev_top"], row["dva_offset"], row["psize_hint"])
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

    def _read_carved(self, vdev_top: int, dva_offset: int, n: int) -> bytes:
        """Unverified bytes of a carved block, through the vdev layer (so RAIDZ columns are
        assembled, and rebuilt from parity where a member is missing). Carved blocks are
        verified later, against the parent pointers that reference them."""
        top = self.pool.vdevs.top.get(vdev_top)
        if top is None:
            raise EOFError(f"carved block on absent vdev {vdev_top}")
        for cand in top.candidates(dva_offset, n):
            return cand.data
        raise EOFError(f"carved block at {vdev_top}:{dva_offset:#x} unreadable")

    # ------------------------------------------------------------------ association
    def signature(self, root: Root, depth: int = 2) -> set[tuple] | None:
        """DVA keys of the top block and its descendants down *depth* levels (upper tree only).

        Copy-on-write generations of one dataset share every subtree that did not change
        between them. Different datasets never share blocks (clones and dedup aside).
        Returns None when the top block itself is unreadable: such a root carries no
        information and must not become a (phantom) volume of its own.
        """
        top = root.top_bp
        if not top.is_hole and not top.embedded:
            if not self.r.read(top).data:
                return None
        sig = {bp_key(top)}
        frontier = [top]
        for _ in range(depth):
            nxt = []
            for bp in frontier:
                if bp.is_hole or bp.embedded or bp.level == 0:
                    continue
                rd = self.r.read(bp)
                if not rd.data:
                    continue
                for child in blkptr.parse_array(rd.data):
                    if not child.is_hole and not child.embedded:
                        sig.add(bp_key(child))
                        nxt.append(child)
            frontier = nxt
        return sig

    def cluster(self, roots: list[Root], anchors: list[Root] | None = None) -> list[list[Root]]:
        """Group roots into datasets by shared tree blocks (union-find).

        With *anchors*, only the cluster connected to them is returned (anchors first).
        Roots sharing nothing with the anchors are left out and reported, never merged by
        size alone.
        """
        anchors = anchors or []
        sig_a = [self.signature(r) or {bp_key(r.top_bp)} for r in anchors]
        kept, sig_r = [], []
        for r in roots:
            sg = self.signature(r)
            if sg is not None:
                kept.append(r)
                sig_r.append(sg)
        if len(kept) < len(roots):
            log.info("%d carved root(s) dropped: their top-level block has since been overwritten",
                     len(roots) - len(kept))
        allr = anchors + kept
        sigs = sig_a + sig_r
        parent = list(range(len(allr)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        owner: dict[tuple, int] = {}
        for i, sig in enumerate(sigs):
            for k in sig:
                j = owner.setdefault(k, i)
                if j != i:
                    parent[find(i)] = find(j)
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(len(allr)):
            groups[find(i)].append(i)
        if anchors:
            a = find(0)
            members = [allr[i] for i in groups[a] if i >= len(anchors)]
            others = sum(len(g) for k, g in groups.items() if k != a)
            if others:
                log.info("%d carved root(s) with the same geometry share no blocks with %s and were "
                         "not merged into it", others, self.ds.get("name"))
            return [members]
        return [[allr[i] for i in g] for g in groups.values() if g]

    # ------------------------------------------------------------------ helpers
    def read_many(self, cands: Iterable[Cand], keep_data: bool = True,
                  on_block=None) -> dict[tuple, BlockRead | Summary]:
        """Read blocks in physical order (the source may be a spinning disk).

        With ``keep_data=False`` only a compact :class:`Summary` of each block's children is
        kept, and ``on_block(key, words)`` is called with the full child pointers first.
        This keeps memory bounded for the tens of thousands of level-1 blocks in a large
        volume.
        """
        out: dict[tuple, BlockRead | Summary] = {}
        uniq = {}
        for c in cands:
            if not c.bp.is_hole:
                uniq.setdefault(bp_key(c.bp), c.bp)
        order = sorted(uniq.items(), key=lambda kv: (kv[1].dvas[0].vdev, kv[1].dvas[0].offset)
                       if not kv[1].embedded else (-1, 0))
        t0 = time.monotonic()
        for i, (k, bp) in enumerate(order):
            rd = self.r.read(bp)
            if keep_data or rd.data is None:
                out[k] = rd
            else:
                words = child_words(rd.data)
                if on_block:
                    on_block(k, words)
                out[k] = Summary.of(words, rd.status)
            if i and i % 5000 == 0:
                log.info("  read %d/%d indirect blocks (%.0f/s)", i, len(order), i / (time.monotonic() - t0))
        return out

    def carved_at_level(self, level: int, skip: set[tuple[int, int]]):
        """Yield carved indirect blocks for ZVOL data at *level* (children at level-1).

        Each is a synthetic BP (checksum computed from the block as found, since no parent
        vouches for it) with its child pointers. Locations in *skip* are not read at all.
        Blocks are read once, in physical order.
        """
        import lz4.block

        from ..zfs.checksum import fletcher4
        rows = self.db.execute(
            "SELECT dva_offset, vdev_top, phys, psize_hint, lsize, max_birth FROM carved WHERE pool_id=? "
            "AND kind='indirect' AND child_type=? AND child_level=? AND max_birth < ? ORDER BY phys",
            (self.pool_id, DmuType.ZVOL, level - 1, self.destroy_txg)).fetchall()
        todo = [r for r in rows if (r["vdev_top"], r["dva_offset"]) not in skip]
        log.info("level %d: %d carved candidate blocks (%d already known)", level, len(todo), len(rows) - len(todo))
        t0 = time.monotonic()
        for i, row in enumerate(todo):
            if i and i % 20000 == 0:
                log.info("  carved %d/%d (%.0f/s)", i, len(todo), i / (time.monotonic() - t0))
            try:
                raw = self._read_carved(row["vdev_top"], row["dva_offset"], row["psize_hint"])
                clen = struct.unpack_from(">I", raw, 0)[0]
                data = lz4.block.decompress(raw[4:4 + clen], uncompressed_size=row["lsize"])
            except Exception:
                continue
            bp = blkptr.build(
                vdev=row["vdev_top"], offset=row["dva_offset"], asize=row["psize_hint"],
                psize=row["psize_hint"], lsize=row["lsize"], comp=Compression.LZ4,
                cksum_type=ChecksumType.FLETCHER_4, type=DmuType.ZVOL, level=level,
                birth=row["max_birth"], cksum=fletcher4(raw))
            yield bp, data

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
        seen: set = set()
        for r in sorted(roots, key=lambda r: -r.txg):
            k = bp_key(r.top_bp)
            if k not in seen:
                seen.add(k)
                cur[0].append(Cand(r.top_bp, False, r.provenance))
        level = top
        while level >= 1:
            last = level == 1
            log.info("level %d: %d positions, %d candidate blocks", level, sum(1 for p in cur if cur[p]),
                     sum(len(v) for v in cur.values()))
            # placement index over the children of every known block at this level
            pos_of: dict[tuple, int] = {}
            for pos, cands in cur.items():
                for c in cands:
                    pos_of.setdefault(bp_key(c.bp), pos)
            idx = ChildIndex()
            reads = self.read_many((c for v in cur.values() for c in v), keep_data=not last,
                                   on_block=lambda k, w, idx=idx, pos_of=pos_of: idx.add(w, pos_of[k]))
            if not last:
                for k, rd in reads.items():
                    if rd.data:
                        idx.add(child_words(rd.data), pos_of[k])
            st = defaultdict(int)
            for rd in reads.values():
                st[rd.status.value] += 1
            self.stats.per_level[level] = dict(st)
            log.info("level %d reads: %s", level, dict(st))
            idx.freeze()

            # --- place carved orphans at this level
            known_locs = {bp_key(c.bp)[:2] for v in cur.values() for c in v if not c.bp.is_hole}
            placed = unplaced = ambiguous = 0
            for bp, data in self.carved_at_level(level, known_locs):
                words = child_words(data)
                best, votes, total = idx.vote(words)
                if best is None:
                    unplaced += 1
                    continue
                if votes < total:
                    ambiguous += 1
                    if votes < 0.9 * total:
                        continue
                cur[best].append(Cand(bp, True, f"carved@{bp.dvas[0].offset:#x}"))
                reads[bp_key(bp)] = Summary.of(words, ReadStatus.OK) if last else BlockRead(ReadStatus.OK, data)
                placed += 1
            self.stats.placed[level], self.stats.unplaced[level] = placed, unplaced
            self.stats.ambiguous[level] = ambiguous
            log.info("level %d: placed %d carved blocks (%d unplaceable, %d ambiguous)",
                     level, placed, unplaced, ambiguous)
            del idx

            if last:
                self._level1 = reads
                return cur, ref

            # --- expand to the next level
            nxt: dict[int, list[Cand]] = defaultdict(list)
            nkeys: dict[int, set] = defaultdict(set)
            span_next = 1 << (epb_shift * (level - 1))
            for pos, cands in cur.items():
                for c in cands:
                    if c.bp.is_hole:
                        # a hole at this level covers every child position
                        children = [(s, c.bp) for s in range(epb)]
                    else:
                        rd = reads.get(bp_key(c.bp))
                        if not rd or not rd.data:
                            continue
                        children = list(enumerate(blkptr.parse_array(rd.data)))
                    for s, child in children:
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
        """For every span, order candidates newest-first and pick an initial choice per slot:
        the child with the highest birth txg wins (ties go to the earlier, non-carved one).

        Yields (span, first_blkid, count, candidates_blob, choice, status, max_birth).
        """
        _, _, _, ibs = ref.geometry
        epb = 1 << (ibs - 7)
        summaries = self._level1
        for span in sorted(spans):
            cands = sorted(spans[span], key=lambda c: (-c.birth, c.carved))[:MAX_CANDIDATES]
            first = span * epb
            count = min(epb, self.maxblkid + 1 - first)
            if count <= 0:
                continue
            births = np.full((len(cands), count), -1, dtype=np.int64)
            kinds = np.zeros((len(cands), count), dtype=np.uint8)
            for ci, c in enumerate(cands):
                if c.bp.is_hole:
                    births[ci] = c.bp.birth
                    kinds[ci] = KIND_HOLE
                    continue
                sm = summaries.get(bp_key(c.bp))
                if isinstance(sm, Summary):
                    births[ci] = sm.births[:count]
                    kinds[ci] = sm.kinds[:count]
            best = births.argmax(axis=0)
            best_birth = births[best, np.arange(count)]
            best_kind = kinds[best, np.arange(count)]
            status = np.full(count, BlockStatus.UNKNOWN, dtype=np.uint8)
            status[best_kind == KIND_EMBEDDED] = BlockStatus.EMBEDDED
            status[(best_kind == KIND_HOLE) & (best_birth == 0)] = BlockStatus.HOLE
            status[(best_kind == KIND_HOLE) & (best_birth > 0)] = BlockStatus.DISCARDED
            none = best_birth < 0
            status[none] = BlockStatus.NO_METADATA
            choice = best.astype(np.uint8)
            choice[none] = NO_CHOICE
            blob = b"".join(c.bp.raw + bytes([1 if c.carved else 0]) for c in cands)
            yield (span, first, count, blob, choice.tobytes(), status.tobytes(),
                   int(best_birth.max()) if count else 0)


# ---------------------------------------------------------------------- compact helpers

KIND_HOLE, KIND_NORMAL, KIND_EMBEDDED = 0, 1, 2
_M63 = np.uint64((1 << 63) - 1)


def child_words(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<u8", count=len(data) // 8).reshape(-1, 16)


@dataclass
class Summary:
    """What reconstruction needs to remember about an indirect block: per child slot, the
    birth txg and whether it is a hole, a normal pointer or embedded data (about 5 KiB
    instead of the 128 KiB block)."""
    status: ReadStatus
    births: np.ndarray
    kinds: np.ndarray

    @property
    def data(self) -> None:          # duck-typing with BlockRead for status counting
        return None

    @classmethod
    def of(cls, words: np.ndarray, status: ReadStatus) -> Summary:
        prop = words[:, 6]
        emb = ((prop >> np.uint64(39)) & np.uint64(1)).astype(bool)
        hole = ~emb & (words[:, 0] == 0) & (words[:, 1] == 0)
        kinds = np.where(emb, KIND_EMBEDDED, np.where(hole, KIND_HOLE, KIND_NORMAL)).astype(np.uint8)
        return cls(status, words[:, 10].astype(np.uint32), kinds)


class ChildIndex:
    """Sorted index of (child DVA → slot, parent position) for placing orphan blocks."""

    def __init__(self) -> None:
        self._keys: list[np.ndarray] = []
        self._slots: list[np.ndarray] = []
        self._pos: list[np.ndarray] = []

    @staticmethod
    def _dva_keys(words: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        prop = words[:, 6]
        emb = ((prop >> np.uint64(39)) & np.uint64(1)).astype(bool)
        normal = ~emb & ((words[:, 0] != 0) | (words[:, 1] != 0))
        slots = np.nonzero(normal)[0]
        keys = ((words[slots, 0] >> np.uint64(32)) << np.uint64(56)) | ((words[slots, 1] & _M63) << np.uint64(9))
        return keys, slots

    def add(self, words: np.ndarray, pos: int) -> None:
        keys, slots = self._dva_keys(words)
        self._keys.append(keys)
        self._slots.append(slots.astype(np.uint16))
        self._pos.append(np.full(len(keys), pos, dtype=np.int64))

    def freeze(self) -> None:
        if self._keys:
            k = np.concatenate(self._keys)
            order = np.argsort(k, kind="stable")
            self.keys = k[order]
            self.slots = np.concatenate(self._slots)[order]
            self.pos = np.concatenate(self._pos)[order]
        else:
            self.keys = np.zeros(0, np.uint64)
            self.slots = np.zeros(0, np.uint16)
            self.pos = np.zeros(0, np.int64)
        self._keys = self._slots = self._pos = []

    def vote(self, words: np.ndarray) -> tuple[int | None, int, int]:
        """Return (best position, votes for it, total votes) for a block's children."""
        keys, slots = self._dva_keys(words)
        if not len(keys) or not len(self.keys):
            return None, 0, 0
        i = np.searchsorted(self.keys, keys)
        i = np.minimum(i, len(self.keys) - 1)
        hit = (self.keys[i] == keys) & (self.slots[i] == slots)
        if not hit.any():
            return None, 0, 0
        vals, counts = np.unique(self.pos[i[hit]], return_counts=True)
        j = int(counts.argmax())
        return int(vals[j]), int(counts[j]), int(counts.sum())
