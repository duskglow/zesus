"""Translation from DVAs to physical reads.

Supported now: single ``disk``/``file`` top-level vdevs, and ``mirror`` (any available
child is read). RAIDZ and dRAID are recognized and reported as unsupported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..errors import Unsupported
from ..io.source import ReadOnlySource
from .constants import VDEV_LABEL_START_SIZE

log = logging.getLogger(__name__)


@dataclass
class LeafDevice:
    guid: int
    path: str
    source: ReadOnlySource

    def read(self, offset: int, length: int) -> bytes:
        return self.source.pread(VDEV_LABEL_START_SIZE + offset, length)

    def physical(self, offset: int) -> int:
        return VDEV_LABEL_START_SIZE + offset


@dataclass
class TopVdev:
    id: int
    type: str
    guid: int
    ashift: int
    asize: int
    children: list[LeafDevice] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    nparity: int = 0

    def readers(self) -> list[LeafDevice]:
        if self.type in ("raidz", "draid"):
            raise Unsupported(f"{self.type} vdevs are not supported yet (vdev {self.id})")
        return self.children


class VdevMap:
    """All top-level vdevs of a pool, with whichever leaf devices we have images for."""

    def __init__(self) -> None:
        self.top: dict[int, TopVdev] = {}

    @classmethod
    def from_config(cls, vdev_tree: dict[str, Any], leaves: dict[int, ReadOnlySource],
                    vdev_children: int = 1) -> VdevMap:
        """Build from one label's ``vdev_tree``.

        A label describes only its own top-level vdev. For multi-vdev pools, call
        :meth:`add_top` once per label from each device. *leaves* maps leaf guid to source.
        """
        m = cls()
        m.add_top(vdev_tree, leaves)
        return m

    def add_top(self, tree: dict[str, Any], leaves: dict[int, ReadOnlySource]) -> TopVdev:
        vid = tree.get("id", 0)
        top = self.top.get(vid)
        if top is None:
            top = TopVdev(id=vid, type=tree.get("type", "?"), guid=tree.get("guid", 0),
                          ashift=tree.get("ashift", 9), asize=tree.get("asize", 0),
                          nparity=tree.get("nparity", 0))
            self.top[vid] = top
        known = {c.guid for c in top.children}
        for leaf in _leaves(tree):
            g = leaf.get("guid", 0)
            if g in known:
                continue
            if g in leaves:
                top.children.append(LeafDevice(guid=g, path=leaf.get("path", "?"), source=leaves[g]))
                known.add(g)
            elif not any(m.get("guid") == g for m in top.missing):
                top.missing.append(leaf)
        return top

    def describe(self) -> list[str]:
        out = []
        for vid, t in sorted(self.top.items()):
            out.append(f"vdev {vid}: {t.type} ashift={t.ashift} asize={t.asize:#x} "
                       f"present={len(t.children)} missing={len(t.missing)}")
        return out


def _leaves(tree: dict[str, Any]) -> list[dict[str, Any]]:
    kids = tree.get("children")
    if not kids:
        return [tree]
    out: list[dict[str, Any]] = []
    for c in kids:
        out.extend(_leaves(c))
    return out
