"""Filesystem plugin interface.

A plugin turns bytes on a device into a file inventory. It never touches the map
database or the evidence directly. It reads through a :class:`Device`, which reports
unrecoverable ranges, and it describes each file's data as extents. The core then works
out each file's recovery status from the volume's block map, and the extractor copies
the extents out. Supporting a new filesystem therefore needs no change to core code.

Minimal plugin::

    class MyFsPlugin(FilesystemPlugin):
        name = "myfs"
        def probe(self, dev): return 1.0 if dev.pread(0, 4) == b"MYFS" else 0.0
        def open(self, dev): return MyFsHandle(dev)

registered in the plugin's ``pyproject.toml``::

    [project.entry-points."zfsrecover.filesystems"]
    myfs = "my_package:MyFsPlugin"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


class Gap(Protocol):
    offset: int
    length: int


class Device(Protocol):
    """What a plugin reads from: a byte range with known holes."""
    size: int

    def read(self, offset: int, length: int) -> tuple[bytes, list[Gap]]:
        """Bytes plus the unrecoverable sub-ranges (which read as zeros)."""

    def pread(self, offset: int, length: int) -> bytes:
        """Bytes only; unrecoverable ranges read as zeros."""


class DeviceSlice:
    """A window [start, start+length) of another device (a partition, for example)."""

    def __init__(self, dev: Device, start: int, length: int) -> None:
        self.dev, self.start = dev, start
        self.size = max(0, min(length, dev.size - start))

    def read(self, offset: int, length: int):
        if offset >= self.size:
            return b"", []
        length = min(length, self.size - offset)
        data, gaps = self.dev.read(self.start + offset, length)
        return data, [replace(g, offset=g.offset - self.start) for g in gaps]

    def pread(self, offset: int, length: int) -> bytes:
        return self.read(offset, length)[0]


FILE, DIR, SYMLINK, CHARDEV, BLOCKDEV, FIFO, SOCKET, OTHER = (
    "file", "dir", "symlink", "chardev", "blockdev", "fifo", "socket", "other")


@dataclass
class Entry:
    inode: int
    parent_inode: int | None
    name: str
    path: str
    type: str
    size: int = 0
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    atime: int | None = None
    mtime: int | None = None
    ctime: int | None = None
    crtime: int | None = None
    deleted: bool = False
    link_target: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Extent:
    """A piece of a file. ``dev_offset`` is a byte offset on the plugin's device.

    kind is one of:
      * ``data``: bytes live at dev_offset;
      * ``sparse``: a hole in the file, reads as zeros, nothing lost;
      * ``inline``: bytes carried in ``inline`` (stored inside metadata);
      * ``unwritten``: preallocated but never written, reads as zeros.
    """
    file_offset: int
    length: int
    dev_offset: int | None
    kind: str = "data"
    inline: bytes | None = None


@dataclass
class FsInfo:
    fstype: str
    label: str = ""
    uuid: str = ""
    block_size: int = 0
    size: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class FsHandle(ABC):
    @abstractmethod
    def info(self) -> FsInfo: ...

    @abstractmethod
    def iter_entries(self) -> Iterator[Entry]:
        """Every file/dir/link reachable from the root, then orphaned or deleted entries
        the plugin can find (``deleted=True``)."""

    @abstractmethod
    def extents(self, entry: Entry) -> Iterator[Extent]: ...

    def problems(self) -> list[tuple[str, str]]:
        """(where, what) pairs describing metadata that could not be read."""
        return []


class FilesystemPlugin(ABC):
    name: str = "?"
    #: Human-readable name of the filesystem family.
    description: str = ""

    @abstractmethod
    def probe(self, dev: Device) -> float:
        """Confidence 0..1 that *dev* starts with this filesystem."""

    @abstractmethod
    def open(self, dev: Device) -> FsHandle: ...


class Identifier(ABC):
    """Signature-only detection (no inventory) for filesystems/containers without a
    full plugin. Registered under ``zfsrecover.identifiers``."""
    name: str = "?"

    @abstractmethod
    def identify(self, dev: Device) -> FsInfo | None: ...
