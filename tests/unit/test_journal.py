from __future__ import annotations

import struct

from zesus.fs.ext4 import journal as J

BS = 1024


def hdr(btype: int, seq: int) -> bytes:
    return struct.pack(">III", J.JBD2_MAGIC, btype, seq)


def descriptor(seq: int, tags: list[tuple[int, int]]) -> bytes:
    b = bytearray(hdr(J.DESCRIPTOR, seq))
    for i, (home, flags) in enumerate(tags):
        if i:
            flags |= J.FLAG_SAME_UUID
        if i == len(tags) - 1:
            flags |= J.FLAG_LAST_TAG
        b += struct.pack(">IHH", home, 0, flags)       # 8-byte tags: no 64bit, no csum
        if not flags & J.FLAG_SAME_UUID:
            b += b"U" * 16
    return bytes(b.ljust(BS, b"\0"))


def build_log() -> list[bytes]:
    blocks = [b""] * 32
    sb = bytearray(BS)
    sb[:12] = hdr(J.SB_V2, 0)
    struct.pack_into(">5I", sb, 12, BS, 32, 1, 5, 1)       # blocksize maxlen first sequence start
    blocks[0] = bytes(sb)
    # T5: blocks 100 and 200
    blocks[1] = descriptor(5, [(100, 0), (200, 0)])
    blocks[2] = b"A" * BS
    blocks[3] = b"B" * BS
    blocks[4] = hdr(J.COMMIT, 5).ljust(BS, b"\0")
    # T6: revoke 100
    blocks[5] = (hdr(J.REVOKE, 6) + struct.pack(">I", 20) + struct.pack(">I", 100)).ljust(BS, b"\0")
    blocks[6] = hdr(J.COMMIT, 6).ljust(BS, b"\0")
    # T7: block 300, escaped (its data started with the journal magic)
    blocks[7] = descriptor(7, [(300, J.FLAG_ESCAPE)])
    blocks[8] = b"\0\0\0\0" + b"C" * (BS - 4)
    blocks[9] = hdr(J.COMMIT, 7).ljust(BS, b"\0")
    # T8: descriptor without commit (crash mid-transaction): must NOT be replayed
    blocks[10] = descriptor(8, [(400, 0)])
    blocks[11] = b"D" * BS
    return blocks


def test_replay():
    blocks = build_log()
    ov = J.replay(lambda n: blocks[n] if n < len(blocks) and blocks[n] else None, len(blocks))
    assert ov.transactions == 3 and (ov.first_seq, ov.last_seq) == (5, 7)
    assert 100 not in ov.blocks and ov.revoked == 1
    assert ov.blocks[200] == (3, False)
    assert ov.blocks[300] == (8, True)
    assert 400 not in ov.blocks
    assert J.unescape(blocks[8], True)[:4] == struct.pack(">I", J.JBD2_MAGIC)


def test_clean_journal():
    blocks = build_log()
    sb = bytearray(blocks[0])
    struct.pack_into(">I", sb, 28, 0)      # s_start = 0
    blocks[0] = bytes(sb)
    ov = J.replay(lambda n: blocks[n] or None, len(blocks))
    assert ov.clean and not ov.blocks
