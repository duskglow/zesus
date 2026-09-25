"""Pool discovery: find ZFS vdevs in a source, read labels, and build a block reader."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..errors import ZfsRecoverError
from ..io.source import ReadOnlySource, SliceSource
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
            found.append(VdevImage(sl, p.start, p, labels))
    if not found and looks_like_vdev(src):
        labels = read_labels(src)
        found.append(VdevImage(src, 0, None, labels))
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


def open_pools(src: ReadOnlySource) -> list[Pool]:
    images = find_vdevs(src)
    by_pool: dict[int, list[VdevImage]] = {}
    for im in images:
        if im.pool_guid is not None:
            by_pool.setdefault(im.pool_guid, []).append(im)
        else:
            log.warning("vdev at %#x has no readable config; skipping", im.base_offset)
    pools = []
    for pguid, ims in by_pool.items():
        cfg = ims[0].config or {}
        vm = VdevMap()
        leaves = {im.guid: im.source for im in ims if im.guid is not None}
        for im in ims:
            if im.config and "vdev_tree" in im.config:
                vm.add_top(im.config["vdev_tree"], leaves)
        n_children = cfg.get("vdev_children", 1)
        if len(vm.top) < n_children:
            log.warning("pool %s has %d top-level vdevs but only %d are present in this source; "
                        "blocks on missing vdevs will be reported unreadable",
                        cfg.get("name"), n_children, len(vm.top))
        ubs: dict[int, Uberblock] = {}
        for im in ims:
            for lb in im.labels:
                for u in lb.uberblocks:
                    cur = ubs.get(u.txg)
                    if cur is None or (u.checksum_ok and not cur.checksum_ok):
                        ubs[u.txg] = u
        pool = Pool(name=cfg.get("name", "?"), guid=pguid, config=cfg, vdev_images=ims,
                    vdevs=vm, reader=BlockReader(vm),
                    uberblocks=sorted(ubs.values(), key=lambda u: u.txg, reverse=True))
        for line in vm.describe():
            log.info("pool %s: %s", pool.name, line)
        pools.append(pool)
    return pools
