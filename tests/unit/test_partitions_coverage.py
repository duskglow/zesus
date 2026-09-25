from __future__ import annotations

import struct
import uuid
import zlib

from tests.conftest import MemDevice
from zfsrecover.map.db import MapDB
from zfsrecover.partitions import detect


def make_gpt(size: int, parts: list[tuple[int, int, str, str]]) -> bytes:
    img = bytearray(size)
    img[446 + 4] = 0xEE
    struct.pack_into("<II", img, 446 + 8, 1, size // 512 - 1)
    img[510:512] = b"\x55\xaa"
    entries = bytearray(128 * 128)
    for i, (first, last, tguid, name) in enumerate(parts):
        e = uuid.UUID(tguid).bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
        e += name.encode("utf-16-le").ljust(72, b"\0")
        entries[i * 128:(i + 1) * 128] = e
    img[1024:1024 + len(entries)] = entries
    hdr = bytearray(92)
    hdr[:8] = b"EFI PART"
    struct.pack_into("<IIIIQQQQ", hdr, 8, 0x10000, 92, 0, 0, 1, size // 512 - 1, 34, size // 512 - 34)
    hdr[56:72] = uuid.uuid4().bytes_le
    struct.pack_into("<QIII", hdr, 72, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into("<I", hdr, 16, zlib.crc32(hdr))
    img[512:604] = hdr
    return bytes(img)


def test_gpt():
    lin = "0fc63daf-8483-4772-8e79-3d69d8477de4"
    dev = MemDevice(make_gpt(4 << 20, [(2048, 4095, lin, "root"), (4096, 8000, lin, "home")]))
    scheme, parts = detect(dev)
    assert scheme == "gpt"
    assert [(p.index, p.start, p.length, p.name, p.type_name) for p in parts] == [
        (1, 2048 * 512, 2048 * 512, "root", "Linux filesystem"),
        (2, 4096 * 512, 3905 * 512, "home", "Linux filesystem")]


def test_mbr_with_logical():
    img = bytearray(8 << 20)
    img[510:512] = b"\x55\xaa"
    img[446:462] = bytes([0x80, 0, 0, 0, 0x83, 0, 0, 0]) + struct.pack("<II", 2048, 2048)
    img[462:478] = bytes([0, 0, 0, 0, 0x05, 0, 0, 0]) + struct.pack("<II", 8192, 4096)
    ebr = 8192 * 512
    img[ebr + 510:ebr + 512] = b"\x55\xaa"
    img[ebr + 446:ebr + 462] = bytes([0, 0, 0, 0, 0x07, 0, 0, 0]) + struct.pack("<II", 63, 1000)
    scheme, parts = detect(MemDevice(bytes(img)))
    assert scheme == "mbr"
    assert [(p.index, p.type_id, p.start // 512) for p in parts] == [(1, "0x83", 2048), (2, "0x05", 8192),
                                                                      (5, "0x07", 8255)]


def test_coverage_lookup(tmp_path):
    from zfsrecover.volume.coverage import Coverage
    db = MapDB(tmp_path / "m.sqlite")
    db.execute("INSERT INTO pools(id,name,guid) VALUES(1,'p','1')")
    db.execute("INSERT INTO volumes(id,name,volblocksize,n_blocks) VALUES(1,'v',16384,100)")
    runs = [(0, 10, "ok"), (10, 5, "cksum_mismatch"), (15, 80, "hole"), (95, 5, "no_metadata")]
    for a, n, s in runs:
        db.execute("INSERT INTO volume_coverage VALUES(1,?,?,?,?)", (a, n, s, ""))
    db.commit()
    c = Coverage(db, 1)
    bs = 16384
    assert c.lost_blocks(0, 10) == 0
    assert c.lost_blocks(0, 100) == 10
    assert c.lost_blocks(12, 20) == 3
    assert c.lost_blocks(96, 120) == 24          # past the end counts as lost
    assert c.lost_bytes(0, 10 * bs) == 0
    assert c.lost_bytes(11 * bs + 5, 10) == 10
    assert c.status_at(11) == "cksum_mismatch"


def test_lost_ranges(tmp_path):
    from zfsrecover.volume.coverage import Coverage
    db = MapDB(tmp_path / "m.sqlite")
    db.execute("INSERT INTO pools(id,name,guid) VALUES(1,'p','1')")
    db.execute("INSERT INTO volumes(id,name,volblocksize,n_blocks) VALUES(1,'v',1024,20)")
    for a, n, s in [(0, 5, "ok"), (5, 2, "cksum_mismatch"), (7, 10, "ok"), (17, 3, "zeroed")]:
        db.execute("INSERT INTO volume_coverage VALUES(1,?,?,?,?)", (a, n, s, ""))
    db.commit()
    c = Coverage(db, 1)
    assert c.lost_ranges(0, 20 * 1024) == [(5120, 2048, "cksum_mismatch"), (17408, 3072, "zeroed")]
    assert c.lost_ranges(5500, 100) == [(5500, 100, "cksum_mismatch")]
    assert c.lost_ranges(19 * 1024, 4096) == [(19456, 1024, "zeroed"), (20480, 3072, "no_metadata")]
