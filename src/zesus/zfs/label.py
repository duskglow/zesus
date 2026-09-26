"""Vdev labels (4 per device) and the uberblock rings inside them."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

from ..io.source import ReadOnlySource
from . import blkptr, nvlist
from .checksum import verify_embedded
from .constants import (
    MAX_UBERBLOCK_SHIFT,
    MMP_MAGIC,
    UBERBLOCK_MAGIC,
    UBERBLOCK_SHIFT,
    VDEV_LABEL_SIZE,
    VDEV_LABELS,
    VDEV_PHYS_OFFSET,
    VDEV_PHYS_SIZE,
    VDEV_UBERBLOCK_RING_OFFSET,
    VDEV_UBERBLOCK_RING_SIZE,
)

log = logging.getLogger(__name__)


def label_offset(vdev_size: int, index: int) -> int:
    """Byte offset of label *index* (0..3). Labels 2 and 3 sit at the end of the device,
    aligned down to a label boundary as ZFS does."""
    if index < 2:
        return index * VDEV_LABEL_SIZE
    psize = vdev_size - vdev_size % VDEV_LABEL_SIZE
    return psize - VDEV_LABELS * VDEV_LABEL_SIZE + index * VDEV_LABEL_SIZE


@dataclass
class Uberblock:
    label: int
    slot: int
    offset: int                 # byte offset of the slot on the vdev
    txg: int
    guid_sum: int
    timestamp: int
    version: int
    rootbp: blkptr.BlockPointer
    software_version: int
    mmp: dict[str, int]
    checkpoint_txg: int
    checksum_ok: bool
    byteorder: str

    def key(self) -> tuple[int, int]:
        return (self.txg, self.timestamp)


@dataclass
class Label:
    index: int
    offset: int
    config: dict[str, Any] | None
    config_ok: bool
    uberblocks: list[Uberblock] = field(default_factory=list)
    error: str | None = None


def ub_slot_shift(ashift: int | None) -> int:
    return min(max(ashift or UBERBLOCK_SHIFT, UBERBLOCK_SHIFT), MAX_UBERBLOCK_SHIFT)


def parse_uberblock(buf: bytes, label: int, slot: int, offset: int) -> Uberblock | None:
    magic_le = struct.unpack_from("<Q", buf, 0)[0]
    if magic_le == UBERBLOCK_MAGIC:
        bo = "<"
    elif struct.unpack_from(">Q", buf, 0)[0] == UBERBLOCK_MAGIC:
        bo = ">"
    else:
        return None
    _m, version, txg, guid_sum, ts = struct.unpack_from(bo + "5Q", buf, 0)
    rootbp = blkptr.parse(buf, 40, bo)
    sw_ver, mmp_magic, mmp_delay, mmp_config, cp_txg = struct.unpack_from(bo + "5Q", buf, 168)
    mmp = {}
    if mmp_magic == MMP_MAGIC:
        mmp = {"delay": mmp_delay, "config": mmp_config}
    ok = verify_embedded(buf, (offset, 0, 0, 0))
    return Uberblock(label=label, slot=slot, offset=offset, txg=txg, guid_sum=guid_sum,
                     timestamp=ts, version=version, rootbp=rootbp, software_version=sw_ver,
                     mmp=mmp, checkpoint_txg=cp_txg, checksum_ok=ok, byteorder=bo)


def read_label(dev: ReadOnlySource, index: int, ashift_hint: int | None = None) -> Label:
    off = label_offset(dev.size, index)
    lab = Label(index=index, offset=off, config=None, config_ok=False)
    phys = dev.pread(off + VDEV_PHYS_OFFSET, VDEV_PHYS_SIZE)
    if len(phys) == VDEV_PHYS_SIZE:
        lab.config_ok = verify_embedded(phys, (off + VDEV_PHYS_OFFSET, 0, 0, 0))
        try:
            lab.config = nvlist.unpack(phys[:-40])
        except Exception as exc:
            lab.error = f"config nvlist: {exc}"
    ashift = ashift_hint
    if lab.config:
        ashift = lab.config.get("vdev_tree", {}).get("ashift", ashift)
    shift = ub_slot_shift(ashift)
    ring_off = off + VDEV_UBERBLOCK_RING_OFFSET
    ring = dev.pread(ring_off, VDEV_UBERBLOCK_RING_SIZE)
    slot_size = 1 << shift
    for slot in range(len(ring) // slot_size):
        buf = ring[slot * slot_size:(slot + 1) * slot_size]
        ub = parse_uberblock(buf, index, slot, ring_off + slot * slot_size)
        if ub:
            lab.uberblocks.append(ub)
    return lab


def read_labels(dev: ReadOnlySource) -> list[Label]:
    labels = [read_label(dev, i) for i in range(VDEV_LABELS)]
    # Labels without a readable config can still hold uberblocks. Re-read their rings with
    # the ashift from a good label.
    ashift = next((lb.config["vdev_tree"].get("ashift") for lb in labels
                   if lb.config and "vdev_tree" in lb.config), None)
    for i, lb in enumerate(labels):
        if not lb.config and ashift:
            labels[i] = read_label(dev, i, ashift)
            labels[i].error = lb.error
    return labels


def looks_like_vdev(dev: ReadOnlySource) -> bool:
    """Cheap check: does label 0 or 1 hold an XDR nvlist with a pool config?"""
    for i in (0, 1, 2, 3):
        try:
            off = label_offset(dev.size, i) + VDEV_PHYS_OFFSET
        except Exception:
            continue
        hdr = dev.pread(off, 4)
        if hdr[:2] == b"\x01\x01" or hdr[:2] == b"\x01\x00":
            return True
    return False
