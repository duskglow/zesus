"""ext2/ext3/ext4 filesystem plugin (read-only, damage-tolerant).

Supports:
* extents and legacy block maps;
* 64-bit, flex_bg and meta_bg layouts;
* inline data and fast symlinks;
* htree directories (read linearly);
* virtual journal replay (see :mod:`.journal`);
* discovery of orphaned inodes and of deleted inodes that still carry a block map.

Encrypted (fscrypt) files are listed but flagged, because their contents cannot be
decrypted without the keys.
"""

from __future__ import annotations

import logging
import struct
from collections import OrderedDict, deque
from collections.abc import Iterator

from ..api import (
    BLOCKDEV,
    CHARDEV,
    DIR,
    FIFO,
    FILE,
    OTHER,
    SOCKET,
    SYMLINK,
    Device,
    Entry,
    Extent,
    FilesystemPlugin,
    FsHandle,
    FsInfo,
)
from . import journal
from .structs import (
    BG_INODE_UNINIT,
    EXT4_ENCRYPT_FL,
    EXT4_EXTENTS_FL,
    EXT4_INLINE_DATA_FL,
    EXTENT_MAGIC,
    INCOMPAT_FILETYPE,
    INCOMPAT_META_BG,
    RO_COMPAT_GDT_CSUM,
    RO_COMPAT_METADATA_CSUM,
    RO_COMPAT_SPARSE_SUPER,
    S_IFBLK,
    S_IFCHR,
    S_IFDIR,
    S_IFIFO,
    S_IFLNK,
    S_IFREG,
    S_IFSOCK,
    SB_OFFSET,
    GroupDesc,
    Inode,
    Superblock,
    is_sparse_group_with_super,
    parse_gd,
    parse_inode,
    parse_superblock,
)

log = logging.getLogger(__name__)

ROOT_INO = 2
FT_MAP = {S_IFREG: FILE, S_IFDIR: DIR, S_IFLNK: SYMLINK, S_IFCHR: CHARDEV, S_IFBLK: BLOCKDEV,
          S_IFIFO: FIFO, S_IFSOCK: SOCKET}
MAX_EXTENT_DEPTH = 5


class Ext4Plugin(FilesystemPlugin):
    name = "ext4"
    description = "Linux ext2/ext3/ext4"

    def probe(self, dev: Device) -> float:
        sb = parse_superblock(dev.pread(SB_OFFSET, 1024))
        if sb is None:
            return 0.0
        if sb.blocks_count * sb.block_size > dev.size * 1.01 + (1 << 20):
            return 0.5          # superblock claims more space than the device has
        return 1.0

    def open(self, dev: Device) -> Ext4Handle:
        return Ext4Handle(dev)


