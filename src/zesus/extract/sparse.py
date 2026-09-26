"""Mark output files sparse, so holes and gaps take no disk space.

POSIX filesystems make a file sparse automatically when it is extended by truncate/seek.
NTFS needs FSCTL_SET_SPARSE first. And on Windows, Python's ``truncate()`` must not be
used to *extend* a file: the C runtime implements that by writing zeros, which takes
hours and fills the disk for a large image. :func:`set_size` uses ``SetEndOfFile``
instead, which just sets the size.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def make_sparse(f) -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        FSCTL_SET_SPARSE = 0x900C4
        handle = msvcrt.get_osfhandle(f.fileno())
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(handle), FSCTL_SET_SPARSE, None, 0,
                                                    None, 0, ctypes.byref(returned), None)
        return bool(ok)
    except Exception as exc:  # pragma: no cover
        log.debug("could not mark %s sparse: %s", getattr(f, "name", f), exc)
        return False


def set_size(f, size: int) -> None:
    """Set the length of open file *f* without writing the new bytes."""
    if sys.platform != "win32":
        f.truncate(size)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    f.flush()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
    k32 = ctypes.windll.kernel32
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                                     wintypes.DWORD]
    k32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    pos = f.tell()
    if not k32.SetFilePointerEx(handle, size, None, 0) or not k32.SetEndOfFile(handle):
        raise OSError(ctypes.get_last_error(), f"could not set the size of {getattr(f, 'name', f)}")
    f.seek(pos)
