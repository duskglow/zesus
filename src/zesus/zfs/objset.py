"""objset_phys_t: an object set (the MOS, a filesystem, or a zvol) and its dnodes."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from ..errors import CorruptStructure
from .blkptr import BlockPointer
from .constants import DNODE_SHIFT, DNODE_SIZE, DmuType, ObjsetType
from .dnode import Dnode, ObjectReader, parse_dnode
from .reader import BlockReader

log = logging.getLogger(__name__)

OS_TYPE_OFFSET = 512 + 192


@dataclass
class ObjsetPhys:
    meta_dnode: Dnode
    type: int
    flags: int
    userused: Dnode | None
    groupused: Dnode | None
    projectused: Dnode | None
    zil_header: bytes

    @property
    def type_name(self) -> str:
        try:
            return ObjsetType(self.type).name
        except ValueError:
            return f"type{self.type}"


def parse_objset(buf: bytes, byteorder: str = "<") -> ObjsetPhys:
    if len(buf) < 1024:
        raise CorruptStructure(f"objset too small ({len(buf)} bytes)")
    meta = parse_dnode(buf, 0, byteorder)
    if meta.type != DmuType.DNODE:
        raise CorruptStructure(f"objset meta-dnode has type {meta.type}, expected DNODE")
    os_type, os_flags = struct.unpack_from(byteorder + "QQ", buf, OS_TYPE_OFFSET)
    if os_type > ObjsetType.ANY:
        raise CorruptStructure(f"implausible objset type {os_type}")

    def extra(off: int) -> Dnode | None:
        if len(buf) < off + DNODE_SIZE:
            return None
        try:
            d = parse_dnode(buf, off, byteorder, strict=False)
        except CorruptStructure:
            return None
        return d if d.type else None

    return ObjsetPhys(meta_dnode=meta, type=os_type, flags=os_flags,
                      userused=extra(1024), groupused=extra(1536), projectused=extra(2048),
                      zil_header=buf[512:704])


class Objset:
    """An object set read through a verified root block pointer."""

    def __init__(self, reader: BlockReader, root: BlockPointer | bytes, name: str = "") -> None:
        self.r = reader
        self.name = name
        if isinstance(root, BlockPointer):
            self.root_bp: BlockPointer | None = root
            data = reader.read_ok(root)
            bo = "<" if root.little_endian else ">"
        else:
            self.root_bp = None
            data = root
            bo = "<"
        self.phys = parse_objset(data, bo)
        self.byteorder = bo
        self.meta = ObjectReader(reader, self.phys.meta_dnode)
        self._dnode_cache: dict[int, Dnode] = {}

    @property
    def type(self) -> int:
        return self.phys.type

    @property
    def max_object(self) -> int:
        return (self.phys.meta_dnode.maxblkid + 1) * (self.phys.meta_dnode.datablksz >> DNODE_SHIFT) - 1

    def dnode(self, obj: int) -> Dnode:
        if obj in self._dnode_cache:
            return self._dnode_cache[obj]
        off = obj << DNODE_SHIFT
        bs = self.phys.meta_dnode.datablksz
        blk = self.meta.read_block(off // bs)
        if blk.data is None:
            raise CorruptStructure(f"dnode block for object {obj} unreadable ({blk.status.value})")
        inner = off % bs
        d = parse_dnode(blk.data, inner, self.byteorder, strict=False)
        if d.extra_slots and inner + d.slots * DNODE_SIZE <= len(blk.data):
            d = parse_dnode(blk.data, inner, self.byteorder, strict=False)
        self._dnode_cache[obj] = d
        return d

    def object(self, obj: int) -> ObjectReader:
        d = self.dnode(obj)
        if d.is_free:
            raise CorruptStructure(f"object {obj} is free")
        return ObjectReader(self.r, d)

    def iter_dnodes(self):
        """Yield (object number, dnode) for all allocated objects. Unreadable dnode blocks
        are logged and skipped."""
        md = self.phys.meta_dnode
        per_block = md.datablksz >> DNODE_SHIFT
        for blkid in range(md.maxblkid + 1):
            r = self.meta.read_block(blkid)
            if r.data is None:
                log.warning("%s: dnode block %d unreadable (%s)", self.name, blkid, r.status.value)
                continue
            i = 0
            while i < per_block:
                obj = blkid * per_block + i
                try:
                    d = parse_dnode(r.data, i * DNODE_SIZE, self.byteorder, strict=False)
                except CorruptStructure:
                    i += 1
                    continue
                if d.type:
                    yield obj, d
                    i += max(1, d.slots)
                else:
                    i += 1
