"""ZFS on-disk constants (OpenZFS 2.x, pool version 5000 + feature flags)."""

from __future__ import annotations

from enum import IntEnum

SPA_MINBLOCKSHIFT = 9
SPA_MINBLOCKSIZE = 1 << SPA_MINBLOCKSHIFT
SPA_MAXBLOCKSIZE = 16 << 20

VDEV_LABEL_SIZE = 256 << 10
VDEV_LABELS = 4
VDEV_PAD_SIZE = 8 << 10
VDEV_PHYS_OFFSET = 16 << 10          # nvlist within a label
VDEV_PHYS_SIZE = 112 << 10
VDEV_UBERBLOCK_RING_OFFSET = 128 << 10
VDEV_UBERBLOCK_RING_SIZE = 128 << 10
VDEV_BOOT_SIZE = (4 << 20) - 2 * VDEV_LABEL_SIZE
VDEV_LABEL_START_SIZE = 2 * VDEV_LABEL_SIZE + VDEV_BOOT_SIZE   # 4 MiB: DVA offsets start here
VDEV_LABEL_END_SIZE = 2 * VDEV_LABEL_SIZE
UBERBLOCK_SHIFT = 10
MAX_UBERBLOCK_SHIFT = 13

UBERBLOCK_MAGIC = 0x00BAB10C
ZEC_MAGIC = 0x0210DA7AB10C7A11         # zio_eck_t magic (embedded checksum tail)
MMP_MAGIC = 0xA11CEA11

POOL_STATE = {0: "active", 1: "exported", 2: "destroyed", 3: "spare", 4: "l2cache",
              5: "uninitialized", 6: "unavail", 7: "potentially_active"}

DNODE_SHIFT = 9
DNODE_SIZE = 1 << DNODE_SHIFT
DNODE_CORE_SIZE = 64
DN_MAX_BONUSLEN = DNODE_SIZE - DNODE_CORE_SIZE - 128
DNODE_FLAG_USED_BYTES = 1 << 0
DNODE_FLAG_USERUSED_ACCOUNTED = 1 << 1
DNODE_FLAG_SPILL_BLKPTR = 1 << 2
BLKPTR_SIZE = 128
BLKPTR_SHIFT = 7

OBJSET_PHYS_SIZE_V1 = 1024
OBJSET_PHYS_SIZE_V2 = 2048
OBJSET_PHYS_SIZE_V3 = 4096

DMU_META_DNODE_OBJECT = 0
DMU_POOL_DIRECTORY_OBJECT = 1
ZVOL_OBJ = 1
ZVOL_ZAP_OBJ = 2

ZBT_LEAF = (1 << 63) + 0
ZBT_HEADER = (1 << 63) + 1
ZBT_MICRO = (1 << 63) + 3
ZAP_MAGIC = 0x2F52AB2AB
ZAP_LEAF_MAGIC = 0x2AB1EAF
MZAP_ENT_LEN = 64
MZAP_NAME_LEN = 50
ZAP_LEAF_CHUNKSIZE = 24
ZAP_CHUNK_FREE = 253
ZAP_CHUNK_ENTRY = 252
ZAP_CHUNK_ARRAY = 251
ZAP_LEAF_ARRAY_BYTES = 21


class ChecksumType(IntEnum):
    INHERIT = 0
    ON = 1
    OFF = 2
    LABEL = 3
    GANG_HEADER = 4
    ZILOG = 5
    FLETCHER_2 = 6
    FLETCHER_4 = 7
    SHA256 = 8
    ZILOG2 = 9
    NOPARITY = 10
    SHA512 = 11
    SKEIN = 12
    EDONR = 13
    BLAKE3 = 14


class Compression(IntEnum):
    INHERIT = 0
    ON = 1
    OFF = 2
    LZJB = 3
    EMPTY = 4
    GZIP_1 = 5
    GZIP_2 = 6
    GZIP_3 = 7
    GZIP_4 = 8
    GZIP_5 = 9
    GZIP_6 = 10
    GZIP_7 = 11
    GZIP_8 = 12
    GZIP_9 = 13
    ZLE = 14
    LZ4 = 15
    ZSTD = 16


class ObjsetType(IntEnum):
    NONE = 0
    META = 1
    ZFS = 2
    ZVOL = 3
    OTHER = 4
    ANY = 5


class DmuType(IntEnum):
    NONE = 0
    OBJECT_DIRECTORY = 1
    OBJECT_ARRAY = 2
    PACKED_NVLIST = 3
    PACKED_NVLIST_SIZE = 4
    BPOBJ = 5
    BPOBJ_HDR = 6
    SPACE_MAP_HEADER = 7
    SPACE_MAP = 8
    INTENT_LOG = 9
    DNODE = 10
    OBJSET = 11
    DSL_DIR = 12
    DSL_DIR_CHILD_MAP = 13
    DSL_DS_SNAP_MAP = 14
    DSL_PROPS = 15
    DSL_DATASET = 16
    ZNODE = 17
    OLDACL = 18
    PLAIN_FILE_CONTENTS = 19
    DIRECTORY_CONTENTS = 20
    MASTER_NODE = 21
    UNLINKED_SET = 22
    ZVOL = 23
    ZVOL_PROP = 24
    PLAIN_OTHER = 25
    UINT64_OTHER = 26
    ZAP_OTHER = 27
    ERROR_LOG = 28
    SPA_HISTORY = 29
    SPA_HISTORY_OFFSETS = 30
    POOL_PROPS = 31
    DSL_PERMS = 32
    ACL = 33
    SYSACL = 34
    FUID = 35
    FUID_SIZE = 36
    NEXT_CLONES = 37
    SCAN_QUEUE = 38
    USERGROUP_USED = 39
    USERGROUP_QUOTA = 40
    USERREFS = 41
    DDT_ZAP = 42
    DDT_STATS = 43
    SA = 44
    SA_MASTER_NODE = 45
    SA_ATTR_REGISTRATION = 46
    SA_ATTR_LAYOUTS = 47
    SCAN_XLATE = 48
    DEDUP = 49
    DEADLIST = 50
    DEADLIST_HDR = 51
    DSL_CLONES = 52
    BPOBJ_SUBOBJ = 53


DMU_OT_NUMTYPES = 54
DMU_OT_NEWTYPE = 0x80
DMU_OT_METADATA = 0x40
DMU_OT_ENCRYPTED = 0x20
DMU_OT_BYTESWAP_MASK = 0x1F


def dmu_type_name(t: int) -> str:
    if t & DMU_OT_NEWTYPE:
        flags = []
        if t & DMU_OT_METADATA:
            flags.append("meta")
        if t & DMU_OT_ENCRYPTED:
            flags.append("enc")
        return f"newtype({t & DMU_OT_BYTESWAP_MASK}{',' if flags else ''}{','.join(flags)})"
    try:
        return DmuType(t).name
    except ValueError:
        return f"type{t}"


def dmu_type_valid(t: int) -> bool:
    return t < DMU_OT_NUMTYPES or bool(t & DMU_OT_NEWTYPE and (t & DMU_OT_BYTESWAP_MASK) <= 9)


def dmu_type_is_metadata(t: int) -> bool:
    if t & DMU_OT_NEWTYPE:
        return bool(t & DMU_OT_METADATA)
    return t not in (DmuType.PLAIN_FILE_CONTENTS, DmuType.ZVOL, DmuType.PLAIN_OTHER,
                     DmuType.NONE, DmuType.OLDACL, DmuType.ACL)
