"""Deterministic synthetic content for the ZFS test pools.

Used by dev/make_zfs_fixtures.sh to fill zvols and files, and by the tests to
regenerate the exact original bytes, so they can check every recovered byte (not
just a hash).

    python3 dev/zfx_gen.py SEED NBYTES random|text|mixed > out.bin
"""

from __future__ import annotations

import random
import sys


def generate(seed: int, n: int, kind: str) -> bytes:
    r = random.Random(seed)
    words = [bytes(r.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(r.randint(2, 9))) for _ in range(500)]
    out = bytearray()
    while len(out) < n:
        k = kind if kind != "mixed" else r.choice(["random", "text"])
        m = min(n - len(out), 1 << 16)
        if k == "random":
            b = r.randbytes(m)
        else:
            b = b" ".join(r.choice(words) for _ in range(m // 4))[:m]
            b = b + b"\n" * (m - len(b))
        out += b
    return bytes(out)


if __name__ == "__main__":
    sys.stdout.buffer.write(generate(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
