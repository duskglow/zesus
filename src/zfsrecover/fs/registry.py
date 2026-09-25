"""Discovery of filesystem plugins and signature identifiers via entry points."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .api import Device, FilesystemPlugin, FsInfo, Identifier

log = logging.getLogger(__name__)


def filesystem_plugins() -> list[FilesystemPlugin]:
    from .ext4 import Ext4Plugin
    plugins: list[FilesystemPlugin] = [Ext4Plugin()]
    names = {p.name for p in plugins}
    for ep in entry_points(group="zfsrecover.filesystems"):
        if ep.name in names:
            continue
        try:
            obj = ep.load()
            plugins.append(obj() if isinstance(obj, type) else obj)
            names.add(ep.name)
            log.debug("loaded filesystem plugin %s", ep.name)
        except Exception as exc:
            log.warning("filesystem plugin %s failed to load: %s", ep.name, exc)
    return plugins


def identifiers() -> list[Identifier]:
    from .identify import BUILTIN
    ids: list[Identifier] = list(BUILTIN)
    for ep in entry_points(group="zfsrecover.identifiers"):
        try:
            obj = ep.load()
            ids.append(obj() if isinstance(obj, type) else obj)
        except Exception as exc:
            log.warning("identifier plugin %s failed to load: %s", ep.name, exc)
    return ids


def detect(dev: Device) -> tuple[FilesystemPlugin | None, FsInfo | None]:
    """Return the best inventory-capable plugin, or else a signature identification."""
    best, best_score = None, 0.0
    for p in filesystem_plugins():
        try:
            s = p.probe(dev)
        except Exception as exc:
            log.debug("probe %s failed: %s", p.name, exc)
            continue
        if s > best_score:
            best, best_score = p, s
    if best is not None and best_score >= 0.5:
        return best, None
    for ident in identifiers():
        try:
            info = ident.identify(dev)
        except Exception:
            continue
        if info:
            return None, info
    return None, None
