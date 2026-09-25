"""Mark output files sparse, so holes and gaps take no disk space.

POSIX filesystems make a file sparse automatically when it is extended by truncate/seek.
NTFS needs FSCTL_SET_SPARSE first.
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
