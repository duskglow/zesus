"""RAIDZ reads, windows and carving over synthetic in-memory member disks.

The member images are built with the column map and parity from :mod:`zesus.zfs.raidz` and
:mod:`zesus.zfs.gf256`, whose correctness is established independently in
``test_raidz_math.py``. These tests check the plumbing: which columns are read, when parity
is used, and that every result is accepted only through its checksum.
"""

from __future__ import annotations

import random
import struct

import lz4.block
import numpy as np
import pytest

from zesus.carve.classify import PoolLimits
from zesus.carve.raidz_carve import ChunkReader, carve_rows
from zesus.io.source import ReadOnlySource
from zesus.zfs import blkptr, gf256, raidz
from zesus.zfs.checksum import fletcher4
from zesus.zfs.constants import VDEV_LABEL_START_SIZE, ChecksumType, Compression, DmuType
from zesus.zfs.reader import BlockReader, ReadStatus
from zesus.zfs.vdev import VdevMap


class MemSource(ReadOnlySource):
    def __init__(self, data: bytearray, name: str) -> None:
        self.data, self._name = data, name

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def name(self) -> str:
        return self._name

    def pread(self, offset: int, length: int) -> bytes:
        return bytes(self.data[offset:offset + length])


def l1_block(seed: int, nptr: int) -> bytes:
    """An uncompressed L1 indirect block of *nptr* plausible zvol block pointers."""
    rng = random.Random(seed)
    out = b"".join(blkptr.build(vdev=0, offset=rng.randrange(1, 1 << 20) << 12, asize=4096, psize=4096,
                                lsize=16384, comp=Compression.LZ4, cksum_type=ChecksumType.FLETCHER_4,
                                type=DmuType.ZVOL, level=0, birth=100 + rng.randrange(500),
                                cksum=tuple(rng.getrandbits(64) for _ in range(4))).raw
                   for _ in range(nptr))
    return out + b"\0" * (128 * 1024 - len(out))


def zfs_lz4(data: bytes) -> bytes:
    c = lz4.block.compress(data, store_size=False)
    return struct.pack(">I", len(c)) + c


class Pool:
    """A synthetic N-wide RAIDZ vdev holding a list of blocks."""

    def __init__(self, N: int, p: int, ashift: int, child_bytes: int) -> None:
        self.N, self.p, self.ashift = N, p, ashift
        self.images = [bytearray(VDEV_LABEL_START_SIZE + child_bytes + (1 << 20)) for _ in range(N)]
        self.asize = (child_bytes >> ashift) * N << ashift
        self.blocks: list[tuple[int, bytes, blkptr.BlockPointer]] = []

    def put(self, offset: int, raw: bytes, lsize: int, level: int = 1) -> int:
        """Write *raw* (psize bytes) at RAIDZ *offset*; return the next free offset."""
        rm = raidz.map_alloc(offset, len(raw), self.ashift, self.N, self.p)
        padded = raw + b"\0" * (rm.psize - len(raw))
        data, pos = [], 0
        for c in rm.data_cols:
            data.append(np.frombuffer(padded[pos:pos + c.size], np.uint8))
            pos += c.size
        par = gf256.parity(data, self.p, rm.cols[0].size)
        for c, col in zip(rm.cols, par + data, strict=True):
            o = VDEV_LABEL_START_SIZE + c.offset
            self.images[c.devidx][o:o + c.size] = col.tobytes()
        bp = blkptr.build(vdev=0, offset=offset, asize=rm.asize, psize=len(raw), lsize=lsize,
                          comp=Compression.LZ4, cksum_type=ChecksumType.FLETCHER_4, type=DmuType.ZVOL,
                          level=level, birth=700, cksum=fletcher4(raw))
        self.blocks.append((offset, raw, bp))
        return offset + rm.asize

    def vdevs(self, missing: tuple[int, ...] = ()) -> VdevMap:
        tree = {"type": "raidz", "id": 0, "guid": 1, "nparity": self.p, "ashift": self.ashift,
                "asize": self.asize,
                "children": [{"type": "disk", "id": i, "guid": 100 + i, "path": f"/dev/d{i}"}
                             for i in range(self.N)]}
        leaves = {100 + i: MemSource(self.images[i], f"d{i}") for i in range(self.N) if i not in missing}
        vm = VdevMap()
        vm.add_top(tree, leaves)
        return vm


