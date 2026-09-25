"""A toy filesystem plugin showing the full contract.

"EXFS" layout (little endian): magic b"EXFS" at 0, u32 file count at 4, then per file a
64-byte record: 48-byte NUL-padded name, u64 data offset, u64 size.
"""

from __future__ import annotations

import struct

from zfsrecover.fs.api import DIR, FILE, Entry, Extent, FilesystemPlugin, FsHandle, FsInfo


class ExampleFsPlugin(FilesystemPlugin):
    name = "examplefs"
    description = "Example flat filesystem"

    def probe(self, dev) -> float:
        return 1.0 if dev.pread(0, 4) == b"EXFS" else 0.0

    def open(self, dev) -> FsHandle:
        return ExampleHandle(dev)


class ExampleHandle(FsHandle):
    def __init__(self, dev) -> None:
        self.dev = dev
        self._problems: list[tuple[str, str]] = []

    def info(self) -> FsInfo:
        return FsInfo(fstype="examplefs", size=self.dev.size)

    def iter_entries(self):
        yield Entry(inode=0, parent_inode=None, name="", path="/", type=DIR)
        count = struct.unpack_from("<I", self.dev.pread(4, 4))[0]
        table, gaps = self.dev.read(8, count * 64)
        if gaps:
            self._problems.append(("file table", "partly unrecoverable; some files missing"))
        for i in range(count):
            rec = table[i * 64:(i + 1) * 64]
            name = rec[:48].split(b"\0", 1)[0].decode("utf-8", "replace")
            if not name:
                continue
            off, size = struct.unpack_from("<QQ", rec, 48)
            yield Entry(inode=i + 1, parent_inode=0, name=name, path="/" + name, type=FILE, size=size,
                        extra={"offset": off})

    def extents(self, entry):
        if entry.type == FILE and entry.size:
            yield Extent(0, entry.size, entry.extra["offset"], "data")

    def problems(self):
        return self._problems
