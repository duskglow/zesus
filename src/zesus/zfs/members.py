"""Which evidence file is which vdev member, and can the pool still be read?"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pool import Pool


@dataclass
class MemberRow:
    top: int
    child: int | None
    guid: int
    devid: str
    config_path: str
    file: str | None           # evidence file, None if missing
    state: str                 # present | missing | stale | duplicate
    note: str = ""
    max_txg: int | None = None


@dataclass
class TopReport:
    id: int
    type: str
    width: int
    nparity: int
    ashift: int
    rows: list[MemberRow] = field(default_factory=list)
    verdict: str = ""
    readable: bool = True


@dataclass
class MemberReport:
    pool: str
    pool_guid: int
    txg: int
    tops: list[TopReport] = field(default_factory=list)
    missing_tops: int = 0

    @property
    def readable(self) -> bool:
        return self.missing_tops == 0 and all(t.readable for t in self.tops)

    def as_dict(self) -> dict[str, Any]:
        return {"pool": self.pool, "pool_guid": str(self.pool_guid), "txg": self.txg,
                "readable": self.readable, "missing_tops": self.missing_tops,
                "tops": [{"id": t.id, "type": t.type, "width": t.width, "nparity": t.nparity,
                          "ashift": t.ashift, "verdict": t.verdict, "readable": t.readable,
                          "members": [{**r.__dict__, "guid": str(r.guid)} for r in t.rows]}
                         for t in self.tops]}

    def lines(self) -> list[str]:
        out = [f"pool {self.pool} (guid {self.pool_guid}), newest txg {self.txg}"]
        for t in self.tops:
            geo = f"raidz{t.nparity} width {t.width}" if t.type == "raidz" else t.type
            out.append(f"  vdev {t.id}: {geo}, ashift {t.ashift}: {t.verdict}")
            for r in t.rows:
                where = r.file or "-"
                extra = f"  ({r.note})" if r.note else ""
                out.append(f"    child {r.child if r.child is not None else '?':>2}  {r.state:<9} {where}"
                           f"  guid {r.guid}  {r.devid or r.config_path}{extra}")
        if self.missing_tops:
            out.append(f"  {self.missing_tops} top-level vdev(s) have no member present at all")
        out.append("  verdict: " + ("readable" if self.readable else "NOT fully readable"))
        return out


def assess(pool: Pool) -> MemberReport:
    rep = MemberReport(pool=pool.name, pool_guid=pool.guid, txg=pool.max_txg)
    by_guid = {im.guid: im for im in pool.vdev_images}
    dups = {}
    for d in pool.duplicates:
        dups.setdefault(d.guid, []).append(d)
    for vid, top in sorted(pool.vdevs.top.items()):
        tr = TopReport(id=vid, type=top.type, width=top.width, nparity=top.nparity, ashift=top.ashift)
        tree = _tree_for(pool, vid)
        kids = (tree or {}).get("children") or ([tree] if tree else [])
        for pos, ch in enumerate(kids):
            cid = ch.get("id", pos) if (tree or {}).get("children") else 0
            leaf = next((lf for lf in top.children if lf.child_id == cid), None)
            g = leaf.guid if leaf else ch.get("guid", 0)
            im = by_guid.get(g)
            row = MemberRow(top=vid, child=cid, guid=g, devid=ch.get("devid", ""),
                            config_path=ch.get("path", ""), file=im.name if im else None,
                            state="present" if im else "missing", max_txg=im.max_txg if im else None)
            if im and im.max_txg < pool.max_txg:
                row.state = "stale"
                row.note = f"last txg {im.max_txg}, pool at {pool.max_txg}"
            tr.rows.append(row)
            for d in dups.get(g, []):
                tr.rows.append(MemberRow(top=vid, child=cid, guid=g, devid=ch.get("devid", ""),
                                         config_path=ch.get("path", ""), file=d.name, state="duplicate",
                                         note=f"same member as {d.duplicate_of.name}: {d.duplicate_note}"
                                         if d.duplicate_of else d.duplicate_note))
        absent = sum(1 for r in tr.rows if r.state == "missing")
        stale = sum(1 for r in tr.rows if r.state == "stale")
        tr.verdict, tr.readable = _verdict(top.type, top.nparity, len(kids), absent, stale, top.unsupported)
        rep.tops.append(tr)
    rep.missing_tops = max(0, pool.config.get("vdev_children", 1) - len(pool.vdevs.top))
    return rep


def _verdict(typ: str, nparity: int, width: int, absent: int, stale: int,
             unsupported: str | None) -> tuple[str, bool]:
    if unsupported:
        return f"unsupported: {unsupported}", False
    st = f"; {stale} stale member(s): blocks written after they dropped out will need parity" if stale else ""
    if typ == "raidz":
        if absent == 0:
            return "intact" + st, True
        if absent <= nparity:
            left = nparity - absent
            red = "no redundancy left" if left == 0 else f"{left} parity column(s) of redundancy left"
            return (f"DEGRADED but recoverable: {absent} of {width} members missing, rebuilt from "
                    f"parity ({red})" + st), True
        return f"NOT recoverable: {absent} members missing, parity covers only {nparity}", False
    if typ == "mirror":
        if absent < width:
            return ("intact" if absent == 0 else f"degraded: {width - absent} of {width} copies present") + st, True
        return "NOT recoverable: no mirror copy present", False
    return ("intact" if absent == 0 else "NOT recoverable: device missing"), absent == 0


def _tree_for(pool: Pool, vid: int) -> dict[str, Any] | None:
    for im in sorted(pool.vdev_images, key=lambda im: -im.max_txg):
        t = (im.config or {}).get("vdev_tree")
        if t and t.get("id", 0) == vid:
            return t
    return None
