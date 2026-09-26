"""ZFS block checksums.

Each function returns the checksum as a tuple of four uint64 values. This is the form in
which ``blk_cksum`` / ``zio_cksum_t`` stores it on disk.

The Fletcher sums use numpy prefix sums. uint64 addition wraps modulo 2**64, exactly as
the C implementation does, so four chained ``cumsum`` calls give the Fletcher-4 state
without a Python-level loop.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable

import numpy as np

from ..errors import Unsupported
from .constants import ChecksumType

Cksum = tuple[int, int, int, int]
_U64 = np.uint64


def fletcher4(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
    n = len(data) // 4
    if n == 0:
        return (0, 0, 0, 0)
    w = np.frombuffer(data, dtype=">u4" if byteswap else "<u4", count=n).astype(_U64)
    with np.errstate(over="ignore"):
        a = np.cumsum(w, dtype=_U64)
        b = np.cumsum(a, dtype=_U64)
        c = np.cumsum(b, dtype=_U64)
        d = np.cumsum(c, dtype=_U64)
    return (int(a[-1]), int(b[-1]), int(c[-1]), int(d[-1]))


def fletcher4_batch(blocks: np.ndarray) -> np.ndarray:
    """Fletcher-4 of many equal-sized blocks at once.

    *blocks* is a (k, size) uint8 array. The result is a (k, 4) uint64 array.
    """
    w = blocks.view("<u4").astype(_U64)
    with np.errstate(over="ignore"):
        a = np.cumsum(w, axis=1, dtype=_U64)
        b = np.cumsum(a, axis=1, dtype=_U64)
        c = np.cumsum(b, axis=1, dtype=_U64)
        d = np.cumsum(c, axis=1, dtype=_U64)
    return np.stack([a[:, -1], b[:, -1], c[:, -1], d[:, -1]], axis=1)


def fletcher2(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
    n = len(data) // 16
    if n == 0:
        return (0, 0, 0, 0)
    w = np.frombuffer(data, dtype=">u8" if byteswap else "<u8", count=2 * n).reshape(n, 2)
    with np.errstate(over="ignore"):
        a = np.cumsum(w, axis=0, dtype=_U64)
        b = np.cumsum(a, axis=0, dtype=_U64)
    return (int(a[-1, 0]), int(a[-1, 1]), int(b[-1, 0]), int(b[-1, 1]))


def sha256(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
    return struct.unpack(">4Q", hashlib.sha256(data).digest())  # type: ignore[return-value]


def sha512_256(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
    try:
        h = hashlib.new("sha512_256", data)
    except ValueError as exc:  # pragma: no cover - depends on OpenSSL build
        raise Unsupported("sha512/256 not available in this Python's hashlib") from exc
    return struct.unpack(">4Q", h.digest())  # type: ignore[return-value]


def blake3(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
    try:
        import blake3 as _b3
    except ImportError as exc:
        raise Unsupported("BLAKE3 checksums need the 'blake3' package (pip install blake3)") from exc
    return struct.unpack("<4Q", _b3.blake3(bytes(data)).digest())  # type: ignore[return-value]


def _unsupported(name: str) -> Callable[..., Cksum]:
    def f(data: bytes | memoryview, byteswap: bool = False) -> Cksum:
        raise Unsupported(f"{name} checksum is not implemented yet")
    return f


_FUNCS: dict[int, Callable[..., Cksum]] = {
    ChecksumType.FLETCHER_2: fletcher2,
    ChecksumType.ZILOG: fletcher2,
    ChecksumType.FLETCHER_4: fletcher4,
    ChecksumType.ZILOG2: fletcher4,
    ChecksumType.SHA256: sha256,
    ChecksumType.LABEL: sha256,
    ChecksumType.GANG_HEADER: sha256,
    ChecksumType.SHA512: sha512_256,
    ChecksumType.BLAKE3: blake3,
    ChecksumType.SKEIN: _unsupported("Skein"),
    ChecksumType.EDONR: _unsupported("Edon-R"),
}

# "on" means fletcher4 in any modern pool
_FUNCS[ChecksumType.ON] = fletcher4


def compute(ctype: int, data: bytes | memoryview, byteswap: bool = False) -> Cksum | None:
    """Checksum *data* with algorithm *ctype*. Returns None for 'off'/'noparity'."""
    if ctype in (ChecksumType.OFF, ChecksumType.NOPARITY, ChecksumType.INHERIT):
        return None
    f = _FUNCS.get(ctype)
    if f is None:
        raise Unsupported(f"unknown checksum type {ctype}")
    return f(data, byteswap)


def is_embedded(ctype: int) -> bool:
    """Checksums stored in a zio_eck_t tail inside the block rather than in the bp."""
    return ctype in (ChecksumType.LABEL, ChecksumType.GANG_HEADER, ChecksumType.ZILOG,
                     ChecksumType.ZILOG2)


def verify_embedded(buf: bytes, verifier: Cksum, ctype: int = ChecksumType.SHA256) -> bool:
    """Verify a buffer ending with a zio_eck_t (magic + checksum).

    The checksum covers the buffer with the stored checksum replaced by *verifier*. For
    labels and uberblocks, *verifier* is (offset_on_vdev, 0, 0, 0).
    """
    if len(buf) < 40:
        return False
    magic = struct.unpack_from("<Q", buf, len(buf) - 40)[0]
    if magic == 0x0210DA7AB10C7A11:
        order = "<"
    elif struct.unpack_from(">Q", buf, len(buf) - 40)[0] == 0x0210DA7AB10C7A11:
        order = ">"
    else:
        return False
    stored = struct.unpack_from(order + "4Q", buf, len(buf) - 32)
    tmp = bytearray(buf)
    struct.pack_into(order + "4Q", tmp, len(buf) - 32, *verifier)
    actual = compute(ctype, bytes(tmp), byteswap=(order == ">"))
    return actual == tuple(stored)
