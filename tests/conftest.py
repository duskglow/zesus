from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class MemDevice:
    """In-memory device implementing the plugin Device protocol (no gaps unless given)."""

    def __init__(self, data: bytes, gaps: list[tuple[int, int]] | None = None) -> None:
        self.data = data
        self.size = len(data)
        self.gaps = gaps or []

    def read(self, offset: int, length: int):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class G:
            offset: int
            length: int

        chunk = bytearray(self.data[offset:offset + length])
        out = []
        for a, n in self.gaps:
            lo, hi = max(a, offset), min(a + n, offset + length)
            if lo < hi:
                chunk[lo - offset:hi - offset] = b"\0" * (hi - lo)
                out.append(G(lo, hi - lo))
        return bytes(chunk), out

    def pread(self, offset: int, length: int) -> bytes:
        return self.read(offset, length)[0]


@pytest.fixture
def ext4_basic() -> MemDevice:
    return MemDevice(gzip.decompress((FIXTURES / "ext4-basic.img.gz").read_bytes()))


@pytest.fixture
def ext4_inline() -> MemDevice:
    return MemDevice(gzip.decompress((FIXTURES / "ext4-inline.img.gz").read_bytes()))


@pytest.fixture
def test_image() -> str:
    p = os.environ.get("ZFR_TEST_IMAGE")
    if not p or not os.path.exists(p):
        pytest.skip("set ZFR_TEST_IMAGE to a real ZFS image to run integration tests")
    return p
