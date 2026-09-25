"""ZFS primitives: nvlists, block pointers, checksums, compression, ZAP, classification."""

from __future__ import annotations

import hashlib
import os
import struct

import lz4.block
import numpy as np
import pytest

from zfsrecover.carve.classify import PoolLimits, classify
from zfsrecover.carve.fastsum import fletcher4_many
from zfsrecover.zfs import blkptr, checksum, compress, nvlist, zap
from zfsrecover.zfs.constants import ChecksumType, Compression, DmuType

# ---------------------------------------------------------------- nvlist encoders (test-only)


def _xdr_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">I", len(b)) + b + b"\0" * ((-len(b)) % 4)


def xdr_nvlist(d: dict) -> bytes:
    out = struct.pack(">II", 0, 1)
    for k, v in d.items():
        name = _xdr_str(k)
        if isinstance(v, bool):
            body = struct.pack(">II", 21, 1) + struct.pack(">i", int(v))
        elif isinstance(v, int):
            body = struct.pack(">II", 8, 1) + struct.pack(">Q", v)
        elif isinstance(v, str):
            body = struct.pack(">II", 9, 1) + _xdr_str(v)
        elif isinstance(v, dict):
            body = struct.pack(">II", 19, 1) + xdr_nvlist(v)
        elif isinstance(v, list):
            body = struct.pack(">II", 20, len(v)) + b"".join(xdr_nvlist(x) for x in v)
        else:
            raise TypeError(v)
        pair = name + body
        out += struct.pack(">II", 8 + len(pair), 0) + pair
    return out + struct.pack(">II", 0, 0)


def native_pairs(d: dict) -> bytes:
    out = b""
    for k, v in d.items():
        name = k.encode() + b"\0"
        hdr_name = name + b"\0" * ((-(16 + len(name))) % 8)
        if isinstance(v, int):
            typ, val, tail = 8, struct.pack("<Q", v), b""
        elif isinstance(v, str):
            s = v.encode() + b"\0"
            typ, val, tail = 9, s + b"\0" * ((-len(s)) % 8), b""
        elif isinstance(v, dict):
            typ, val, tail = 19, b"\0" * 24, native_pairs(v)
        else:
            raise TypeError(v)
        size = 16 + len(hdr_name) + len(val)
        out += struct.pack("<ihhii", size, len(name), 0, 1, typ) + hdr_name + val + tail
    return out + struct.pack("<i", 0)


def test_xdr_roundtrip():
    d = {"name": "tank", "txg": 1 << 40, "flag": True,
         "vdev_tree": {"type": "disk", "ashift": 12, "children": [{"guid": 1}, {"guid": 2}]}}
    buf = b"\x01\x01\x00\x00" + xdr_nvlist(d)
    assert nvlist.unpack(buf) == d


def test_native_with_embedded_list():
    d = {"ioctl": "reopen", "in_nvl": {"scrub_restart": 0}, "history time": 1790304415}
    buf = b"\x00\x01\x00\x00" + struct.pack("<ii", 0, 1) + native_pairs(d)
    assert nvlist.unpack(buf) == d


# ---------------------------------------------------------------- checksums


def ref_fletcher4(data: bytes) -> tuple:
    a = b = c = d = 0
    m = (1 << 64) - 1
    for (w,) in struct.iter_unpack("<I", data):
        a = (a + w) & m
        b = (b + a) & m
        c = (c + b) & m
        d = (d + c) & m
    return (a, b, c, d)


def test_fletcher4_matches_reference():
    data = os.urandom(4096)
    assert checksum.fletcher4(data) == ref_fletcher4(data)


def test_fletcher4_many_matches_single():
    buf = os.urandom(64 * 1024)
    offs = np.array([0, 4096, 8192, 60000 - 60000 % 4], dtype=np.int64)
    sizes = np.array([4096, 512, 16384, 4096], dtype=np.int64)
    got = fletcher4_many(buf, offs, sizes)
    for i in range(len(offs)):
        assert tuple(int(x) for x in got[i]) == checksum.fletcher4(buf[offs[i]:offs[i] + sizes[i]])


def test_sha256_word_order():
    data = b"abc" * 100
    words = checksum.sha256(data)
    assert struct.pack(">4Q", *words) == hashlib.sha256(data).digest()


