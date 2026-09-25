"""Decoder for packed nvlists (libnvpair), in both XDR and native encodings.

Vdev labels use XDR, which is big-endian and 4-byte aligned. Pool history records and
some MOS objects use the native encoding (the writer's endianness, 8-byte aligned).
The decoder is tolerant: unknown types are skipped using the pair's recorded size and
kept as a placeholder, so one odd pair does not lose the whole list.
"""

from __future__ import annotations

import struct
from typing import Any

from ..errors import CorruptStructure

NV_ENCODE_NATIVE = 0
NV_ENCODE_XDR = 1

# data_type_t
BOOLEAN, BYTE, INT16, UINT16, INT32, UINT32, INT64, UINT64, STRING = 1, 2, 3, 4, 5, 6, 7, 8, 9
BYTE_ARRAY, INT16_ARRAY, UINT16_ARRAY, INT32_ARRAY, UINT32_ARRAY = 10, 11, 12, 13, 14
INT64_ARRAY, UINT64_ARRAY, STRING_ARRAY, HRTIME, NVLIST, NVLIST_ARRAY = 15, 16, 17, 18, 19, 20
BOOLEAN_VALUE, INT8, UINT8, BOOLEAN_ARRAY, INT8_ARRAY, UINT8_ARRAY, DOUBLE = 21, 22, 23, 24, 25, 26, 27

_MAX_DEPTH = 64


class UnknownValue:
    """Placeholder for a pair whose type we could not decode."""

    def __init__(self, type_: int, raw: bytes) -> None:
        self.type, self.raw = type_, raw

    def __repr__(self) -> str:
        return f"<nvpair type {self.type}, {len(self.raw)} bytes>"


def unpack(buf: bytes | memoryview) -> dict[str, Any]:
    """Decode a packed nvlist, whichever encoding its 4-byte header says."""
    b = bytes(buf[:4])
    if len(b) < 4:
        raise CorruptStructure("nvlist buffer too short")
    encoding, endian = b[0], b[1]
    if encoding == NV_ENCODE_XDR:
        return _Xdr(buf, 4).nvlist(0)
    if encoding == NV_ENCODE_NATIVE:
        return _Native(buf, 4, "<" if endian == 1 else ">").nvlist(0)
    raise CorruptStructure(f"unknown nvlist encoding {encoding}")


class _Xdr:
    def __init__(self, buf: bytes | memoryview, pos: int) -> None:
        self.b = memoryview(buf)
        self.p = pos

    def _take(self, n: int) -> memoryview:
        if self.p + n > len(self.b):
            raise CorruptStructure("nvlist (xdr) truncated")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self._take(8))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def string(self) -> str:
        n = self.u32()
        s = bytes(self._take(n)).decode("utf-8", "replace")
        self.p += (-n) % 4
        return s

    def nvlist(self, depth: int) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise CorruptStructure("nvlist nesting too deep")
        self.u32()   # nvl_version
        self.u32()   # nvl_nvflag
        out: dict[str, Any] = {}
        while True:
            start = self.p
            esize = self.u32()
            dsize = self.u32()
            if esize == 0 and dsize == 0:
                return out
            name = self.string()
            typ = self.u32()
            nelem = self.u32()
            try:
                out[name] = self.value(typ, nelem, depth)
            except CorruptStructure:
                raise
            except Exception:  # pragma: no cover - defensive
                out[name] = UnknownValue(typ, bytes(self.b[start:start + esize]))
            # esize covers the whole encoded pair, including any embedded list. Use it to
            # skip anything we did not consume, but never move backwards.
            if esize and self.p < start + esize:
                self.p = start + esize

    def value(self, typ: int, n: int, depth: int) -> Any:
        if typ == BOOLEAN:
            return True
        if typ in (BYTE, INT8, UINT8, INT16, UINT16, INT32, BOOLEAN_VALUE):
            v = self.i32()
            return bool(v) if typ == BOOLEAN_VALUE else v
        if typ == UINT32:
            return self.u32()
        if typ in (INT64, HRTIME):
            return self.i64()
        if typ == UINT64:
            return self.u64()
        if typ == DOUBLE:
            return struct.unpack(">d", self._take(8))[0]
        if typ == STRING:
            return self.string()
        if typ == BYTE_ARRAY:
            data = bytes(self._take(n))
            self.p += (-n) % 4
            return data
        if typ in (INT8_ARRAY, UINT8_ARRAY, INT16_ARRAY, UINT16_ARRAY, INT32_ARRAY, BOOLEAN_ARRAY):
            return [self.i32() for _ in range(n)]
        if typ == UINT32_ARRAY:
            return [self.u32() for _ in range(n)]
        if typ == INT64_ARRAY:
            return [self.i64() for _ in range(n)]
        if typ == UINT64_ARRAY:
            return [self.u64() for _ in range(n)]
        if typ == STRING_ARRAY:
            return [self.string() for _ in range(n)]
        if typ == NVLIST:
            return self.nvlist(depth + 1)
        if typ == NVLIST_ARRAY:
            return [self.nvlist(depth + 1) for _ in range(n)]
        raise ValueError(typ)


