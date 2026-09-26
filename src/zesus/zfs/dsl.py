"""DSL (dataset and snapshot layer): directories, datasets, snapshots and properties in the MOS.

Traversal is failure-tolerant. A damaged directory or dataset is recorded with an error,
and walking continues with its siblings.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import blkptr
from .blkptr import BlockPointer
from .constants import DmuType
from .objset import Objset
from .zap import read_zap

log = logging.getLogger(__name__)

DS_FLAG_INCONSISTENT = 1 << 0
DS_FLAG_NOPROMOTE = 1 << 1
DS_FLAG_UNIQUE_ACCURATE = 1 << 2
DS_FLAG_DEFER_DESTROY = 1 << 3
DS_FLAG_CI_DATASET = 1 << 16


@dataclass
class DslDir:
    obj: int
    creation_time: int
    head_dataset_obj: int
    parent_obj: int
    origin_obj: int
    child_dir_zapobj: int
    used_bytes: int
    props_zapobj: int
    flags: int


@dataclass
class DslDataset:
    obj: int
    dir_obj: int
    prev_snap_obj: int
    prev_snap_txg: int
    next_snap_obj: int
    snapnames_zapobj: int
    num_children: int
    creation_time: int
    creation_txg: int
    referenced_bytes: int
    unique_bytes: int
    fsid_guid: int
    guid: int
    flags: int
    bp: BlockPointer
    props_obj: int


def parse_dsl_dir(obj: int, bonus: bytes) -> DslDir:
    f = struct.unpack_from("<13Q", bonus, 0)
    return DslDir(obj, f[0], f[1], f[2], f[3], f[4], f[5], f[10], f[12])


def parse_dsl_dataset(obj: int, bonus: bytes, bo: str = "<") -> DslDataset:
    f = struct.unpack_from(bo + "16Q", bonus, 0)
    bp = blkptr.parse(bonus, 128, bo)
    props_obj = struct.unpack_from(bo + "Q", bonus, 264)[0] if len(bonus) >= 272 else 0
    return DslDataset(obj=obj, dir_obj=f[0], prev_snap_obj=f[1], prev_snap_txg=f[2], next_snap_obj=f[3],
                      snapnames_zapobj=f[4], num_children=f[5], creation_time=f[6], creation_txg=f[7],
                      referenced_bytes=f[9], unique_bytes=f[12], fsid_guid=f[13], guid=f[14],
                      flags=f[15], bp=bp, props_obj=props_obj)


@dataclass
class DatasetInfo:
    name: str
    dsobj: int
    dir_obj: int
    is_snapshot: bool
    ds: DslDataset | None
    props: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def objset_bp(self) -> BlockPointer | None:
        return self.ds.bp if self.ds else None


class Dsl:
    def __init__(self, mos: Objset, pool_name: str) -> None:
        self.mos = mos
        self.pool_name = pool_name
        self.objdir = read_zap(mos.object(1))

    def dir(self, obj: int) -> DslDir:
        d = self.mos.dnode(obj)
        if d.bonustype != DmuType.DSL_DIR:
            raise ValueError(f"MOS object {obj} is not a DSL dir (bonus type {d.bonustype})")
        return parse_dsl_dir(obj, d.bonus)

    def dataset(self, obj: int) -> DslDataset:
        d = self.mos.dnode(obj)
        if d.bonustype != DmuType.DSL_DATASET:
            raise ValueError(f"MOS object {obj} is not a DSL dataset (bonus type {d.bonustype})")
        return parse_dsl_dataset(obj, d.bonus, self.mos.byteorder)

    def props(self, zapobj: int) -> dict[str, Any]:
        if not zapobj:
            return {}
        try:
            return read_zap(self.mos.object(zapobj))
        except Exception as exc:
            log.debug("props zap %d unreadable: %s", zapobj, exc)
            return {}

    def walk(self) -> Iterator[DatasetInfo]:
        root = self.objdir.get("root_dataset")
        if root is None:
            raise ValueError("MOS object directory has no root_dataset")
        yield from self._walk_dir(root, self.pool_name, depth=0)

    def _walk_dir(self, dir_obj: int, name: str, depth: int) -> Iterator[DatasetInfo]:
        if depth > 64:
            return
        try:
            dd = self.dir(dir_obj)
        except Exception as exc:
            yield DatasetInfo(name, 0, dir_obj, False, None, error=f"dsl dir unreadable: {exc}")
            return
        props = self.props(dd.props_zapobj)
        if dd.head_dataset_obj:
            try:
                ds = self.dataset(dd.head_dataset_obj)
                yield DatasetInfo(name, dd.head_dataset_obj, dir_obj, False, ds, props)
                yield from self._snapshots(ds, name, dir_obj)
            except Exception as exc:
                yield DatasetInfo(name, dd.head_dataset_obj, dir_obj, False, None, props,
                                  error=f"dataset unreadable: {exc}")
        if dd.child_dir_zapobj:
            try:
                children = read_zap(self.mos.object(dd.child_dir_zapobj))
            except Exception as exc:
                log.warning("children of %s unreadable: %s", name, exc)
                children = {}
            for child, cobj in sorted(children.items()):
                if isinstance(child, str) and child.startswith("$"):
                    continue          # $MOS, $FREE, $ORIGIN, $LEAK
                yield from self._walk_dir(cobj, f"{name}/{child}", depth + 1)

    def _snapshots(self, head: DslDataset, name: str, dir_obj: int) -> Iterator[DatasetInfo]:
        if not head.snapnames_zapobj:
            return
        try:
            snaps = read_zap(self.mos.object(head.snapnames_zapobj))
        except Exception as exc:
            log.warning("snapshot list of %s unreadable: %s", name, exc)
            return
        for sname, sobj in sorted(snaps.items()):
            try:
                ds = self.dataset(sobj)
                yield DatasetInfo(f"{name}@{sname}", sobj, dir_obj, True, ds)
            except Exception as exc:
                yield DatasetInfo(f"{name}@{sname}", sobj, dir_obj, True, None, error=str(exc))