def test_embedded_checksum_roundtrip():
    buf = bytearray(os.urandom(4096))
    struct.pack_into("<Q", buf, 4096 - 40, 0x0210DA7AB10C7A11)
    struct.pack_into("<4Q", buf, 4096 - 32, 0x20000, 0, 0, 0)
    ck = checksum.sha256(bytes(buf))
    struct.pack_into("<4Q", buf, 4096 - 32, *ck)
    assert checksum.verify_embedded(bytes(buf), (0x20000, 0, 0, 0))
    assert not checksum.verify_embedded(bytes(buf), (0x21000, 0, 0, 0))


# ---------------------------------------------------------------- compression


def test_lz4_zfs_framing():
    data = (b"zfs recovery " * 2000)[:16384]
    c = lz4.block.compress(data, store_size=False)
    framed = struct.pack(">I", len(c)) + c
    framed += b"\0" * ((-len(framed)) % 512)
    assert compress.decompress(Compression.LZ4, framed, 16384) == data


def test_zle():
    # 3 literal bytes, then 100 zeros (encoded as length > 64), then 1 literal
    enc = bytes([2]) + b"abc" + bytes([64 + 100 - 1]) + bytes([0]) + b"z"
    out = compress.zle(enc, 104)
    assert out == b"abc" + b"\0" * 100 + b"z"


def test_lzjb_literals_only():
    data = b"ABCDEFGH" * 4
    enc = bytearray()
    for i in range(0, len(data), 8):
        enc.append(0)           # copymap: 8 literals
        enc += data[i:i + 8]
    assert compress.lzjb(bytes(enc), len(data)) == data


def test_gzip():
    import zlib
    data = os.urandom(100) * 50
    assert compress.decompress(Compression.GZIP_6, zlib.compress(data, 6), len(data)) == data


# ---------------------------------------------------------------- block pointers & classification


def mkbp(offset: int, birth: int, level: int = 0, typ: int = DmuType.ZVOL, psize: int = 4096) -> blkptr.BlockPointer:
    return blkptr.build(vdev=0, offset=offset, asize=psize, psize=psize, lsize=16384, comp=Compression.LZ4,
                        cksum_type=ChecksumType.FLETCHER_4, type=typ, level=level, birth=birth,
                        cksum=(1, 2, 3, 4))


def test_blkptr_roundtrip():
    bp = mkbp(0x123456000, 777, level=1)
    p = blkptr.parse(bp.raw)
    assert p.dvas[0].offset == 0x123456000 and p.dvas[0].asize == 4096
    assert (p.lsize, p.psize, p.comp, p.cksum_type, p.type, p.level, p.birth) == \
        (16384, 4096, Compression.LZ4, ChecksumType.FLETCHER_4, DmuType.ZVOL, 1, 777)
    assert not p.is_hole and p.little_endian


def test_hole():
    assert blkptr.parse(b"\0" * 128).is_hole


LIM = PoolLimits(n_vdevs=1, vdev_asize=1 << 40, max_txg=10_000)


def test_classify_indirect():
    bps = b"".join(mkbp(0x1000 * (i + 1), 100 + i).raw for i in range(200)) + b"\0" * (128 * (1024 - 200))
    c = classify(bps, LIM)
    assert c is not None and c.kind == "indirect"
    assert (c.child_type, c.child_level, c.n_children) == (DmuType.ZVOL, 0, 200)
    assert (c.min_birth, c.max_birth) == (100, 299)


def test_classify_rejects_mixed_levels_and_noise():
    mixed = mkbp(0x1000, 5, level=0).raw + mkbp(0x2000, 5, level=1).raw + b"\0" * (128 * 1022)
    assert classify(mixed, LIM) is None
    assert classify(os.urandom(128 * 1024), LIM) is None
    future = mkbp(0x1000, 99_999).raw + b"\0" * (128 * 1023)       # birth beyond pool txg
    assert classify(future, LIM) is None


def test_micro_zap():
    blk = bytearray(512)
    struct.pack_into("<Q", blk, 0, (1 << 63) + 3)
    struct.pack_into("<QI", blk, 64, 944892805120, 0)
    blk[64 + 14:64 + 18] = b"size"
    assert zap.parse_micro(bytes(blk)) == {"size": 944892805120}


@pytest.mark.parametrize("bs", [512, 4096])
def test_fletcher_batch_shape(bs):
    blocks = np.frombuffer(os.urandom(bs * 3), dtype=np.uint8).reshape(3, bs)
    got = checksum.fletcher4_batch(blocks)
    assert got.shape == (3, 4)
    assert tuple(int(x) for x in got[1]) == checksum.fletcher4(blocks[1].tobytes())
