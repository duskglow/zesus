"""ZPL: the POSIX filesystem layer of ZFS filesystem datasets.

Given an objset of type ZFS, this lists every file, directory and symlink with its
attributes, and can read file contents with gap accounting. It handles:

* system-attribute (SA) znodes, including spill blocks, and legacy ``znode_phys_t``;
* micro and fat ZAP directories;
* the unlinked set: files deleted while still open, whose data still exists;
* orphans: file objects present in the objset but unreachable from the root, which
  happens when directories are damaged or the dataset was torn down mid-destroy.

Data verification happens block by block through the checksum-verifying reader.
"""

from __future__ import annotations

import logging
import struct
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..fs.api import DIR, FILE, OTHER, SYMLINK, Entry
from ..map.codes import BlockStatus
from .constants import DmuType
from .dnode import Dnode, ObjectReader
from .objset import Objset
from .reader import ReadStatus
from .zap import read_zap

log = logging.getLogger(__name__)

SA_MAGIC = 0x2F505A
ZFS_DIRENT_OBJ_MASK = (1 << 48) - 1
S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0o170000, 0o040000, 0o100000, 0o120000
_TYPES = {S_IFDIR: DIR, S_IFREG: FILE, S_IFLNK: SYMLINK}

_STATUS_MAP = {ReadStatus.CHECKSUM_MISMATCH: BlockStatus.CKSUM_MISMATCH, ReadStatus.ZEROED: BlockStatus.ZEROED,
               ReadStatus.DECOMPRESS_FAILED: BlockStatus.DECOMPRESS_FAIL, ReadStatus.UNREADABLE: BlockStatus.UNREADABLE}


@dataclass
class ZGap:
    offset: int
    length: int
    status: BlockStatus


@dataclass
class Znode:
    obj: int
    mode: int = 0
    size: int = 0
    parent: int | None = None
    links: int = 0
    uid: int | None = None
    gid: int | None = None
    atime: int | None = None
    mtime: int | None = None
    ctime: int | None = None
    crtime: int | None = None
    flags: int = 0
    symlink: bytes | None = None
    extra: dict = field(default_factory=dict)