def build(N=4, p=1, ashift=12, seed=1, near_bit20=True) -> Pool:
    rng = random.Random(seed)
    pool = Pool(N, p, ashift, child_bytes=(2 << 20) // N * 2 + (1 << 20))
    off = 0
    for i in range(24):
        blk = zfs_lz4(l1_block(seed * 1000 + i, rng.choice([1, 3, 40, 200, 900])))
        raw = blk + b"\0" * (-len(blk) % 512)
        off = pool.put(off, raw, 128 << 10)
        off += rng.choice([0, 0, 1 << ashift, 3 << ashift])
    if near_bit20:
        off = max(off, 1 << 20)          # RAIDZ1 swaps parity and first data column here
        for i in range(12):
            blk = zfs_lz4(l1_block(seed * 2000 + i, rng.choice([1, 50, 600])))
            off = pool.put(off, blk + b"\0" * (-len(blk) % 512), 128 << 10)
    return pool


GEOMS = [(4, 1, 12), (3, 1, 9), (6, 2, 12), (5, 2, 9), (7, 3, 12)]


@pytest.mark.parametrize("N,p,ashift", GEOMS)
def test_reads_intact_and_with_every_tolerable_loss(N, p, ashift):
    import itertools
    pool = build(N, p, ashift)
    for k in range(0, p + 1):
        for missing in itertools.combinations(range(N), k):
            r = BlockReader(pool.vdevs(missing))
            for _off, raw, bp in pool.blocks:
                rd = r.read(bp, decompress=False)
                assert rd.status is ReadStatus.OK, (missing, rd.notes)
                assert rd.data == raw
                if not missing:
                    assert rd.repaired == ()


@pytest.mark.parametrize("N,p,ashift", GEOMS)
def test_more_losses_than_parity_never_returns_data(N, p, ashift):
    pool = build(N, p, ashift)
    r = BlockReader(pool.vdevs(tuple(range(p + 1))))
    for _off, _raw, bp in pool.blocks:
        rm = raidz.map_alloc(bp.dvas[0].offset, bp.psize, ashift, N, p)
        rd = r.read(bp, decompress=False)
        lost = {c.devidx for c in rm.cols} & set(range(p + 1))
        if len(lost) > p:
            assert rd.status is not ReadStatus.OK and rd.data is None
        else:
            assert rd.status is ReadStatus.OK      # narrow block that avoids the lost disks


@pytest.mark.parametrize("N,p,ashift", GEOMS)
def test_silent_corruption_is_repaired_by_combinatorial_reconstruction(N, p, ashift):
    pool = build(N, p, ashift)
    for off, raw, bp in pool.blocks[:10]:
        rm = raidz.map_alloc(off, bp.psize, ashift, N, p)
        c = rm.data_cols[-1]
        img = pool.images[c.devidx]
        o = VDEV_LABEL_START_SIZE + c.offset
        saved = bytes(img[o:o + 16])
        img[o:o + 16] = bytes(x ^ 0xA5 for x in saved)        # damage one column in memory
        try:
            rd = BlockReader(pool.vdevs()).read(bp, decompress=False)
            assert rd.status is ReadStatus.OK and rd.data == raw
            assert rd.repaired == (c.devidx,)
            assert any("combrec" in n for n in rd.notes)
        finally:
            img[o:o + 16] = saved


def test_window_gather_matches_block_reads():
    pool = build(4, 1, 12)
    offs = np.array([b[2].dvas[0].offset for b in pool.blocks], dtype=np.uint64)
    psz = np.array([b[2].psize for b in pool.blocks], dtype=np.int32)
    for missing in [(), (1,), (3,)]:
        top = pool.vdevs(missing).top[0]
        end = int(max(o + top.extent(int(p)) for o, p in zip(offs, psz, strict=True)))
        win = top.read_window(0, end)
        buf, local, avail = win.gather(offs, psz)
        assert avail.all()
        for (_off, raw, _bp), lo in zip(pool.blocks, local, strict=True):
            assert buf[int(lo):int(lo) + len(raw)] == raw
        assert (win.repaired > 0) == bool(missing)


LIM = PoolLimits(n_vdevs=1, vdev_asize=1 << 40, max_txg=10_000)


@pytest.mark.parametrize("N,p,ashift", [(4, 1, 12), (3, 1, 9), (5, 1, 9), (6, 2, 12)])
@pytest.mark.parametrize("missing", [(), (0,), (1,), (2,)])
@pytest.mark.parametrize("seed", [1, 2])
def test_carving_finds_every_block_exactly_once(N, p, ashift, missing, seed):
    if len(missing) > 0 and p > 1:
        pytest.skip("virtual carving of a missing member is RAIDZ1-only")
    pool = build(N, p, ashift, seed=seed)
    top = pool.vdevs(missing).top[0]
    bs = 1 << ashift
    child_end = pool.asize // N
    found: dict[int, object] = {}
    step = 64 * bs
    for lo in range(0, child_end, step):
        cr = ChunkReader(top, lo, min(lo + step, child_end))
        hits, _st = carve_rows(top, cr, LIM)
        for h in hits:
            assert h.dva_offset not in found, "block reported twice"
            found[h.dva_offset] = h
    want = {off for off, _raw, _bp in pool.blocks}
    assert want <= set(found), sorted(want - set(found))
    # no echoes: a one-column block's parity copy is not reported as another block
    assert set(found) == want, sorted(set(found) - want)
    for off, _raw, bp in pool.blocks:
        h = found[off]
        assert h.kind == "indirect" and h.psize >= bp.psize
        assert h.virtual == (h.member in missing)


def test_windows_cover_raidz_extents_and_accept_empty_worklists():
    import numpy as np

    from zesus.carve.verify import windows
    assert list(windows({"blkid": np.zeros(0, np.int64)})) == []
    wl = {"blkid": np.arange(3), "vdev": np.zeros(3, np.uint32),
          "offset": np.array([0, 8192, 1 << 30], np.uint64), "psize": np.array([4096, 4096, 4096], np.int32),
          "extent": np.array([8192, 8192, 8192], np.int64)}
    got = [(w[2], w[3]) for w in windows(wl)]
    assert got == [(0, 16384), (1 << 30, (1 << 30) + 8192)]
