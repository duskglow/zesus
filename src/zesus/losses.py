"""What was lost, where, and could anything more bring it back?

Three questions an owner asks once a scan is done, answered from the map:

* **Which files are affected?** For each partially or wholly lost file: its size, the bytes
  lost, and, for files inside a volume, the exact byte ranges that will be zero-filled.
* **Could carving recover more?** Carving reads every sector, so it is worth it only when
  it can plausibly add something. That depends on whether the uberblock ring still reaches
  the volume's final state, and whether older versions of the lost blocks can exist.
* **Could a missing member help?** (with the evidence) A lost block with a column on a
  member that is absent might come back once that disk is imaged. A block whose every
  column was read and still failed cannot.

Only the map is needed for the first two. The third reads the level-1 block pointers of
the lost blocks from the evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .map.codes import LOST, BlockStatus
from .map.db import MapDB

_LOST = {s.name.lower() for s in LOST}


@dataclass
class FileLoss:
    fs_id: int
    path: str
    status: str                        # partial | none
    size: int
    lost_bytes: int
    ranges: list[tuple[int, int]] = field(default_factory=list)   # (offset, length) in the file
    lost_blocks: list[int] = field(default_factory=list)          # volume block ids
    missing_member_could_help: int | None = None                  # lost blocks with a column on an absent disk


@dataclass
class CarveAssessment:
    volume_id: int
    volume: str
    worth_it: str                      # "no" | "yes" | "maybe" | "done"
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class LossReport:
    files: list[FileLoss] = field(default_factory=list)
    carve: list[CarveAssessment] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)
    members_checked: bool = False
    member_note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- per-file losses

def _lost_runs(db: MapDB, volume_id: int) -> list[tuple[int, int, str]]:
    return [(r[0], r[0] + r[1], r[2]) for r in db.execute(
        "SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=? ORDER BY first_blkid",
        (volume_id,)) if r[2] in _LOST]


def file_losses(db: MapDB, fs_id: int | None = None) -> list[FileLoss]:
    out: list[FileLoss] = []
    q = "SELECT * FROM filesystems WHERE plugin IS NOT NULL"
    for fs in db.execute(q + (" AND id=?" if fs_id else ""), (fs_id,) if fs_id else ()).fetchall():
        vid = fs["volume_id"]
        runs, bs = [], 0
        if vid is not None:
            runs = _lost_runs(db, vid)
            bs = db.execute("SELECT volblocksize FROM volumes WHERE id=?", (vid,)).fetchone()[0]
        for e in db.execute("SELECT id, path, size, recoverable_bytes, status FROM fs_entries WHERE fs_id=? "
                            "AND type='file' AND status IN ('partial','none') ORDER BY path", (fs["id"],)):
            size = e["size"] or 0
            fl = FileLoss(fs["id"], e["path"], e["status"], size,
                          max(0, size - (e["recoverable_bytes"] or 0)))
            if runs:
                fl.ranges, fl.lost_blocks = _file_ranges(db, e["id"], runs, bs)
            else:
                fl.ranges = [(r[0] or 0, r[1] or 0) for r in db.execute(
                    "SELECT start, length FROM unrecoverable WHERE scope='file' AND ref_id=? AND start IS NOT NULL",
                    (e["id"],))]
            out.append(fl)
    return out


def _file_ranges(db: MapDB, entry_id: int, runs: list, bs: int) -> tuple[list[tuple[int, int]], list[int]]:
    ranges: list[tuple[int, int]] = []
    blocks: set[int] = set()
    for fo, ln, vo in db.execute("SELECT file_offset, length, volume_offset FROM fs_extents WHERE entry_id=? "
                                 "AND kind='data' ORDER BY file_offset", (entry_id,)):
        if vo is None or not ln:
            continue
        b0, b1 = vo // bs, (vo + ln - 1) // bs + 1
        for a, b, _st in runs:
            if b <= b0 or a >= b1:
                continue
            s, e = max(vo, a * bs), min(vo + ln, b * bs)
            if s < e:
                ranges.append((fo + s - vo, e - s))
                blocks.update(range(max(a, b0), min(b, b1)))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for o, n in ranges:
        if merged and merged[-1][0] + merged[-1][1] >= o:
            po, pn = merged[-1]
            merged[-1] = (po, max(po + pn, o + n) - po)
        else:
            merged.append((o, n))
    return merged, sorted(blocks)


# ---------------------------------------------------------------- carve assessment

def carve_assessment(db: MapDB, volume_id: int, lost_births: list[int] | None = None) -> CarveAssessment:
    v = db.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
    ca = CarveAssessment(volume_id, v["name"], "maybe")
    ds = db.execute("SELECT * FROM datasets WHERE id=?", (v["dataset_id"],)).fetchone() if v["dataset_id"] else None
    roots = db.execute("SELECT txg, provenance FROM volume_roots WHERE volume_id=?", (volume_id,)).fetchall()
    ring = [r["txg"] for r in roots if str(r["provenance"]).startswith("ring")]
    carved = [r["txg"] for r in roots if not str(r["provenance"]).startswith("ring")]
    carve_done = db.is_done("carve")
    lost = (v["n_missing"] or 0) + (v["n_damaged"] or 0)
    f = ca.facts
    f.update(lost_blocks=lost, tree_versions=len(roots), ring_versions=len(ring), carved_versions=len(carved),
             newest_root_txg=max((r["txg"] for r in roots), default=None), carve_done=carve_done)
    if ds is not None:
        f.update(destroy_txg=ds["destroy_txg"], last_seen_txg=ds["seen_txg"], status=ds["status"])
    if lost_births:
        f.update(lost_birth_min=min(lost_births), lost_birth_max=max(lost_births))

    if carve_done:
        ca.worth_it = "done"
        ca.reasons.append("carving has already run; every generation it found is in the map")
        return ca
    if not ring:
        ca.worth_it = "yes"
        ca.reasons.append("no uberblock in the ring reaches this volume: carving is the only source of its "
                          "tree versions")
        return ca
    newest = max(ring)
    seen, destroyed = f.get("last_seen_txg"), f.get("destroy_txg")
    final = seen is not None and (destroyed is None or destroyed - seen <= 64)
    if final:
        ca.reasons.append(f"the ring still reaches the volume at txg {seen}"
                          + (f", just before it was destroyed at txg {destroyed}" if destroyed else "")
                          + f"; its newest tree version was written at txg {newest}, so this is its final state")
    else:
        gap = (destroyed - (seen or newest)) if destroyed else None
        ca.reasons.append(f"the ring's newest view of the volume is txg {seen or newest}"
                          + (f", {gap} txgs before it was destroyed" if gap else "")
                          + "; later writes may exist only as carved tree versions")
    if lost == 0:
        ca.worth_it = "no" if final else "maybe"
        ca.reasons.append("nothing is lost" + ("" if final else ", but carving could find a newer state"))
        return ca
    if lost_births:
        ca.reasons.append(
            f"the lost blocks were last written between txg {min(lost_births)} and {max(lost_births)}. ZFS never "
            "keeps old copies of a data block in place, so an older version exists only if the block was "
            "rewritten, and then only where its old location has not been reused since")
    ca.worth_it = "no" if final else "maybe"
    if final:
        ca.reasons.append("carving cannot find a newer state. It could only find older versions of rewritten "
                          "blocks (recovered as older data, marked ok_stale); write-once files such as "
                          "checkpoints and archives have none")
    return ca


# ---------------------------------------------------------------- with evidence

def member_help(pool, db: MapDB, volume_id: int) -> tuple[dict[int, bool], dict[int, int]]:
    """For each lost block of a volume: does it have a column on an absent member? Also
    returns each lost block's birth txg. Reads only the level-1 pointers of lost blocks."""
    from .carve.verify import l1_children, load_spans
    from .zfs import blkptr, raidz
    helps: dict[int, bool] = {}
    births: dict[int, int] = {}
    for sp in load_spans(db, volume_id).values():
        bad = [s for s in range(sp.count) if sp.status[s] in (BlockStatus.CKSUM_MISMATCH, BlockStatus.ZEROED,
                                                            BlockStatus.UNREADABLE)]
        if not bad:
            continue
        cand = sp.cands[sp.choice[bad[0]]][0] if sp.choice[bad[0]] != 255 else None
        words = l1_children(pool, cand, sp.count) if cand is not None else None
        if words is None:
            # the level-1 block itself is unreadable with this evidence: we cannot tell
            # where its children live, so a missing member might well help
            for s in bad:
                helps[sp.first + s] = True
            continue
        for s in bad:
            bp = blkptr.parse(words[s].tobytes())
            births[sp.first + s] = bp.birth
            could = False
            for dva in bp.valid_dvas:
                top = pool.vdevs.top.get(dva.vdev)
                if top is None:
                    could = True
                elif top.type == "raidz":
                    rm = raidz.map_alloc(dva.offset, bp.psize, top.ashift, top.width, top.nparity)
                    could |= any(top.slots[c.devidx] is None for c in rm.cols)
                elif top.type == "mirror":
                    could |= bool(top.missing)
            helps[sp.first + s] = could
    return helps, births