class Ext4Handle(FsHandle):
    def __init__(self, dev: Device, *, replay_journal: bool = True) -> None:
        self.dev = dev
        raw, gaps = dev.read(SB_OFFSET, 1024)
        sb = parse_superblock(raw)
        if sb is None:
            raise ValueError("no ext superblock")
        self.sb: Superblock = sb
        self.bs = sb.block_size
        self._problems: list[tuple[str, str]] = []
        if gaps:
            self._problems.append(("superblock", "primary superblock partially unrecoverable"))
        self._blk_cache: OrderedDict[int, bytes] = OrderedDict()
        self.overlay = journal.Overlay()
        self._journal_extents: list[Extent] | None = None
        self.gds = self._read_gds()
        self._inodes: dict[int, Inode] = {}
        self._reached: set[int] = set()
        if replay_journal and sb.journal_inum and sb.feature_compat & 0x4:
            try:
                self._replay_journal()
            except Exception as exc:
                self._problems.append(("journal", f"replay failed: {exc}"))
                log.warning("ext4: journal replay failed: %s", exc)

    # ------------------------------------------------------------------ block I/O
    def _raw_read(self, blk: int, n: int = 1) -> tuple[bytes, list]:
        """Read n blocks without the journal overlay. Returns (data, gaps)."""
        return self.dev.read(blk * self.bs, n * self.bs)

    def _raw_block(self, blk: int, n: int = 1) -> tuple[bytes, bool]:
        data, gaps = self._raw_read(blk, n)
        return data, bool(gaps)

    def block(self, blk: int) -> bytes:
        """Read one fs block, with the journal overlay applied (metadata view)."""
        if blk in self._blk_cache:
            self._blk_cache.move_to_end(blk)
            return self._blk_cache[blk]
        ov = self.overlay.blocks.get(blk)
        if ov is not None:
            jdata = self._journal_block(ov[0])
            data = journal.unescape(jdata, ov[1]) if jdata else self._raw_block(blk)[0]
        else:
            data, damaged = self._raw_block(blk)
            if damaged:
                self._problems.append((f"block {blk}", "metadata block unrecoverable (read as zeros)"))
        self._blk_cache[blk] = data
        if len(self._blk_cache) > 4096:
            self._blk_cache.popitem(last=False)
        return data

    # ------------------------------------------------------------------ groups
    def _read_gds(self) -> list[GroupDesc]:
        sb = self.sb
        n = sb.group_count
        gsz = sb.gd_size
        per_block = self.bs // gsz
        out: list[GroupDesc] = []
        gdt_start = sb.first_data_block + 1
        if sb.feature_incompat & INCOMPAT_META_BG:
            first_meta = sb.first_meta_bg
        else:
            first_meta = n
        blocks_needed = -(-n // per_block)
        for bi in range(blocks_needed):
            if bi < first_meta:
                blk = gdt_start + bi
            else:
                g0 = bi * per_block
                blk = sb.first_data_block + g0 * sb.blocks_per_group + (1 if self._has_super(g0) else 0)
            data = self.block(blk)
            for i in range(per_block):
                g = bi * per_block + i
                if g >= n:
                    break
                out.append(parse_gd(data, i * gsz, gsz))
        return out

    def _has_super(self, g: int) -> bool:
        if not self.sb.feature_ro_compat & RO_COMPAT_SPARSE_SUPER:
            return True
        return is_sparse_group_with_super(g)

    # ------------------------------------------------------------------ inodes
    def inode(self, ino: int) -> Inode | None:
        if ino in self._inodes:
            return self._inodes[ino]
        sb = self.sb
        if ino < 1 or ino > sb.inodes_count:
            return None
        g, idx = divmod(ino - 1, sb.inodes_per_group)
        if g >= len(self.gds):
            return None
        off = idx * sb.inode_size
        blk = self.gds[g].inode_table + off // self.bs
        data = self.block(blk)
        return parse_inode(ino, data, off % self.bs, sb.inode_size)

    def _used_inode_range(self, g: int) -> int:
        """Number of inodes at the start of group *g* that could ever have been used."""
        gd = self.gds[g]
        ipg = self.sb.inodes_per_group
        if self.sb.feature_ro_compat & (RO_COMPAT_GDT_CSUM | RO_COMPAT_METADATA_CSUM):
            if gd.flags & BG_INODE_UNINIT:
                return 0
            return max(0, ipg - gd.itable_unused)
        return ipg

    def load_inodes(self) -> None:
        """Read the used part of every inode table, in large sequential reads."""
        sb = self.sb
        per_blk = self.bs // sb.inode_size
        for g, gd in enumerate(self.gds):
            used = self._used_inode_range(g)
            if not used:
                continue
            nblk = -(-used // per_blk)
            data, gaps = self._raw_read(gd.inode_table, nblk)
            # overlay: journaled copies of inode table blocks win
            if self.overlay.blocks:
                parts = bytearray(data)
                for k in range(nblk):
                    if gd.inode_table + k in self.overlay.blocks:
                        parts[k * self.bs:(k + 1) * self.bs] = self.block(gd.inode_table + k)
                data = bytes(parts)
            if gaps:
                self._problems.append((f"inode table of group {g}",
                                       f"{sum(x.length for x in gaps)} bytes unrecoverable; "
                                       "inodes stored there are unknown"))
            bad = [(x.offset - gd.inode_table * self.bs, x.length) for x in gaps]
            for i in range(used):
                off = i * sb.inode_size
                if any(a <= off < a + ln for a, ln in bad):
                    continue
                ino = g * sb.inodes_per_group + i + 1
                inode = parse_inode(ino, data, off, sb.inode_size)
                if inode.mode or inode.dtime:
                    self._inodes[ino] = inode
            if g and g % 1000 == 0:
                log.info("ext4: read inode tables of %d/%d groups (%d inodes so far)", g, len(self.gds),
                         len(self._inodes))

    # ------------------------------------------------------------------ block maps
    def file_blocks(self, inode: Inode) -> Iterator[tuple[int, int, int, bool]]:
        """Yield (logical block, physical block, count, unwritten) runs."""
        if inode.flags & EXT4_INLINE_DATA_FL:
            return
        if inode.flags & EXT4_EXTENTS_FL:
            yield from self._extent_node(inode.i_block, 0, inode.ino)
        else:
            yield from self._blockmap(inode)

    def _extent_node(self, node: bytes, depth_guard: int, ino: int) -> Iterator[tuple[int, int, int, bool]]:
        magic, entries, _max, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != EXTENT_MAGIC:
            self._problems.append((f"inode {ino}", "bad extent header"))
            return
        if depth_guard > MAX_EXTENT_DEPTH:
            self._problems.append((f"inode {ino}", "extent tree too deep"))
            return
        entries = min(entries, (len(node) - 12) // 12)
        for i in range(entries):
            off = 12 + i * 12
            if depth == 0:
                ee_block, ee_len, hi, lo = struct.unpack_from("<IHHI", node, off)
                unwritten = ee_len > 32768
                n = ee_len - 32768 if unwritten else ee_len
                yield ee_block, (hi << 32) | lo, n, unwritten
            else:
                _ei_block, lo, hi = struct.unpack_from("<IIH", node, off)
                child = (hi << 32) | lo
                data, damaged = self._raw_block(child) if child not in self.overlay.blocks else (self.block(child), False)
                if damaged:
                    self._problems.append((f"inode {ino}", f"extent index block {child} unrecoverable"))
                    continue
                yield from self._extent_node(data, depth_guard + 1, ino)

    def _blockmap(self, inode: Inode) -> Iterator[tuple[int, int, int, bool]]:
        ptrs = struct.unpack_from("<15I", inode.i_block, 0)
        per = self.bs // 4
        lblk = 0
        run_l = run_p = run_n = None

        def emit(lb: int, pb: int):
            nonlocal run_l, run_p, run_n
            if run_n and pb == run_p + run_n and lb == run_l + run_n:
                run_n += 1
                return None
            prev = (run_l, run_p, run_n, False) if run_n else None
            run_l, run_p, run_n = lb, pb, 1
            return prev

        def walk(blk: int, level: int):
            nonlocal lblk
            if level == 0:
                if blk:
                    r = emit(lblk, blk)
                    if r:
                        yield r
                lblk += 1
                return
            span = per ** level
            if not blk:
                lblk += span
                return
            data = self.block(blk)
            for p in struct.unpack_from(f"<{per}I", data, 0):
                yield from walk(p, level - 1)

        for i in range(12):
            yield from walk(ptrs[i], 0)
        for lvl, idx in ((1, 12), (2, 13), (3, 14)):
            if lblk * self.bs >= inode.size:
                break
            yield from walk(ptrs[idx], lvl)
        if run_n:
            yield run_l, run_p, run_n, False

    # ------------------------------------------------------------------ directories
    def read_file_bytes(self, inode: Inode, limit: int | None = None) -> bytes:
        size = inode.size if limit is None else min(inode.size, limit)
        if inode.flags & EXT4_INLINE_DATA_FL:
            return inode.i_block[:size]
        out = bytearray(size)
        for lb, pb, n, unwritten in self.file_blocks(inode):
            start = lb * self.bs
            if start >= size:
                continue
            if unwritten:
                continue
            for k in range(n):
                o = start + k * self.bs
                if o >= size:
                    break
                blk = self.block(pb + k)
                out[o:o + self.bs] = blk[: max(0, min(self.bs, size - o))]
        return bytes(out)

    def dir_entries(self, inode: Inode) -> Iterator[tuple[int, str, int]]:
        data = self.read_file_bytes(inode)
        filetype = bool(self.sb.feature_incompat & INCOMPAT_FILETYPE)
        for base in range(0, len(data), self.bs):
            off = base
            end = min(base + self.bs, len(data))
            while off + 8 <= end:
                ino, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, off)
                if rec_len < 8 or off + rec_len > end:
                    break
                if not filetype:
                    name_len |= ftype << 8
                    ftype = 0
                if ino and name_len and 8 + name_len <= rec_len:
                    name = data[off + 8:off + 8 + name_len].decode("utf-8", "surrogateescape")
                    if name not in (".", ".."):
                        yield ino, name, ftype
                off += rec_len

    # ------------------------------------------------------------------ journal
    def _replay_journal(self) -> None:
        jino = self.inode(self.sb.journal_inum)
        if jino is None or not jino.mode:
            self._problems.append(("journal", "journal inode unreadable"))
            return
        runs = list(self.file_blocks(jino))
        lmap: dict[int, int] = {}
        for lb, pb, n, _ in runs:
            for k in range(n):
                lmap[lb + k] = pb + k
        self._journal_map = lmap
        self.overlay = journal.replay(self._journal_block, len(lmap))
        for n in self.overlay.notes:
            log.info("ext4 journal: %s", n)
        # overlay may change group descriptors
        if self.overlay.blocks:
            self._blk_cache.clear()
            self.gds = self._read_gds()

    def _journal_block(self, n: int) -> bytes | None:
        pb = getattr(self, "_journal_map", {}).get(n)
        if pb is None:
            return None
        data, damaged = self._raw_block(pb)
        return None if damaged else data

    # ------------------------------------------------------------------ FsHandle API
    def info(self) -> FsInfo:
        sb = self.sb
        warnings = []
        if sb.needs_recovery:
            warnings.append("filesystem was not cleanly unmounted (needs_recovery)"
                            + (f"; journal replayed virtually: {self.overlay.transactions} transactions, "
                               f"{len(self.overlay.blocks)} blocks" if self.overlay.transactions else ""))
        return FsInfo(
            fstype="ext4" if sb.feature_incompat & 0x40 else ("ext3" if sb.feature_compat & 0x4 else "ext2"),
            label=sb.volume_name, uuid=sb.uuid, block_size=self.bs, size=sb.blocks_count * self.bs,
            details={"inodes": sb.inodes_count, "free_inodes": sb.free_inodes, "blocks": sb.blocks_count,
                     "free_blocks": sb.free_blocks, "groups": sb.group_count, "inode_size": sb.inode_size,
                     "features": sb.feature_names(), "last_mounted": sb.last_mounted,
                     "mtime": sb.mtime, "wtime": sb.wtime, "mkfs_time": sb.mkfs_time,
                     "journal": {"transactions": self.overlay.transactions,
                                 "blocks_overlaid": len(self.overlay.blocks),
                                 "seq": [self.overlay.first_seq, self.overlay.last_seq],
                                 "notes": self.overlay.notes}},
            warnings=warnings)

    def _entry(self, inode: Inode, parent: int | None, name: str, path: str, deleted: bool = False) -> Entry:
        t = FT_MAP.get(inode.ftype, OTHER)
        e = Entry(inode=inode.ino, parent_inode=parent, name=name, path=path, type=t, size=inode.size,
                  mode=inode.mode, uid=inode.uid, gid=inode.gid, atime=inode.atime, mtime=inode.mtime,
                  ctime=inode.ctime, crtime=inode.crtime, deleted=deleted)
        if inode.flags & EXT4_ENCRYPT_FL:
            e.extra["encrypted"] = True
        if deleted:
            e.extra["dtime"] = inode.dtime
        if t == SYMLINK:
            try:
                if inode.size < 60 and not inode.flags & EXT4_EXTENTS_FL and not inode.blocks:
                    e.link_target = inode.i_block[:inode.size].decode("utf-8", "surrogateescape")
                else:
                    e.link_target = self.read_file_bytes(inode, 4096).decode("utf-8", "surrogateescape")
            except Exception:
                pass
        return e

    def iter_entries(self) -> Iterator[Entry]:
        if not self._inodes:
            log.info("ext4: loading inode tables (%d groups)", len(self.gds))
            self.load_inodes()
            log.info("ext4: %d inodes with content", len(self._inodes))
        root = self._inodes.get(ROOT_INO) or self.inode(ROOT_INO)
        if root is None or root.ftype != S_IFDIR:
            self._problems.append(("root", "root directory inode unreadable"))
        else:
            yield self._entry(root, None, "", "/")
            queue: deque[tuple[int, str]] = deque([(ROOT_INO, "")])
            self._reached.add(ROOT_INO)
            ndirs = 0
            while queue:
                dino, dpath = queue.popleft()
                d = self._inodes.get(dino) or self.inode(dino)
                if d is None:
                    continue
                ndirs += 1
                if ndirs % 20000 == 0:
                    log.info("ext4: walked %d directories", ndirs)
                try:
                    children = list(self.dir_entries(d))
                except Exception as exc:
                    self._problems.append((dpath or "/", f"directory unreadable: {exc}"))
                    continue
                for cino, name, _ft in children:
                    ci = self._inodes.get(cino) or self.inode(cino)
                    if ci is None or not ci.mode:
                        self._problems.append((f"{dpath}/{name}", f"entry points to unreadable inode {cino}"))
                        continue
                    path = f"{dpath}/{name}"
                    yield self._entry(ci, dino, name, path)
                    if ci.ftype == S_IFDIR and cino not in self._reached:
                        self._reached.add(cino)
                        queue.append((cino, path))
                    else:
                        self._reached.add(cino)
        # orphans (in use but unreachable) and deleted inodes that still have data
        for ino, inode in sorted(self._inodes.items()):
            if ino in self._reached or ino < self.sb.first_ino and ino != ROOT_INO:
                continue
            if inode.in_use:
                yield self._entry(inode, None, f"#{ino}", f"/$orphans/#{ino}")
            elif inode.looks_deleted and inode.ftype in (S_IFREG, S_IFLNK, S_IFDIR):
                has_map = any(True for _ in self._safe_blocks(inode))
                if has_map:
                    yield self._entry(inode, None, f"#{ino}", f"/$deleted/#{ino}", deleted=True)

    def _safe_blocks(self, inode: Inode):
        try:
            yield from self.file_blocks(inode)
        except Exception:
            return

    def extents(self, entry: Entry) -> Iterator[Extent]:
        inode = self._inodes.get(entry.inode) or self.inode(entry.inode)
        if inode is None:
            return
        size = inode.size
        if entry.type not in (FILE, SYMLINK, DIR):
            return
        if inode.flags & EXT4_INLINE_DATA_FL or (entry.type == SYMLINK and entry.link_target is not None
                                                   and size < 60 and not inode.blocks):
            yield Extent(0, size, None, "inline", inode.i_block[:size])
            return
        pos = 0
        for lb, pb, n, unwritten in sorted(self._safe_blocks(inode)):
            start = lb * self.bs
            if start >= size:
                break
            if start > pos:
                yield Extent(pos, start - pos, None, "sparse")
            length = min(n * self.bs, size - start)
            yield Extent(start, length, None if unwritten else pb * self.bs,
                         "unwritten" if unwritten else "data")
            pos = start + length
        if pos < size:
            yield Extent(pos, size - pos, None, "sparse")

    def problems(self) -> list[tuple[str, str]]:
        return self._problems
