"""Partition-table plugins.

A scheme plugin is a class with:

* ``name``: a short identifier (``"gpt"``, ``"mbr"``...);
* ``probe(dev) -> bool``;
* ``parse(dev) -> list[Partition]``.

Plugins read through a ``BlockReader`` (``pread(offset, length) -> bytes``). They receive
no file handles and have no access to the database.

Third-party schemes register under the ``zfsrecover.partitions`` entry-point group.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Protocol

log = logging.getLogger(__name__)


class BlockReader(Protocol):
    @property
    def size(self) -> int: ...
    def pread(self, offset: int, length: int) -> bytes: ...


@dataclass
class Partition:
    scheme: str
    index: int
    start: int          # bytes
    length: int         # bytes
    type_id: str        # GUID string for GPT, hex byte for MBR
    type_name: str
    name: str = ""
    uuid: str = ""
    flags: dict = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.start + self.length


class PartitionScheme(Protocol):
    name: str
    def probe(self, dev: BlockReader) -> bool: ...
    def parse(self, dev: BlockReader) -> list[Partition]: ...


def load_schemes() -> list[PartitionScheme]:
    from .gpt import GptScheme
    from .mbr import MbrScheme
    schemes: list[PartitionScheme] = [GptScheme(), MbrScheme()]
    seen = {s.name for s in schemes}
    for ep in entry_points(group="zfsrecover.partitions"):
        if ep.name in seen:
            continue
        try:
            schemes.append(ep.load()())
            seen.add(ep.name)
        except Exception as exc:  # a broken plugin must not break scanning
            log.warning("partition plugin %s failed to load: %s", ep.name, exc)
    return schemes


def detect(dev: BlockReader) -> tuple[str | None, list[Partition]]:
    """Return (scheme name, partitions) for the first scheme that recognizes *dev*."""
    for s in load_schemes():
        try:
            if s.probe(dev):
                return s.name, s.parse(dev)
        except Exception as exc:
            log.warning("partition scheme %s failed on %s: %s", s.name, getattr(dev, "name", dev), exc)
    return None, []
