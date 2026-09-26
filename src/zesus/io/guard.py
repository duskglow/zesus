"""Process-wide enforcement that protected sources are never modified.

A single audit hook (PEP 578) is installed the first time a path is protected. Audit
hooks cannot be removed, so the hook consults a mutable registry; unprotecting a path
simply removes it from the registry.

The hook intercepts every ``open`` performed through Python (builtin ``open``,
``io.open``, ``os.open``) and a set of mutating ``os``/``shutil`` operations. If the target
resolves to a protected path and the operation could modify it, ``SourceWriteAttempt``
is raised *before* the operation happens.

This is defence in depth: :class:`zesus.io.source.ReadOnlySource` already opens
sources with ``O_RDONLY`` and exposes no write methods. The guard also catches bugs
elsewhere in the code, such as an extractor mistakenly pointed at the source path.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterable

from ..errors import SourceWriteAttempt

_lock = threading.Lock()
_protected: set[str] = set()
_installed = False

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_WRITE_MODE_CHARS = set("wax+")

# Audit events whose first argument is a path (or fd) that would be modified.
_MUTATING_EVENTS = {
    "os.truncate", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.utime",
    "os.chflags", "os.setxattr", "os.removexattr", "shutil.rmtree", "shutil.move",
}
# Events whose first two arguments are (src, dst); either being protected is a violation.
_TWO_PATH_EVENTS = {"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copymode",
                    "shutil.copystat", "shutil.copytree"}


def normalize(path: str | os.PathLike[str] | bytes) -> str:
    """Return a canonical form of *path* for comparison.

    Windows device paths (``\\\\.\\PhysicalDrive0``) and Linux ``/dev`` nodes are not
    resolved through ``realpath`` beyond symlinks, because that can mangle them.
    """
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    p = os.fspath(path)
    if p.startswith(("\\\\.\\", "\\\\?\\GLOBALROOT")):
        return p.lower()
    try:
        p = os.path.realpath(p)
    except (OSError, ValueError):
        p = os.path.abspath(p)
    return os.path.normcase(p)


def _is_protected(target: object) -> bool:
    if not _protected or isinstance(target, int) or target is None:
        return False
    try:
        return normalize(target) in _protected  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _hook(event: str, args: tuple) -> None:
    if not _protected:
        return
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writes = False
        if isinstance(mode, str) and _WRITE_MODE_CHARS & set(mode):
            writes = True
        if isinstance(flags, int) and flags & _WRITE_FLAGS:
            writes = True
        if writes and _is_protected(path):
            raise SourceWriteAttempt(f"refusing to open protected source for writing: {path!r}")
    elif event in _MUTATING_EVENTS:
        if args and _is_protected(args[0]):
            raise SourceWriteAttempt(f"refusing {event} on protected source: {args[0]!r}")
    elif event in _TWO_PATH_EVENTS:
        for a in args[:2]:
            if _is_protected(a):
                raise SourceWriteAttempt(f"refusing {event} involving protected source: {a!r}")


def _ensure_installed() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def protect(path: str | os.PathLike[str]) -> str:
    """Register *path* as a read-only source for the lifetime of the process."""
    norm = normalize(path)
    with _lock:
        _ensure_installed()
        _protected.add(norm)
    return norm


def unprotect(path: str | os.PathLike[str]) -> None:
    with _lock:
        _protected.discard(normalize(path))


def protected_paths() -> frozenset[str]:
    return frozenset(_protected)


def assert_not_protected(paths: Iterable[str | os.PathLike[str]]) -> None:
    """Raise if any of *paths* (intended outputs) is a protected source."""
    for p in paths:
        if normalize(p) in _protected:
            raise SourceWriteAttempt(f"output path is a protected source: {os.fspath(p)!r}")
