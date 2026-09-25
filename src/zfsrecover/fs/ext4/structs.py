"""ext2/3/4 on-disk structures."""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

EXT4_MAGIC = 0xEF53
SB_OFFSET = 1024

# feature flags
COMPAT_HAS_JOURNAL = 0x4
COMPAT_EXT_ATTR = 0x8
COMPAT_RESIZE_INODE = 0x10
COMPAT_DIR_INDEX = 0x20
INCOMPAT_FILETYPE = 0x2
INCOMPAT_RECOVER = 0x4
INCOMPAT_JOURNAL_DEV = 0x8
INCOMPAT_META_BG = 0x10
INCOMPAT_EXTENTS = 0x40
INCOMPAT_64BIT = 0x80
INCOMPAT_FLEX_BG = 0x200
INCOMPAT_INLINE_DATA = 0x8000
INCOMPAT_ENCRYPT = 0x10000
RO_COMPAT_SPARSE_SUPER = 0x1
RO_COMPAT_HUGE_FILE = 0x8
RO_COMPAT_GDT_CSUM = 0x10
RO_COMPAT_BIGALLOC = 0x200
RO_COMPAT_METADATA_CSUM = 0x400

# inode flags
EXT4_INDEX_FL = 0x1000
EXT4_HUGE_FILE_FL = 0x40000
EXT4_EXTENTS_FL = 0x80000
EXT4_EA_INODE_FL = 0x200000
EXT4_INLINE_DATA_FL = 0x10000000
EXT4_ENCRYPT_FL = 0x800

BG_INODE_UNINIT = 0x1
BG_BLOCK_UNINIT = 0x2

S_IFMT = 0o170000
S_IFSOCK, S_IFLNK, S_IFREG, S_IFBLK, S_IFDIR, S_IFCHR, S_IFIFO = (
    0o140000, 0o120000, 0o100000, 0o060000, 0o040000, 0o020000, 0o010000)

EXTENT_MAGIC = 0xF30A


