"""Pool discovery: find ZFS vdevs in a source, read labels, and build a block reader."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import ZfsRecoverError
from ..io.source import ReadOnlySource, SliceSource
from ..io.sourceset import SourceSet, as_sources
from ..partitions import Partition, detect
from ..partitions.gpt import ZFS_TYPES
from .label import Label, Uberblock, looks_like_vdev, read_labels
from .reader import BlockReader
from .vdev import VdevMap

log = logging.getLogger(__name__)


@dataclass
class VdevImage:
    """One ZFS leaf device found inside the source (a partition, or the whole source)."""
    source: ReadOnlySource            # a slice covering exactly the vdev
    base_offset: int                  # byte offset of the vdev within the evidence source
    partition: Partition | None
    labels: list[Label]
    evidence: ReadOnlySource | None = None     # the evidence file this vdev was found in
    duplicate_of: VdevImage | None = None      # same member guid seen in an earlier file
    duplicate_note: str = ""

    @property
    def config(self) -> dict[str, Any] | None:
        good = [lb.config for lb in self.labels if lb.config and lb.config_ok]
        anycfg = [lb.config for lb in self.labels if lb.config]
        return (good or anycfg or [None])[0]

    @property
    def guid(self) -> int | None:
        c = self.config
        return c.get("guid") if c else None

    @property
    def pool_guid(self) -> int | None:
        c = self.config
        return c.get("pool_guid") if c else None

    @property
    def max_txg(self) -> int:
        """Newest txg this member saw: its best uberblock, else its label txg."""
        ubs = [u.txg for lb in self.labels for u in lb.uberblocks if u.checksum_ok]
        if ubs:
            return max(ubs)
        return (self.config or {}).get("txg", 0)

    @property
    def child_id(self) -> int | None:
        """Position of this leaf among its top-level vdev's children (RAIDZ column)."""
        c = self.config or {}
        tree, g = c.get("vdev_tree", {}), c.get("guid")
        for i, ch in enumerate(tree.get("children") or []):
            if ch.get("guid") == g or any(x.get("guid") == g for x in ch.get("children") or []):
                return ch.get("id", i)
        return 0 if not tree.get("children") else None

    @property
    def name(self) -> str:
        return (self.evidence or self.source).name


def find_vdevs(src: ReadOnlySource) -> list[VdevImage]:
    """Locate ZFS vdevs: in ZFS-typed partitions first, then any partition, then the whole
    source."""
    scheme, parts = detect(src)
    log.info("partition table: %s (%d partitions)", scheme or "none", len(parts))
    for p in parts:
        log.info("  part %d: start=%#x len=%#x type=%s name=%r", p.index, p.start, p.length,
                 p.type_name, p.name)
    found: list[VdevImage] = []
    ordered = sorted(parts, key=lambda p: (p.type_id not in ZFS_TYPES and p.type_id != "0xbf", p.index))
    for p in ordered:
        if p.start + p.length > src.size:
            log.warning("partition %d extends beyond end of source; truncating", p.index)
        length = min(p.length, src.size - p.start)
        if length < (8 << 20):
            continue
        sl = SliceSource(src, p.start, length, f"{src.name}#p{p.index}")
        if looks_like_vdev(sl):
            labels = read_labels(sl)
            log.info("ZFS vdev found in partition %d (%d/4 labels readable)", p.index,
                     sum(1 for lb in labels if lb.config))
            found.append(VdevImage(sl, p.start, p, labels, src))
    if not found and looks_like_vdev(src):
        labels = read_labels(src)
        found.append(VdevImage(src, 0, None, labels, src))
        log.info("ZFS vdev found at start of source (no partition)")
    return found


