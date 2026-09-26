"""Read-only access to evidence sources (raw image files and block devices).

``ReadOnlySource`` is the *only* way the rest of the package touches the evidence.
It deliberately has no write/truncate/flush methods. The underlying descriptor is
opened with ``O_RDONLY``, so writing through it fails at the OS level even if someone
bypasses this class. The path is also registered with :mod:`zesus.io.guard`.

Other kinds of source (E01, split raw, AFF4...) can be added as plugins through the
``zesus.sources`` entry-point group. They only need to subclass
:class:`ReadOnlySource`.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import SourceChanged
from . import guard

log = logging.getLogger(__name__)

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOINHERIT = getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)


class ReadOnlySource(ABC):
    """Abstract random-access, read-only byte source."""

    @property
    @abstractmethod
    def size(self) -> int: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def pread(self, offset: int, length: int) -> bytes:
        """Read up to *length* bytes at *offset*. Short reads happen only at end of source."""

    def read_exact(self, offset: int, length: int) -> bytes:
        data = self.pread(offset, length)
        if len(data) != length:
            raise EOFError(f"{self.name}: short read at {offset:#x} (+{length:#x}), got {len(data):#x}")
        return data

    def close(self) -> None:  # noqa: B027 - optional override
        pass

    def __enter__(self) -> ReadOnlySource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class SourceIdentity:
    """Facts recorded at open time, re-checked later to detect a changing source."""
    path: str
    size: int
    mtime_ns: int | None
    is_device: bool


class RawSource(ReadOnlySource):
    """A raw disk image file or a block/character device, opened read-only.

    Devices on Windows (``\\\\.\\PhysicalDriveN``) and some Unix character devices need
    sector-aligned I/O, so reads on devices are widened to *align* bytes transparently.
    """

    def __init__(self, path: str | os.PathLike[str], *, align: int | None = None) -> None:
        self._path = os.fspath(path)
        guard.protect(self._path)
        self._fd = os.open(self._path, os.O_RDONLY | _O_BINARY | _O_NOINHERIT)
        self._lock = threading.Lock()
        # Windows has no os.pread. Concurrent readers each check out their own read-only
        # descriptor, so they do not serialize on one file position.
        self._free_fds: list[int] = [self._fd]
        self._all_fds: list[int] = [self._fd]
        st = os.fstat(self._fd)
        self._is_device = _is_device_path(self._path) or stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode)
        self._size = _device_size(self._fd, self._path) if self._is_device else st.st_size
        self._align = align if align is not None else (4096 if self._is_device else 1)
        self._identity = SourceIdentity(
            path=guard.normalize(self._path), size=self._size,
            mtime_ns=None if self._is_device else st.st_mtime_ns, is_device=self._is_device)
        log.info("opened source %s read-only (%d bytes, %s)", self._path, self._size,
                 "device" if self._is_device else "image file")
        if self._is_device:
            _warn_if_device_writable(self._fd, self._path)

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        return self._path

    @property
    def identity(self) -> SourceIdentity:
        return self._identity

    def pread(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("negative offset/length")
        if offset >= self._size or length == 0:
            return b""
        length = min(length, self._size - offset)
        if self._align > 1:
            start = offset - offset % self._align
            end = -(-(offset + length) // self._align) * self._align
            end = min(end, self._size) if self._size % self._align == 0 else end
            raw = self._raw_pread(start, end - start)
            return raw[offset - start: offset - start + length]
        return self._raw_pread(offset, length)

    def _raw_pread(self, offset: int, length: int) -> bytes:
        if hasattr(os, "pread"):
            chunks = []
            while length > 0:
                b = os.pread(self._fd, length, offset)
                if not b:
                    break
                chunks.append(b)
                offset += len(b)
                length -= len(b)
            return b"".join(chunks)
        fd = self._checkout()
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            chunks = []
            while length > 0:
                b = os.read(fd, length)
                if not b:
                    break
                chunks.append(b)
                length -= len(b)
            return b"".join(chunks)
        finally:
            with self._lock:
                self._free_fds.append(fd)

    def _checkout(self) -> int:
        with self._lock:
            if self._fd < 0:
                raise ValueError(f"{self._path}: source is closed")
            if self._free_fds:
                return self._free_fds.pop()
        fd = os.open(self._path, os.O_RDONLY | _O_BINARY | _O_NOINHERIT)
        with self._lock:
            self._all_fds.append(fd)
        return fd

    def verify_unchanged(self) -> None:
        """Raise :class:`SourceChanged` if size or mtime differ from open time."""
        if self._is_device:
            return
        st = os.stat(self._path)
        if st.st_size != self._identity.size or st.st_mtime_ns != self._identity.mtime_ns:
            raise SourceChanged(
                f"{self._path} changed while open (size {self._identity.size}->{st.st_size}, "
                f"mtime {self._identity.mtime_ns}->{st.st_mtime_ns})")

    def close(self) -> None:
        with self._lock:
            fds, self._all_fds, self._free_fds = self._all_fds, [], []
            self._fd = -1
        for fd in fds:
            os.close(fd)


class SliceSource(ReadOnlySource):
    """A window [offset, offset+size) of another source (a partition, a vdev...)."""

    def __init__(self, parent: ReadOnlySource, offset: int, size: int, name: str | None = None) -> None:
        if offset < 0 or size < 0 or offset + size > parent.size:
            raise ValueError(f"slice {offset:#x}+{size:#x} outside parent of size {parent.size:#x}")
        self._parent, self._offset, self._size = parent, offset, size
        self._name = name or f"{parent.name}@{offset:#x}"

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self) -> ReadOnlySource:
        return self._parent

    @property
    def offset(self) -> int:
        return self._offset

    def pread(self, offset: int, length: int) -> bytes:
        if offset >= self._size:
            return b""
        length = min(length, self._size - offset)
        return self._parent.pread(self._offset + offset, length)


def open_source(path: str | os.PathLike[str]) -> RawSource:
    return RawSource(path)


# ---------------------------------------------------------------- platform helpers

def _is_device_path(path: str) -> bool:
    return path.startswith("\\\\.\\") or path.startswith("/dev/")


def _device_size(fd: int, path: str) -> int:
    if sys.platform == "win32":
        return _win_device_size(fd)
    if sys.platform.startswith("linux"):
        import fcntl
        import struct
        BLKGETSIZE64 = 0x80081272
        buf = fcntl.ioctl(fd, BLKGETSIZE64, b"\0" * 8)
        return struct.unpack("Q", buf)[0]
    if sys.platform == "darwin":
        import fcntl
        import struct
        DKIOCGETBLOCKSIZE, DKIOCGETBLOCKCOUNT = 0x40046418, 0x40086419
        bs = struct.unpack("I", fcntl.ioctl(fd, DKIOCGETBLOCKSIZE, b"\0" * 4))[0]
        n = struct.unpack("Q", fcntl.ioctl(fd, DKIOCGETBLOCKCOUNT, b"\0" * 8))[0]
        return bs * n
    return os.lseek(fd, 0, os.SEEK_END)


def _win_device_size(fd: int) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes
    IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
    handle = msvcrt.get_osfhandle(fd)
    length = ctypes.c_longlong(0)
    returned = wintypes.DWORD(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        wintypes.HANDLE(handle), IOCTL_DISK_GET_LENGTH_INFO, None, 0,
        ctypes.byref(length), ctypes.sizeof(length), ctypes.byref(returned), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), "IOCTL_DISK_GET_LENGTH_INFO failed")
    return length.value


def _warn_if_device_writable(fd: int, path: str) -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        import fcntl
        import struct
        BLKROGET = 0x125E
        ro = struct.unpack("i", fcntl.ioctl(fd, BLKROGET, b"\0" * 4))[0]
        if not ro:
            log.warning("block device %s is not marked read-only at the kernel level. This tool "
                        "never writes to it, but for forensic hygiene consider "
                        "`blockdev --setro %s` before scanning.", path, path)
    except OSError:
        pass
