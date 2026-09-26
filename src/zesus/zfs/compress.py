"""ZFS block decompression.

``decompress(comp, data, lsize)`` returns exactly *lsize* bytes or raises
:class:`DecompressionError`. The ZFS framings differ from the stock formats in a few ways:

* LZ4: a 4-byte big-endian compressed length precedes a raw LZ4 block.
* ZSTD: a 4-byte BE compressed length and a 4-byte BE version/level word precede a zstd frame.
* GZIP: plain zlib stream (``compress2``).
* LZJB / ZLE: ZFS-specific, implemented here.
"""

from __future__ import annotations

import struct
import zlib

from ..errors import DecompressionError, Unsupported
from .constants import Compression


def lz4_zfs(data: bytes | memoryview, lsize: int) -> bytes:
    import lz4.block
    if len(data) < 4:
        raise DecompressionError("lz4: buffer shorter than header")
    clen = struct.unpack_from(">I", data, 0)[0]
    if clen == 0 or clen + 4 > len(data):
        raise DecompressionError(f"lz4: implausible compressed length {clen} for psize {len(data)}")
    try:
        out = lz4.block.decompress(bytes(data[4:4 + clen]), uncompressed_size=lsize)
    except Exception as exc:  # lz4.block.LZ4BlockError
        raise DecompressionError(f"lz4: {exc}") from exc
    return _fit(out, lsize, "lz4")


def zstd_zfs(data: bytes | memoryview, lsize: int) -> bytes:
    try:
        import zstandard
    except ImportError as exc:
        raise Unsupported("zstd-compressed blocks need 'zstandard' (pip install zstandard)") from exc
    if len(data) < 8:
        raise DecompressionError("zstd: buffer shorter than header")
    clen = struct.unpack_from(">I", data, 0)[0]
    if clen == 0 or clen + 8 > len(data):
        raise DecompressionError(f"zstd: implausible compressed length {clen}")
    try:
        out = zstandard.ZstdDecompressor().decompress(bytes(data[8:8 + clen]), max_output_size=lsize)
    except zstandard.ZstdError as exc:
        raise DecompressionError(f"zstd: {exc}") from exc
    return _fit(out, lsize, "zstd")


def gzip_zfs(data: bytes | memoryview, lsize: int) -> bytes:
    try:
        d = zlib.decompressobj()
        out = d.decompress(bytes(data), lsize)
    except zlib.error as exc:
        raise DecompressionError(f"gzip: {exc}") from exc
    return _fit(out, lsize, "gzip")


def lzjb(data: bytes | memoryview, lsize: int) -> bytes:
    src = bytes(data)
    dst = bytearray(lsize)
    s = d = 0
    copymask = 1 << 7
    copymap = 0
    n = len(src)
    NBBY, MATCH_BITS, MATCH_MIN = 8, 6, 3
    OFFSET_MASK = (1 << (16 - MATCH_BITS)) - 1
    while d < lsize:
        copymask <<= 1
        if copymask == (1 << NBBY):
            copymask = 1
            if s >= n:
                raise DecompressionError("lzjb: input exhausted")
            copymap = src[s]
            s += 1
        if copymap & copymask:
            if s + 1 >= n:
                raise DecompressionError("lzjb: input exhausted in match")
            mlen = (src[s] >> (NBBY - MATCH_BITS)) + MATCH_MIN
            offset = ((src[s] << NBBY) | src[s + 1]) & OFFSET_MASK
            s += 2
            cpy = d - offset
            if cpy < 0:
                raise DecompressionError("lzjb: match before start of output")
            while mlen > 0 and d < lsize:
                dst[d] = dst[cpy]
                d += 1
                cpy += 1
                mlen -= 1
        else:
            if s >= n:
                raise DecompressionError("lzjb: input exhausted in literal")
            dst[d] = src[s]
            d += 1
            s += 1
    return bytes(dst)


def zle(data: bytes | memoryview, lsize: int, n: int = 64) -> bytes:
    src = bytes(data)
    dst = bytearray()
    s = 0
    while len(dst) < lsize:
        if s >= len(src):
            raise DecompressionError("zle: input exhausted")
        length = 1 + src[s]
        s += 1
        if length <= n:
            dst += src[s:s + length]
            s += length
        else:
            dst += b"\0" * (length - n)
    return _fit(bytes(dst), lsize, "zle")


def _fit(out: bytes, lsize: int, name: str) -> bytes:
    if len(out) == lsize:
        return out
    if len(out) < lsize:
        # Some encoders legitimately stop early. ZFS zero-fills the rest of the buffer.
        return out + b"\0" * (lsize - len(out))
    raise DecompressionError(f"{name}: produced {len(out)} bytes, expected {lsize}")


def decompress(comp: int, data: bytes | memoryview, lsize: int) -> bytes:
    if comp in (Compression.OFF, Compression.INHERIT):
        return bytes(data[:lsize])
    if comp == Compression.EMPTY:
        return b"\0" * lsize
    if comp in (Compression.LZ4, Compression.ON):
        return lz4_zfs(data, lsize)
    if comp == Compression.LZJB:
        return lzjb(data, lsize)
    if Compression.GZIP_1 <= comp <= Compression.GZIP_9:
        return gzip_zfs(data, lsize)
    if comp == Compression.ZLE:
        return zle(data, lsize)
    if comp == Compression.ZSTD:
        return zstd_zfs(data, lsize)
    raise Unsupported(f"unknown compression {comp}")