@dataclass
class Pool:
    name: str
    guid: int
    config: dict[str, Any]
    vdev_images: list[VdevImage]
    vdevs: VdevMap
    reader: BlockReader
    uberblocks: list[Uberblock] = field(default_factory=list)   # unique by txg, newest first
    duplicates: list[VdevImage] = field(default_factory=list)   # second copies of a member

    @property
    def ashift(self) -> int:
        return self.config.get("vdev_tree", {}).get("ashift", 9)

    @property
    def max_txg(self) -> int:
        return max((u.txg for u in self.uberblocks), default=self.config.get("txg", 0))

    def best_uberblock(self) -> Uberblock:
        for u in self.uberblocks:
            if u.checksum_ok:
                return u
        raise ZfsRecoverError("no uberblock with a valid checksum")


def open_pools(src: ReadOnlySource | SourceSet | Sequence[ReadOnlySource]) -> list[Pool]:
    """Find every pool whose vdevs appear in *src*: one evidence file, or one per member.

    Members are matched to their place in the pool by the guid in their own label. The
    config is taken from the member that saw the newest txg. A second file carrying the
    same member guid is kept aside as a duplicate (reported, never read).
    """
    images: list[VdevImage] = []
    for s in as_sources(src):
        images.extend(find_vdevs(s))
    by_pool: dict[int, list[VdevImage]] = {}
    for im in images:
        if im.pool_guid is not None:
            by_pool.setdefault(im.pool_guid, []).append(im)
        else:
            log.warning("vdev at %#x in %s has no readable config; skipping", im.base_offset, im.name)
    pools = []
    for pguid, ims in by_pool.items():
        first: dict[int, VdevImage] = {}
        for im in ims:
            if im.guid in first:
                im.duplicate_of = first[im.guid]
                im.duplicate_note = _compare(first[im.guid], im)
                log.warning("%s is the same member (guid %d) as %s (%s); using the first",
                            im.name, im.guid, first[im.guid].name, im.duplicate_note)
            elif im.guid is not None:
                first[im.guid] = im
        use = [im for im in ims if im.duplicate_of is None]
        newest = max(use, key=lambda im: (im.max_txg, (im.config or {}).get("txg", 0)))
        cfg = newest.config or {}
        vm = VdevMap()
        leaves = {im.guid: im.source for im in use if im.guid is not None}
        seen_top: set[int] = set()
        for im in sorted(use, key=lambda im: -im.max_txg):
            tree = (im.config or {}).get("vdev_tree")
            if tree is not None and tree.get("id", 0) not in seen_top:
                seen_top.add(tree.get("id", 0))
                vm.add_top(tree, leaves)
        n_children = cfg.get("vdev_children", 1)
        if len(vm.top) < n_children:
            log.warning("pool %s has %d top-level vdevs but only %d are present in these sources; "
                        "blocks on missing vdevs will be reported unreadable",
                        cfg.get("name"), n_children, len(vm.top))
        ubs: dict[int, Uberblock] = {}
        for im in use:
            for lb in im.labels:
                for u in lb.uberblocks:
                    cur = ubs.get(u.txg)
                    if cur is None or (u.checksum_ok and not cur.checksum_ok):
                        ubs[u.txg] = u
        pool = Pool(name=cfg.get("name", "?"), guid=pguid, config=cfg, vdev_images=use,
                    vdevs=vm, reader=BlockReader(vm),
                    uberblocks=sorted(ubs.values(), key=lambda u: u.txg, reverse=True),
                    duplicates=[im for im in ims if im.duplicate_of is not None])
        for line in vm.describe():
            log.info("pool %s: %s", pool.name, line)
        pools.append(pool)
    return pools


def _compare(a: VdevImage, b: VdevImage, samples: int = 8, length: int = 1 << 16) -> str:
    """Cheaply compare two copies of one member: labels plus a few sampled regions."""
    size = min(a.source.size, b.source.size)
    if a.source.size != b.source.size:
        return f"sizes differ ({a.source.size} vs {b.source.size})"
    offs = [lb.offset for lb in a.labels] + [size * i // (samples + 1) for i in range(1, samples + 1)]
    for o in offs:
        if a.source.pread(o, length) != b.source.pread(o, length):
            return f"contents differ at {o:#x}"
    return f"identical at {len(offs)} sampled regions"