@dataclass
class Superblock:
    inodes_count: int
    blocks_count: int
    free_blocks: int
    free_inodes: int
    first_data_block: int
    block_size: int
    blocks_per_group: int
    inodes_per_group: int
    mtime: int
    wtime: int
    state: int
    rev_level: int
    first_ino: int
    inode_size: int
    feature_compat: int
    feature_incompat: int
    feature_ro_compat: int
    uuid: str
    volume_name: str
    last_mounted: str
    journal_inum: int
    desc_size: int
    first_meta_bg: int
    mkfs_time: int
    log_groups_per_flex: int
    raw: bytes

    @property
    def group_count(self) -> int:
        return -(-(self.blocks_count - self.first_data_block) // self.blocks_per_group)

    @property
    def is_64bit(self) -> bool:
        return bool(self.feature_incompat & INCOMPAT_64BIT)

    @property
    def gd_size(self) -> int:
        return self.desc_size if self.is_64bit and self.desc_size >= 64 else 32

    @property
    def needs_recovery(self) -> bool:
        return bool(self.feature_incompat & INCOMPAT_RECOVER)

    def has(self, incompat: int = 0, ro: int = 0, compat: int = 0) -> bool:
        return bool((self.feature_incompat & incompat) or (self.feature_ro_compat & ro)
                    or (self.feature_compat & compat))

    def feature_names(self) -> list[str]:
        names = []
        for bits, table in ((self.feature_compat, {0x4: "has_journal", 0x8: "ext_attr", 0x10: "resize_inode",
                                                   0x20: "dir_index", 0x200: "sparse_super2"}),
                            (self.feature_incompat, {0x2: "filetype", 0x4: "needs_recovery", 0x10: "meta_bg",
                                                     0x40: "extent", 0x80: "64bit", 0x100: "mmp",
                                                     0x200: "flex_bg", 0x400: "ea_inode", 0x4000: "large_dir",
                                                     0x8000: "inline_data", 0x10000: "encrypt",
                                                     0x20000: "casefold"}),
                            (self.feature_ro_compat, {0x1: "sparse_super", 0x2: "large_file", 0x8: "huge_file",
                                                      0x10: "uninit_bg", 0x20: "dir_nlink", 0x40: "extra_isize",
                                                      0x100: "quota", 0x200: "bigalloc", 0x400: "metadata_csum",
                                                      0x2000: "project"})):
            for bit, n in table.items():
                if bits & bit:
                    names.append(n)
        return names


def parse_superblock(b: bytes) -> Superblock | None:
    if len(b) < 1024 or struct.unpack_from("<H", b, 0x38)[0] != EXT4_MAGIC:
        return None
    u = lambda off: struct.unpack_from("<I", b, off)[0]  # noqa: E731
    h = lambda off: struct.unpack_from("<H", b, off)[0]  # noqa: E731
    log_bs = u(0x18)
    if log_bs > 6:
        return None
    inc = u(0x60)
    blocks = u(0x04) | ((u(0x150) << 32) if inc & INCOMPAT_64BIT else 0)
    free = u(0x0C) | ((u(0x158) << 32) if inc & INCOMPAT_64BIT else 0)
    rev = u(0x4C)
    sb = Superblock(
        inodes_count=u(0x00), blocks_count=blocks, free_blocks=free, free_inodes=u(0x10),
        first_data_block=u(0x14), block_size=1024 << log_bs, blocks_per_group=u(0x20),
        inodes_per_group=u(0x28), mtime=u(0x2C), wtime=u(0x30), state=h(0x3A), rev_level=rev,
        first_ino=u(0x54) if rev else 11, inode_size=h(0x58) if rev else 128,
        feature_compat=u(0x5C), feature_incompat=inc, feature_ro_compat=u(0x64),
        uuid=str(uuid.UUID(bytes=bytes(b[0x68:0x78]))),
        volume_name=b[0x78:0x88].split(b"\0", 1)[0].decode("utf-8", "replace"),
        last_mounted=b[0x88:0xC8].split(b"\0", 1)[0].decode("utf-8", "replace"),
        journal_inum=u(0xE0), desc_size=h(0xFE), first_meta_bg=u(0x104), mkfs_time=u(0x108),
        log_groups_per_flex=b[0x174], raw=bytes(b[:1024]))
    if not sb.blocks_per_group or not sb.inodes_per_group or sb.inode_size < 128:
        return None
    return sb


@dataclass
class GroupDesc:
    block_bitmap: int
    inode_bitmap: int
    inode_table: int
    free_blocks: int
    free_inodes: int
    used_dirs: int
    flags: int
    itable_unused: int


def parse_gd(b: bytes, off: int, size: int) -> GroupDesc:
    lo = struct.unpack_from("<IIIHHHHIHHHH", b, off)
    bb, ib, it, fb, fi, ud, fl = lo[0], lo[1], lo[2], lo[3], lo[4], lo[5], lo[6]
    unused = lo[10]
    if size >= 64:
        hi = struct.unpack_from("<IIIHHHH", b, off + 0x20)
        bb |= hi[0] << 32
        ib |= hi[1] << 32
        it |= hi[2] << 32
        fb |= hi[3] << 16
        fi |= hi[4] << 16
        ud |= hi[5] << 16
        unused |= hi[6] << 16
    return GroupDesc(bb, ib, it, fb, fi, ud, fl, unused)


@dataclass
class Inode:
    ino: int
    mode: int
    uid: int
    gid: int
    size: int
    atime: int
    ctime: int
    mtime: int
    dtime: int
    crtime: int | None
    links: int
    blocks: int
    flags: int
    i_block: bytes
    file_acl: int
    generation: int
    raw: bytes

    @property
    def ftype(self) -> int:
        return self.mode & S_IFMT

    @property
    def in_use(self) -> bool:
        return self.mode != 0 and self.links > 0 and self.dtime == 0

    @property
    def looks_deleted(self) -> bool:
        return self.mode != 0 and self.dtime != 0


def parse_inode(ino: int, b: bytes, off: int, inode_size: int) -> Inode:
    r = b[off:off + inode_size]
    (mode, uid_lo, size_lo, atime, ctime, mtime, dtime, gid_lo, links, blocks_lo, flags) = \
        struct.unpack_from("<HHIIIIIHHII", r, 0)
    i_block = bytes(r[0x28:0x64])
    generation, acl_lo, size_hi = struct.unpack_from("<III", r, 0x64)
    blocks_hi, acl_hi, uid_hi, gid_hi = struct.unpack_from("<HHHH", r, 0x74)
    crtime = None
    if inode_size > 128:
        extra = struct.unpack_from("<H", r, 0x80)[0]
        if extra >= 0x18 and 0x94 <= 128 + extra:
            crtime = struct.unpack_from("<I", r, 0x90)[0]
    return Inode(ino=ino, mode=mode, uid=uid_lo | (uid_hi << 16), gid=gid_lo | (gid_hi << 16),
                 size=size_lo | (size_hi << 32), atime=atime, ctime=ctime, mtime=mtime, dtime=dtime,
                 crtime=crtime, links=links, blocks=blocks_lo | (blocks_hi << 32), flags=flags,
                 i_block=i_block, file_acl=acl_lo | (acl_hi << 32), generation=generation, raw=bytes(r))


def is_sparse_group_with_super(g: int) -> bool:
    if g in (0, 1):
        return True
    for base in (3, 5, 7):
        n = base
        while n < g:
            n *= base
        if n == g:
            return True
    return False