class ZplFilesystem:
    def __init__(self, os_: Objset, name: str = "") -> None:
        self.os = os_
        self.name = name
        self.r = os_.r
        self.master = read_zap(os_.object(1))
        self.root = self.master.get("ROOT")
        self._problems: list[tuple[str, str]] = []
        self.attr_by_num: dict[int, tuple[str, int]] = {}
        self.layouts: dict[int, list[int]] = {}
        sa = self.master.get("SA_ATTRS")
        if sa:
            try:
                sam = read_zap(os_.object(sa))
                for aname, v in read_zap(os_.object(sam["REGISTRY"])).items():
                    self.attr_by_num[v & 0xFFFF] = (aname, (v >> 24) & 0xFFFF)
                for k, v in read_zap(os_.object(sam["LAYOUTS"])).items():
                    self.layouts[int(k)] = v if isinstance(v, list) else [v]
            except Exception as exc:
                self._problems.append(("SA registry", f"unreadable: {exc}"))
        self._reached: set[int] = set()

    # ------------------------------------------------------------------ znodes
    def znode(self, obj: int) -> Znode | None:
        try:
            dn = self.os.dnode(obj)
        except Exception as exc:
            self._problems.append((f"object {obj}", f"dnode unreadable: {exc}"))
            return None
        if dn.is_free:
            return None
        if dn.bonustype == DmuType.SA:
            return self._sa_znode(obj, dn)
        if dn.bonustype == DmuType.ZNODE:
            return self._legacy_znode(obj, dn)
        return None

    def _sa_attrs(self, buf: bytes) -> dict[str, bytes]:
        if len(buf) < 8:
            return {}
        magic, info = struct.unpack_from("<IH", buf, 0)
        if magic != SA_MAGIC:
            return {}
        layout = info & 0x3FF
        hdrsz = ((info >> 10) & 0x3F) * 8
        attrs = self.layouts.get(layout)
        if attrs is None:
            return {}
        var_lens = list(struct.unpack_from(f"<{(hdrsz - 6) // 2}H", buf, 6)) if hdrsz > 6 else []
        out: dict[str, bytes] = {}
        off = hdrsz
        vi = 0
        for a in attrs:
            name, ln = self.attr_by_num.get(a, (f"attr{a}", 0))
            if ln == 0:
                if vi >= len(var_lens):
                    break
                ln = var_lens[vi]
                vi += 1
            if off + ln > len(buf):
                break          # continues in the spill block
            out[name] = buf[off:off + ln]
            off += (ln + 7) & ~7
        return out

    def _sa_znode(self, obj: int, dn: Dnode) -> Znode:
        a = self._sa_attrs(dn.bonus)
        if dn.spill is not None and not dn.spill.is_hole:
            rd = self.r.read(dn.spill)
            if rd.data:
                a = {**self._sa_attrs(rd.data), **a}
            else:
                self._problems.append((f"object {obj}", f"spill block unreadable ({rd.status.value})"))

        def u64(k: str) -> int | None:
            v = a.get(k)
            return struct.unpack_from("<Q", v)[0] if v and len(v) >= 8 else None

        return Znode(obj=obj, mode=u64("ZPL_MODE") or 0, size=u64("ZPL_SIZE") or 0, parent=u64("ZPL_PARENT"),
                     links=u64("ZPL_LINKS") or 0, uid=u64("ZPL_UID"), gid=u64("ZPL_GID"),
                     atime=u64("ZPL_ATIME"), mtime=u64("ZPL_MTIME"), ctime=u64("ZPL_CTIME"),
                     crtime=u64("ZPL_CRTIME"), flags=u64("ZPL_FLAGS") or 0, symlink=a.get("ZPL_SYMLINK"))

    def _legacy_znode(self, obj: int, dn: Dnode) -> Znode:
        b = dn.bonus
        if len(b) < 264:
            return Znode(obj=obj)
        f = struct.unpack_from("<8Q13Q", b, 0)
        z = Znode(obj=obj, atime=f[0], mtime=f[2], ctime=f[4], crtime=f[6], mode=f[9], size=f[10],
                  parent=f[11], links=f[12], flags=f[15], uid=f[16], gid=f[17])
        if (z.mode & S_IFMT) == S_IFLNK and z.size <= len(b) - 264:
            z.symlink = b[264:264 + z.size]
        return z

    # ------------------------------------------------------------------ listing
    def _entry(self, z: Znode, parent: int | None, name: str, path: str, deleted: bool = False) -> Entry:
        t = _TYPES.get(z.mode & S_IFMT, OTHER)
        e = Entry(inode=z.obj, parent_inode=parent, name=name, path=path, type=t, size=z.size, mode=z.mode,
                  uid=z.uid, gid=z.gid, atime=z.atime, mtime=z.mtime, ctime=z.ctime, crtime=z.crtime,
                  deleted=deleted)
        if t == SYMLINK:
            tgt = z.symlink
            if tgt is None:
                data, _ = self.read_object(z.obj, 0, min(z.size, 4096))
                tgt = data
            e.link_target = tgt[: z.size].decode("utf-8", "surrogateescape") if tgt is not None else None
        return e

    def dir_entries(self, obj: int) -> dict[str, tuple[int, int]]:
        raw = read_zap(self.os.object(obj))
        return {k: (v & ZFS_DIRENT_OBJ_MASK, v >> 60) for k, v in raw.items() if isinstance(v, int)}

    def iter_entries(self) -> Iterator[Entry]:
        if self.root is None:
            self._problems.append(("master node", "no ROOT entry"))
        else:
            rz = self.znode(self.root)
            if rz is not None:
                yield self._entry(rz, None, "", "/")
            q: deque[tuple[int, str]] = deque([(self.root, "")])
            self._reached.add(self.root)
            while q:
                dobj, dpath = q.popleft()
                try:
                    kids = self.dir_entries(dobj)
                except Exception as exc:
                    self._problems.append((dpath or "/", f"directory unreadable: {exc}"))
                    continue
                for name, (cobj, _t) in sorted(kids.items()):
                    z = self.znode(cobj)
                    path = f"{dpath}/{name}"
                    if z is None:
                        self._problems.append((path, f"znode {cobj} unreadable"))
                        continue
                    yield self._entry(z, dobj, name, path)
                    if (z.mode & S_IFMT) == S_IFDIR and cobj not in self._reached:
                        q.append((cobj, path))
                    self._reached.add(cobj)
        # files deleted while open (still on the unlinked set)
        dq = self.master.get("DELETE_QUEUE")
        if dq:
            try:
                for k in read_zap(self.os.object(dq)):
                    obj = int(k, 16) if isinstance(k, str) else int(k)
                    z = self.znode(obj)
                    if z and obj not in self._reached:
                        self._reached.add(obj)
                        yield self._entry(z, None, f"#{obj}", f"/$unlinked/#{obj}", deleted=True)
            except Exception as exc:
                self._problems.append(("unlinked set", f"unreadable: {exc}"))
        # orphans: file/dir objects nobody points to any more
        try:
            for obj, dn in self.os.iter_dnodes():
                if obj in self._reached or dn.type not in (DmuType.PLAIN_FILE_CONTENTS, DmuType.DIRECTORY_CONTENTS):
                    continue
                z = self.znode(obj)
                if z and z.mode:
                    yield self._entry(z, None, f"#{obj}", f"/$orphans/#{obj}")
        except Exception as exc:
            self._problems.append(("object set", f"scan for orphans failed: {exc}"))

    # ------------------------------------------------------------------ data
    def read_object(self, obj: int, offset: int, length: int) -> tuple[bytes, list[ZGap]]:
        dn = self.os.dnode(obj)
        rdr = ObjectReader(self.r, dn)
        bs = dn.datablksz
        out = bytearray()
        gaps: list[ZGap] = []
        pos, end = offset, offset + length
        while pos < end:
            blkid, inner = divmod(pos, bs)
            take = min(bs - inner, end - pos)
            rd = rdr.read_block(blkid)
            if rd.status in (ReadStatus.OK, ReadStatus.HOLE, ReadStatus.UNVERIFIED) and rd.data is not None:
                chunk = rd.data[inner:inner + take]
                out += chunk + b"\0" * (take - len(chunk))
            else:
                out += b"\0" * take
                st = _STATUS_MAP.get(rd.status, BlockStatus.UNREADABLE)
                if gaps and gaps[-1].offset + gaps[-1].length == pos and gaps[-1].status == st:
                    gaps[-1].length += take
                else:
                    gaps.append(ZGap(pos, take, st))
            pos += take
        return bytes(out), gaps

    def check_object(self, obj: int, size: int) -> tuple[int, list[ZGap]]:
        """Verify every block of a file without keeping data. Returns (lost bytes, gaps)."""
        gaps: list[ZGap] = []
        try:
            dn = self.os.dnode(obj)
        except Exception:
            return size, [ZGap(0, size, BlockStatus.NO_METADATA)]
        rdr = ObjectReader(self.r, dn)
        bs = dn.datablksz
        for blkid, bp, failed in rdr.iter_l0():
            start = blkid * bs
            if start >= size:
                break
            if failed is not None:
                span = rdr.span_of_level(1) * bs if dn.nlevels > 1 else bs
                gaps.append(ZGap(start, min(span, size - start), BlockStatus.NO_METADATA))
                continue
            if bp is None or bp.is_hole or bp.embedded:
                continue
            rd = self.r.read(bp, decompress=False)
            if rd.status not in (ReadStatus.OK, ReadStatus.UNVERIFIED):
                gaps.append(ZGap(start, min(bs, size - start), _STATUS_MAP.get(rd.status, BlockStatus.UNREADABLE)))
        return sum(g.length for g in gaps), gaps

    def problems(self) -> list[tuple[str, str]]:
        return self._problems
