"""GF(2^8) arithmetic and RAIDZ parity, as used by OpenZFS ``vdev_raidz.c``.

The field uses the polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d), matching
``VDEV_RAIDZ_MUL_2``. RAIDZ parities over data columns D_0..D_{n-1} are:

* P = D_0 ^ D_1 ^ ... ^ D_{n-1}
* Q = sum 2^(n-1-i) * D_i    (Horner: Q = Q*2 ^ D_i)
* R = sum 4^(n-1-i) * D_i    (Horner: R = R*4 ^ D_i)

Shorter columns are treated as zero-padded to the parity length, which is exactly what
the ``*_generate_parity_*`` routines do.

Reconstruction is a generic erasure solve. Any set of at most ``nparity`` missing data
columns is recovered from the same number of surviving parity columns, by inverting a
small coefficient matrix over the field. It refuses rather than guess: asking for more
unknowns than available parities raises :class:`ValueError`.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

POLY = 0x11D
GENERATORS = (1, 2, 4)          # coefficient base for P, Q, R

EXP = np.zeros(512, dtype=np.uint8)
LOG = np.zeros(256, dtype=np.int32)
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= POLY
EXP[255:510] = EXP[0:255]
del _x, _i


def mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return int(EXP[LOG[a] + LOG[b]])


def inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(2^8)")
    return int(EXP[255 - LOG[a]])


def pow_(a: int, n: int) -> int:
    if n == 0:
        return 1
    if a == 0:
        return 0
    return int(EXP[(LOG[a] * n) % 255])


def mul_vec(c: int, v: np.ndarray) -> np.ndarray:
    """Multiply a uint8 vector by the scalar *c*."""
    if c == 0:
        return np.zeros_like(v)
    if c == 1:
        return v.copy()
    out = EXP[(LOG[v] + LOG[c])]
    out[v == 0] = 0
    return out


def _pad(cols: Sequence[np.ndarray], n: int) -> list[np.ndarray]:
    out = []
    for c in cols:
        if len(c) == n:
            out.append(c)
        else:
            z = np.zeros(n, dtype=np.uint8)
            z[:len(c)] = c
            out.append(z)
    return out


def coef(k: int, i: int, ndata: int) -> int:
    """Coefficient of data column *i* in parity *k* (0=P, 1=Q, 2=R)."""
    return pow_(GENERATORS[k], ndata - 1 - i)


def parity(data: Sequence[np.ndarray], nparity: int, length: int | None = None) -> list[np.ndarray]:
    """Compute the first *nparity* parity columns of *data* (uint8 arrays)."""
    if not data:
        raise ValueError("no data columns")
    n = length if length is not None else max(len(d) for d in data)
    cols = _pad([np.asarray(d, dtype=np.uint8) for d in data], n)
    out = []
    for k in range(nparity):
        g = GENERATORS[k]
        acc = np.zeros(n, dtype=np.uint8)
        for d in cols:                      # Horner, in column order
            acc = mul_vec(g, acc) ^ d
        out.append(acc)
    return out


def _solve(a: list[list[int]]) -> list[list[int]]:
    """Invert a square matrix over GF(2^8) by Gauss-Jordan elimination."""
    m = len(a)
    aug = [row[:] + [1 if i == j else 0 for j in range(m)] for i, row in enumerate(a)]
    for col in range(m):
        piv = next((r for r in range(col, m) if aug[r][col]), None)
        if piv is None:
            raise ValueError("singular reconstruction matrix")
        aug[col], aug[piv] = aug[piv], aug[col]
        iv = inv(aug[col][col])
        aug[col] = [mul(iv, x) for x in aug[col]]
        for r in range(m):
            if r != col and aug[r][col]:
                f = aug[r][col]
                aug[r] = [x ^ mul(f, y) for x, y in zip(aug[r], aug[col], strict=True)]
    return [row[m:] for row in aug]


def reconstruct(data: Sequence[np.ndarray | None], parities: Sequence[np.ndarray | None],
                sizes: Sequence[int]) -> list[np.ndarray]:
    """Recover missing data columns.

    *data*: one entry per data column, ``None`` where missing.
    *parities*: P, Q, R... as available (``None`` where missing), each of the parity length.
    *sizes*: the true byte length of each data column (short columns are zero-padded).

    Returns the full list of data columns. Raises ValueError if there are more missing
    columns than surviving parities.
    """
    ndata = len(data)
    if len(sizes) != ndata:
        raise ValueError("sizes/data length mismatch")
    missing = [i for i, d in enumerate(data) if d is None]
    if not missing:
        return [np.asarray(d, dtype=np.uint8) for d in data]   # type: ignore[arg-type]
    avail = [k for k, p in enumerate(parities) if p is not None]
    if len(avail) < len(missing):
        raise ValueError(f"{len(missing)} data columns missing but only {len(avail)} parities")
    use = avail[:len(missing)]
    n = len(parities[use[0]])                                   # type: ignore[arg-type]
    known = {i: _pad([np.asarray(d, dtype=np.uint8)], n)[0]
             for i, d in enumerate(data) if d is not None}
    # b_k = parity_k ^ sum_{known i} coef(k,i) * D_i
    rhs = []
    for k in use:
        acc = np.asarray(parities[k], dtype=np.uint8).copy()
        for i, d in known.items():
            acc ^= mul_vec(coef(k, i, ndata), d)
        rhs.append(acc)
    if len(missing) == 1 and use[0] == 0:
        solved = [rhs[0]]                                       # P-only: plain XOR
    else:
        a = [[coef(k, j, ndata) for j in missing] for k in use]
        ainv = _solve(a)
        solved = []
        for r in range(len(missing)):
            acc = np.zeros(n, dtype=np.uint8)
            for c in range(len(missing)):
                acc ^= mul_vec(ainv[r][c], rhs[c])
            solved.append(acc)
    out: list[np.ndarray] = []
    it = iter(solved)
    for i in range(ndata):
        if i in known:
            out.append(known[i][:sizes[i]])
        else:
            out.append(next(it)[:sizes[i]])
    return out
