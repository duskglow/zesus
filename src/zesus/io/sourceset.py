"""An ordered set of read-only evidence files: one per vdev member disk.

A single-disk scan is a set of one, and behaves exactly as a lone :class:`RawSource`
did. Every member is opened through :class:`RawSource`, so every member path is
registered with the write guard.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence

from .source import RawSource, SourceIdentity


class SourceSet:
    def __init__(self, members: Sequence[RawSource]) -> None:
        if not members:
            raise ValueError("no evidence sources given")
        self.members = list(members)

    @classmethod
    def open(cls, paths: Sequence[str | os.PathLike[str]]) -> SourceSet:
        opened: list[RawSource] = []
        seen: set[str] = set()
        try:
            for p in paths:
                src = RawSource(p)
                if src.identity.path in seen:
                    src.close()
                    raise ValueError(f"source given twice: {p}")
                seen.add(src.identity.path)
                opened.append(src)
        except BaseException:
            for s in opened:
                s.close()
            raise
        return cls(opened)

    def __iter__(self) -> Iterator[RawSource]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    @property
    def name(self) -> str:
        return " + ".join(m.name for m in self.members)

    @property
    def identity(self) -> SourceIdentity:
        """Identity of the first member (single-disk compatibility)."""
        return self.members[0].identity

    @property
    def identities(self) -> list[SourceIdentity]:
        return [m.identity for m in self.members]

    def verify_unchanged(self) -> None:
        for m in self.members:
            m.verify_unchanged()

    def close(self) -> None:
        for m in self.members:
            m.close()

    def __enter__(self) -> SourceSet:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def as_sources(src: RawSource | SourceSet | Sequence) -> list:
    """Normalize a source, a set, or a list of sources to a list of sources."""
    if isinstance(src, SourceSet):
        return list(src.members)
    if isinstance(src, (list, tuple)):
        return list(src)
    return [src]