def build_report(db: MapDB, pool=None, fs_id: int | None = None) -> LossReport:
    rep = LossReport(files=file_losses(db, fs_id))
    births_by_vol: dict[int, list[int]] = {}
    helps_all: dict[int, dict[int, bool]] = {}
    vols = db.execute("SELECT * FROM volumes").fetchall()
    if pool is not None:
        rep.members_checked = True
        missing = sum(len(t.missing) for t in pool.vdevs.top.values())
        rep.member_note = (f"{missing} member(s) absent from the evidence given" if missing
                           else "every member is present: nothing more can come from another disk")
        for v in vols:
            helps, births = member_help(pool, db, v["id"])
            helps_all[v["id"]] = helps
            births_by_vol[v["id"]] = list(births.values())
    for v in vols:
        rep.volumes.append({"id": v["id"], "name": v["name"], "block_size": v["volblocksize"],
                            "verified": v["n_verified"], "never_written": v["n_holes"], "stale": v["n_stale"],
                            "lost": (v["n_missing"] or 0) + (v["n_damaged"] or 0)})
        rep.carve.append(carve_assessment(db, v["id"], births_by_vol.get(v["id"])))
    if pool is not None:
        fs_vol = {r[0]: r[1] for r in db.execute("SELECT id, volume_id FROM filesystems")}
        for fl in rep.files:
            h = helps_all.get(fs_vol.get(fl.fs_id), {})
            if fl.lost_blocks:
                fl.missing_member_could_help = sum(1 for b in fl.lost_blocks if h.get(b))
    return rep