class _Native:
    """Native encoding: an 8-byte list header (version, nvflag), then nvpair_t records
    whose sizes are multiples of 8, then a 4-byte zero terminator.

    Embedded nvlists (and each element of an nvlist array) have no header: their pairs
    and terminator come right after the pair that contains them.
    """

    def __init__(self, buf: bytes | memoryview, pos: int, endian: str) -> None:
        self.b = memoryview(buf)
        self.p = pos
        self.e = endian

    def _unpack(self, fmt: str, off: int) -> tuple:
        try:
            return struct.unpack_from(self.e + fmt, self.b, off)
        except struct.error as exc:
            raise CorruptStructure("nvlist (native) truncated") from exc

    def nvlist(self, depth: int) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise CorruptStructure("nvlist nesting too deep")
        self.p += 8  # only nvl_version and nvl_nvflag are encoded
        return self.pairs(depth)

    def pairs(self, depth: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            (size,) = self._unpack("i", self.p)
            if size == 0:
                self.p += 4
                return out
            if size < 16 or self.p + size > len(self.b):
                raise CorruptStructure(f"bad native nvpair size {size}")
            start = self.p
            size, name_sz, _res, nelem, typ = self._unpack("ihhii", start)
            name = bytes(self.b[start + 16:start + 16 + name_sz]).split(b"\0", 1)[0].decode("utf-8", "replace")
            voff = start + _align8(16 + name_sz)
            self.p = start + size
            if typ == NVLIST:
                # the value area holds a (zeroed) nvlist_t; the embedded list's pairs and
                # terminator follow this nvpair directly (no version/flag header)
                out[name] = self.pairs(depth + 1)
                continue
            if typ == NVLIST_ARRAY:
                # value area: nelem pointers + nelem nvlist_t; the lists follow in order
                out[name] = [self.pairs(depth + 1) for _ in range(nelem)]
                continue
            out[name] = self.value(typ, nelem, voff, start + size)

    def value(self, typ: int, n: int, off: int, end: int) -> Any:
        e = self.e
        fixed = {BYTE: "B", INT8: "b", UINT8: "B", INT16: "h", UINT16: "H", INT32: "i",
                 UINT32: "I", INT64: "q", UINT64: "Q", HRTIME: "q", DOUBLE: "d",
                 BOOLEAN_VALUE: "i"}
        arrays = {INT8_ARRAY: "b", UINT8_ARRAY: "B", INT16_ARRAY: "h", UINT16_ARRAY: "H",
                  INT32_ARRAY: "i", UINT32_ARRAY: "I", INT64_ARRAY: "q", UINT64_ARRAY: "Q",
                  BOOLEAN_ARRAY: "i"}
        if typ == BOOLEAN:
            return True
        if typ in fixed:
            v = struct.unpack_from(e + fixed[typ], self.b, off)[0]
            return bool(v) if typ == BOOLEAN_VALUE else v
        if typ in arrays:
            return list(struct.unpack_from(f"{e}{n}{arrays[typ]}", self.b, off))
        if typ == BYTE_ARRAY:
            return bytes(self.b[off:off + n])
        if typ == STRING:
            return bytes(self.b[off:end]).split(b"\0", 1)[0].decode("utf-8", "replace")
        if typ == STRING_ARRAY:
            # n pointers (8 bytes each) followed by NUL-terminated strings
            raw = bytes(self.b[off + 8 * n:end]).split(b"\0")
            return [s.decode("utf-8", "replace") for s in raw[:n]]
        return UnknownValue(typ, bytes(self.b[off:end]))


def _align8(n: int) -> int:
    return (n + 7) & ~7
