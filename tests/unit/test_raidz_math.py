"""RAIDZ geometry and parity math, checked against an independent slow reference.

The reference below is written straight from OpenZFS ``vdev_raidz.c`` (the
``VDEV_RAIDZ_MUL_2`` macro and the byte loops of ``vdev_raidz_generate_parity_*``). It uses
no tables and shares no code with :mod:`zesus.zfs.gf256`.
"""

from __future__ import annotations

import itertools
import random

import numpy as np
import pytest

from zesus.zfs import gf256, raidz

# ------------------------------------------------------------------ reference implementation


def ref_mul2(x: int) -> int:
    return ((x << 1) & 0xFF) ^ (0x1D if x & 0x80 else 0)


def ref_mul4(x: int) -> int:
    return ref_mul2(ref_mul2(x))


def ref_mul(a: int, b: int) -> int:
    """Shift-and-add multiplication in GF(2^8)/0x11d."""
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = ref_mul2(a)
        b >>= 1
    return r


def ref_parity(cols: list[bytes], nparity: int) -> list[bytes]:
    """Byte loop of vdev_raidz_generate_parity_{p,pq,pqr}.

    The first data column is copied in (and zero-extended); every later column is folded
    in with P ^= D, Q = 2Q ^ D, R = 4R ^ D. Where a column is shorter, the remaining
    positions of Q and R are still multiplied (as with a zero byte).
    """
    n = len(cols[0])
    p = bytearray(n)
    q = bytearray(n)
    r = bytearray(n)
    for ci, d in enumerate(cols):
        for i in range(n):
            x = d[i] if i < len(d) else 0
            if ci == 0:
                p[i], q[i], r[i] = x, x, x
            else:
                p[i] ^= x
                q[i] = ref_mul2(q[i]) ^ x
                r[i] = ref_mul4(r[i]) ^ x
    return [bytes(p), bytes(q), bytes(r)][:nparity]


# ------------------------------------------------------------------ field


def test_field_tables_match_reference():
    for a in range(256):
        for b in (0, 1, 2, 3, 4, 0x1D, 0x80, 0x8E, 0xFF, a):
            assert gf256.mul(a, b) == ref_mul(a, b)
    for a in range(1, 256):
        assert ref_mul(a, gf256.inv(a)) == 1
    v = np.arange(256, dtype=np.uint8)
    for c in (0, 1, 2, 4, 0x53, 0xFF):
        assert list(gf256.mul_vec(c, v)) == [ref_mul(c, int(x)) for x in v]


@pytest.mark.parametrize("nparity", [1, 2, 3])
def test_parity_matches_reference(nparity):
    rng = random.Random(1000 + nparity)
    for _ in range(40):
        ndata = rng.randint(1, 9)
        big = rng.randint(1, 64)
        nbig = rng.randint(1, ndata)
        cols = [bytes(rng.getrandbits(8) for _ in range(big if i < nbig else big - 1 or big))
                for i in range(ndata)]
        ref = ref_parity(cols, nparity)
        got = gf256.parity([np.frombuffer(c, np.uint8) for c in cols], nparity, len(cols[0]))
        assert [g.tobytes() for g in got] == ref


# ------------------------------------------------------------------ erasure round trip


@pytest.mark.parametrize("nparity", [1, 2, 3])
def test_every_erasure_pattern_round_trips(nparity):
    rng = random.Random(7 * nparity)
    for width in range(nparity + 2, 13):
        ndata = width - nparity
        for _ in range(3):
            big = rng.choice([1, 3, 8, 64])
            nbig = rng.randint(1, ndata)
            sizes = [big if i < nbig else max(1, big - 1) for i in range(ndata)]
            if big == 1:
                sizes = [1] * ndata
            data = [np.frombuffer(rng.randbytes(s), np.uint8) for s in sizes]
            par = gf256.parity(data, nparity, big)
            # every combination of up to nparity erased columns, data and parity alike
            for k in range(1, nparity + 1):
                for erased in itertools.combinations(range(width), k):
                    dd = [None if (nparity + i) in erased else d for i, d in enumerate(data)]
                    pp = [None if i in erased else p for i, p in enumerate(par)]
                    out = gf256.reconstruct(dd, pp, sizes)
                    assert [o.tobytes() for o in out] == [d.tobytes() for d in data], (width, erased)


@pytest.mark.parametrize("nparity", [1, 2, 3])
def test_too_many_erasures_refuses(nparity):
    rng = random.Random(3)
    ndata = 4
    data = [np.frombuffer(rng.randbytes(16), np.uint8) for _ in range(ndata)]
    par = gf256.parity(data, nparity)
    dd = [None] * (nparity + 1) + data[nparity + 1:]
    with pytest.raises(ValueError):
        gf256.reconstruct(dd, par, [16] * ndata)
    # losing a parity too leaves fewer equations than unknowns
    dd = [None] * nparity + data[nparity:]
    pp = [None] + par[1:]
    with pytest.raises(ValueError):
        gf256.reconstruct(dd, pp, [16] * ndata)


