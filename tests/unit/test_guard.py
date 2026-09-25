"""The read-only guarantee: nothing in the process may modify a protected source."""

from __future__ import annotations

import os
import shutil

import pytest

from zfsrecover.errors import SourceWriteAttempt
from zfsrecover.io import RawSource, guard


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "evidence.img"
    p.write_bytes(bytes(range(256)) * 64)
    s = RawSource(p)
    yield p, s
    s.close()
    guard.unprotect(p)


def test_reads_work(src):
    p, s = src
    assert s.size == 256 * 64
    assert s.pread(1, 3) == b"\x01\x02\x03"
    assert s.pread(s.size - 1, 10) == b"\xff"
    assert s.pread(s.size, 10) == b""


def test_no_write_api(src):
    _, s = src
    for name in ("write", "pwrite", "truncate", "flush"):
        assert not hasattr(s, name)


@pytest.mark.parametrize("mode", ["wb", "ab", "r+b", "w", "xb"])
def test_builtin_open_for_write_is_blocked(src, mode):
    p, _ = src
    with pytest.raises(SourceWriteAttempt):
        open(p, mode)


def test_os_open_write_flags_blocked(src):
    p, _ = src
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC, os.O_WRONLY | os.O_APPEND):
        with pytest.raises(SourceWriteAttempt):
            os.open(p, flags)


def test_read_open_still_allowed(src):
    p, _ = src
    with open(p, "rb") as f:
        assert f.read(2) == b"\x00\x01"


def test_mutating_os_calls_blocked(src, tmp_path):
    p, _ = src
    with pytest.raises(SourceWriteAttempt):
        os.remove(p)
    with pytest.raises(SourceWriteAttempt):
        os.rename(p, tmp_path / "moved")
    with pytest.raises(SourceWriteAttempt):
        os.truncate(p, 0)
    with pytest.raises(SourceWriteAttempt):
        shutil.copyfile(tmp_path / "nonexistent-but-irrelevant", p)
    assert p.stat().st_size == 256 * 64


def test_alias_paths_are_normalized(src, tmp_path):
    p, _ = src
    alias = tmp_path / "." / p.name
    with pytest.raises(SourceWriteAttempt):
        open(alias, "wb")


def test_assert_not_protected(src, tmp_path):
    p, _ = src
    with pytest.raises(SourceWriteAttempt):
        guard.assert_not_protected([p])
    guard.assert_not_protected([tmp_path / "other"])


def test_unchanged_check(src):
    _, s = src
    s.verify_unchanged()


def test_io_package_has_no_write_calls():
    """Static guard: the I/O layer must never gain a write path."""
    import re
    from pathlib import Path

    import zfsrecover.io as io_pkg
    root = Path(io_pkg.__file__).parent
    bad = re.compile(r"\bos\.(write|pwrite|ftruncate|truncate)\(|\.write\(|open\([^)]*['\"][wa+]")
    for f in root.glob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if bad.search(line) and "noqa: write-ok" not in line:
                raise AssertionError(f"{f.name}:{i}: write call in the read-only I/O layer: {line.strip()}")
