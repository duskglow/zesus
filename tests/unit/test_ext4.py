"""ext4 plugin against real mke2fs-built images (see dev/make_ext4_fixtures.sh)."""

from __future__ import annotations

import pytest

from zfsrecover.fs.ext4 import Ext4Plugin
from zfsrecover.fs.registry import detect

BINARY = bytes((i * 7 + i // 251) & 255 for i in range(300 * 1024))


def read_entry(h, e) -> bytes:
    out = bytearray(e.size)
    for x in h.extents(e):
        if x.kind == "data":
            out[x.file_offset:x.file_offset + x.length] = h.dev.pread(x.dev_offset, x.length)
        elif x.kind == "inline":
            out[x.file_offset:x.file_offset + x.length] = x.inline[: x.length]
    return bytes(out)


@pytest.fixture(params=["ext4_basic", "ext4_inline"])
def fs(request):
    dev = request.getfixturevalue(request.param)
    h = Ext4Plugin().open(dev)
    entries = {e.path: e for e in h.iter_entries()}
    return h, entries


def test_probe_and_registry(ext4_basic):
    assert Ext4Plugin().probe(ext4_basic) == 1.0
    plugin, ident = detect(ext4_basic)
    assert plugin is not None and plugin.name == "ext4"


def test_info(fs):
    h, _ = fs
    info = h.info()
    assert info.fstype == "ext4"
    assert info.uuid == "11111111-2222-3333-4444-555555555555"
    assert info.block_size == 1024


def test_tree(fs):
    _, e = fs
    for p in ["/", "/hello.txt", "/empty.txt", "/dir", "/dir/sub/deeper/nested.txt", "/empty-dir",
              "/fast-link", "/slow-link", "/sparse.bin", "/dir/hardlink-to-hello",
              "/dir/a file: with odd*chars?.txt"]:
        assert p in e, p
    assert e["/dir"].type == "dir"
    assert e["/hello.txt"].inode == e["/dir/hardlink-to-hello"].inode
    assert "/deleted.txt" not in e


def test_contents(fs):
    h, e = fs
    assert read_entry(h, e["/hello.txt"]) == b"hello, world\n"
    assert read_entry(h, e["/dir/binary.bin"]) == BINARY
    assert read_entry(h, e["/empty.txt"]) == b""
    assert read_entry(h, e["/dir/sub/deeper/nested.txt"]) == b"nested file\n"


def test_sparse(fs):
    h, e = fs
    s = e["/sparse.bin"]
    assert s.size == (1 << 20) + len(b"after the hole")
    kinds = [x.kind for x in h.extents(s)]
    assert "sparse" in kinds
    data = read_entry(h, s)
    assert data[: 1 << 20] == b"\0" * (1 << 20) and data.endswith(b"after the hole")


def test_symlinks(fs):
    _, e = fs
    assert e["/fast-link"].link_target == "hello.txt"
    assert e["/slow-link"].link_target == "long/" * 20 + "target"


def test_deleted_inode_found(fs):
    h, e = fs
    deleted = [x for x in e.values() if x.deleted]
    assert len(deleted) == 1
    d = deleted[0]
    assert d.path.startswith("/$deleted/")
    assert read_entry(h, d) == b"to be deleted\n" * 400


def test_gap_in_inode_table_is_reported(ext4_basic):
    h0 = Ext4Plugin().open(ext4_basic)
    it = h0.gds[0].inode_table * h0.bs
    from tests.conftest import MemDevice
    dev = MemDevice(ext4_basic.data, gaps=[(it + h0.bs, h0.bs)])   # second inode-table block lost
    h = Ext4Plugin().open(dev)
    list(h.iter_entries())
    assert any("inode table" in where for where, _ in h.problems())