# ------------------------------------------------------------------ geometry


def cols(rm):
    return [(c.devidx, c.offset, c.size) for c in rm.cols]


def test_map_hand_computed_raidz1():
    K = 4096
    rm = raidz.map_alloc(0, K, 12, 4, 1)
    assert cols(rm) == [(0, 0, K), (1, 0, K)] and rm.nskip == 0 and rm.asize == 2 * K
    rm = raidz.map_alloc(3 * K, 3 * K, 12, 4, 1)          # starts on the last child: wraps
    assert cols(rm) == [(3, 0, K), (0, K, K), (1, K, K), (2, K, K)]
    assert rm.asize == 4 * K
    rm = raidz.map_alloc(5 * K, 5 * K, 12, 4, 1)          # big columns + a skip sector
    assert cols(rm) == [(1, K, 2 * K), (2, K, 2 * K), (3, K, 2 * K), (0, 2 * K, K)]
    assert rm.bigcols == 3 and rm.nskip == 1 and rm.asize == 8 * K
    # a psize that is not a sector multiple is padded to the sector size first
    assert cols(raidz.map_alloc(0, 1536, 12, 4, 1)) == [(0, 0, K), (1, 0, K)]


def test_map_raidz1_bit20_swap():
    K = 4096
    off = 1 << 20                                          # b = 256, f = 0, row 64
    rm = raidz.map_alloc(off, K, 12, 4, 1)
    assert cols(rm) == [(1, 64 * K, K), (0, 64 * K, K)]
    rm = raidz.map_alloc(3 << 20, 2 * K, 12, 6, 2)          # the swap is raidz1-only
    assert rm.cols[0].devidx == ((3 << 20) >> 12) % 6


def test_map_hand_computed_raidz2():
    rm = raidz.map_alloc(0, 512, 9, 6, 2)
    assert cols(rm) == [(0, 0, 512), (1, 0, 512), (2, 0, 512)] and rm.nskip == 0
    rm = raidz.map_alloc(5 * 512, 1024, 9, 6, 2)
    assert cols(rm) == [(5, 0, 512), (0, 512, 512), (1, 512, 512), (2, 512, 512)]
    assert rm.nskip == 2 and rm.asize == 6 * 512


def test_map_invariants_random():
    rng = random.Random(11)
    for _ in range(3000):
        nparity = rng.randint(1, 3)
        dcols = rng.randint(nparity + 1, 16)
        ashift = rng.choice([9, 12])
        sector = 1 << ashift
        off = rng.randrange(0, 1 << 40) & ~(sector - 1)
        psize = rng.randint(1, 256) * 512
        rm = raidz.map_alloc(off, psize, ashift, dcols, nparity)
        assert rm.asize == raidz.asize_of(psize, ashift, dcols, nparity)
        # data columns hold exactly the (padded) psize, parity columns are the big size
        assert sum(c.size for c in rm.data_cols) == rm.psize
        assert all(c.size == rm.cols[0].size for c in rm.parity_cols)
        # every column is on a distinct child
        assert len({c.devidx for c in rm.cols}) == len(rm.cols)
        # every sector of the block lies inside [off, off + asize) in RAIDZ space
        for c in rm.cols:
            for j in range(c.size >> ashift):
                s = (c.offset >> ashift) * dcols + c.devidx + j * dcols
                assert off >> ashift <= s < (off + rm.asize) >> ashift
        lo, hi = raidz.child_row_range(off, off + rm.asize, ashift, dcols)
        assert all(lo <= c.offset and c.offset + c.size <= hi for c in rm.cols)
        # carving can recover the block origin from where its first data column starts
        fd = rm.data_cols[0]
        assert off in raidz.first_data_origins(fd.devidx, fd.offset, ashift, dcols, nparity)
        assert raidz.locates_first_data(rm, fd.devidx, fd.offset)


def test_blocks_do_not_overlap_when_packed():
    """Consecutively allocated blocks never share a (child, sector)."""
    rng = random.Random(5)
    for nparity, dcols, ashift in [(1, 4, 12), (2, 6, 9), (3, 8, 12), (1, 3, 9)]:
        used: set[tuple[int, int]] = set()
        off = 0
        for _ in range(400):
            psize = rng.randint(1, 40) * 512
            rm = raidz.map_alloc(off, psize, ashift, dcols, nparity)
            for c in rm.cols:
                for j in range(c.size >> ashift):
                    key = (c.devidx, (c.offset >> ashift) + j)
                    assert key not in used
                    used.add(key)
            off += rm.asize
