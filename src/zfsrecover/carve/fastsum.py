"""Bulk Fletcher-4 over many blocks inside one buffer.

numba is used when installed. It compiles automatically on first use, and the user never
builds anything. Otherwise a numpy implementation is used.
"""

from __future__ import annotations

import numpy as np

try:  # pragma: no cover - depends on environment
    import numba

    @numba.njit(cache=True, nogil=True)
    def _f4_many(words, starts, counts, out):  # type: ignore[no-untyped-def]
        for i in range(starts.shape[0]):
            a = np.uint64(0)
            b = np.uint64(0)
            c = np.uint64(0)
            d = np.uint64(0)
            s = starts[i]
            for k in range(s, s + counts[i]):
                a += np.uint64(words[k])
                b += a
                c += b
                d += c
            out[i, 0] = a
            out[i, 1] = b
            out[i, 2] = c
            out[i, 3] = d

    HAVE_NUMBA = True
except Exception:  # pragma: no cover
    HAVE_NUMBA = False


def fletcher4_many(buf: bytes | memoryview, offsets: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    """Fletcher-4 of buf[offsets[i]:offsets[i]+sizes[i]] for each i.

    Offsets and sizes must be multiples of 4. Returns an (n, 4) uint64 array.
    """
    n = len(offsets)
    out = np.zeros((n, 4), dtype=np.uint64)
    if n == 0:
        return out
    words = np.frombuffer(buf, dtype="<u4", count=len(buf) // 4)
    starts = (np.asarray(offsets, dtype=np.int64) // 4)
    counts = (np.asarray(sizes, dtype=np.int64) // 4)
    if HAVE_NUMBA:
        _f4_many(words, starts, counts, out)
        return out
    with np.errstate(over="ignore"):
        for i in range(n):
            w = words[starts[i]:starts[i] + counts[i]].astype(np.uint64)
            a = np.cumsum(w, dtype=np.uint64)
            b = np.cumsum(a, dtype=np.uint64)
            c = np.cumsum(b, dtype=np.uint64)
            out[i] = (a[-1], b[-1], c[-1], np.cumsum(c, dtype=np.uint64)[-1])
    return out