# ---------------------------------------------------------------- rendering

def human(n: int | float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def markdown(rep: LossReport, ranges: bool = True) -> str:
    L = ["# Losses", ""]
    for v in rep.volumes:
        bs = v["block_size"] or 0
        L.append(f"* **{v['name']}**: {v['verified']:,} blocks verified, {v['never_written']:,} never written, "
                 f"{v['stale'] or 0:,} from an older version, **{v['lost']:,} lost ({human(v['lost'] * bs)})**")
    none = [f for f in rep.files if f.status == "none"]
    part = [f for f in rep.files if f.status == "partial"]
    L += ["", f"**{len(rep.files)} file(s) affected**: {len(none)} unrecoverable, {len(part)} partial, "
          f"{human(sum(f.lost_bytes for f in rep.files))} lost in total.", ""]
    helpcol = rep.members_checked and any(f.missing_member_could_help is not None for f in rep.files)
    if none:
        L += ["## Unrecoverable", "", "| File | Size |" + (" Could a missing disk help |" if helpcol else ""),
              "|---|---:|" + ("---|" if helpcol else "")]
        for f in none:
            L.append(f"| `{f.path}` | {human(f.size)} |" + (f" {_help(f)} |" if helpcol else ""))
        L.append("")
    if part:
        L += ["## Partial (the listed ranges are zero-filled)", "",
              "| File | Size | Lost |" + (" Zero-filled ranges (offset+length) |" if ranges else "")
              + (" Could a missing disk help |" if helpcol else ""),
              "|---|---:|---:|" + ("---|" if ranges else "") + ("---|" if helpcol else "")]
        for f in part:
            rng = ", ".join(f"{o:#x}+{n:#x}" for o, n in f.ranges[:8]) + \
                (f", … ({len(f.ranges)} ranges)" if len(f.ranges) > 8 else "")
            L.append(f"| `{f.path}` | {human(f.size)} | {human(f.lost_bytes)} |" + (f" {rng} |" if ranges else "")
                     + (f" {_help(f)} |" if helpcol else ""))
        L.append("")
    L += ["## Could carving recover more?", ""]
    for c in rep.carve:
        L.append(f"**{c.volume}: {dict(no='no', yes='yes', maybe='possibly', done='already done')[c.worth_it]}.** "
                 + " ".join(r[0].upper() + r[1:] + "." for r in c.reasons))
        L.append("")
    if rep.members_checked:
        L += ["## Missing members", "", rep.member_note[0].upper() + rep.member_note[1:] + ".", ""]
    return "\n".join(L)


def _help(f: FileLoss) -> str:
    if f.missing_member_could_help is None:
        return "?"
    n = len(f.lost_blocks)
    return "no" if not f.missing_member_could_help else f"{f.missing_member_could_help} of {n} blocks"


def to_json(rep: LossReport) -> str:
    return json.dumps(rep.as_dict(), indent=1)
