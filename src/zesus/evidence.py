"""Open the evidence a map was built from, and the pool in it.

Extraction re-reads and re-verifies every block, so it needs the same evidence as the
scan: one file for a single-disk pool, or one per member disk. Callers may name the
files. Otherwise the member paths recorded in the map are used. Either way, each file
is checked against what the map recorded about it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from .io.sourceset import SourceSet
from .map.db import MapDB
from .zfs.pool import Pool, open_pools

log = logging.getLogger(__name__)


class EvidenceError(RuntimeError):
    pass


def recorded_paths(db: MapDB) -> list[str]:
    """Member files recorded by the most recent scan (duplicates excluded)."""
    has_role = db.has_column("sources", "role")
    rows = db.execute("SELECT path, opened_at" + (", role" if has_role else "") +
                      " FROM sources ORDER BY id").fetchall()
    if not rows:
        return []
    last = rows[-1]["opened_at"]
    paths = [r["path"] for r in rows if r["opened_at"] == last
             and not (has_role and r["role"] == "duplicate")]
    return list(dict.fromkeys(paths))


def check_against_map(db: MapDB, ss: SourceSet) -> None:
    """Refuse evidence whose size differs from what the map recorded.

    A single-disk map is compared as before, with the most recent source row. A member
    file is compared with the rows recorded under its path, or else with any row of the
    same size.
    """
    rows = db.execute("SELECT path, size FROM sources ORDER BY id").fetchall()
    if not rows:
        return
    if len(ss) == 1 and len({r["path"] for r in rows}) == 1:
        if rows[-1]["size"] != ss.members[0].size:
            raise EvidenceError(f"source size {ss.members[0].size} does not match the map's source "
                                f"({rows[-1]['size']})")
        return
    for m in ss:
        same = [r for r in rows if r["path"] == m.name]
        if same and all(r["size"] != m.size for r in same):
            raise EvidenceError(f"{m.name}: size {m.size} does not match the map ({same[-1]['size']})")
        if not same and not any(r["size"] == m.size for r in rows):
            raise EvidenceError(f"{m.name}: no file of this size was scanned into this map")


def open_evidence(db: MapDB, paths: Sequence[str] | None) -> SourceSet:
    paths = list(paths or [])
    if not paths:
        paths = [p for p in recorded_paths(db) if os.path.exists(p)]
        if not paths:
            raise EvidenceError("no evidence given, and none of the files recorded in the map exist "
                                "here; name the image(s) the map was built from")
        log.info("using the evidence recorded in the map: %s", ", ".join(paths))
    ss = SourceSet.open(paths)
    try:
        check_against_map(db, ss)
    except BaseException:
        ss.close()
        raise
    return ss


def pool_for_map(db: MapDB, ss: SourceSet) -> Pool:
    """The pool of this map, found in the evidence."""
    pools = open_pools(ss)
    if not pools:
        raise EvidenceError("no ZFS pool found in the evidence")
    guids = [r[0] for r in db.execute("SELECT guid FROM pools")]
    for p in pools:
        if str(p.guid) in guids:
            return p
    if guids:
        raise EvidenceError(f"the evidence holds pool(s) {[p.name for p in pools]}, not the map's pool")
    return pools[0]
