"""`zesus report`: a human-readable account of what was found, what can be
recovered, and what cannot (and why), plus a per-file CSV."""

from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path

from ..map.codes import DESCRIPTIONS, BlockStatus
from ..map.db import MapDB
from ..volume.coverage import Coverage

_REASON = {s.name.lower(): DESCRIPTIONS[s] for s in BlockStatus}


def _ts(t) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "?"


def _h(n) -> str:
    if n is None:
        return "?"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def write_report(db: MapDB, out_md: Path, csv_path: Path | None = None, max_items: int = 200) -> None:
    L: list[str] = []
    w = L.append
    w("# ZFS recovery report\n")
    w(f"Generated {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} from map `{db.path}`.\n")
    src = db.execute("SELECT * FROM sources ORDER BY id DESC LIMIT 1").fetchone()
    if src:
        w(f"Source: `{src['path']}` ({_h(src['size'])}). The source was opened read-only.\n")

    for p in db.execute("SELECT * FROM pools"):
        w(f"## Pool `{p['name']}`\n")
        ubs = db.execute("SELECT count(*), min(txg), max(txg), sum(checksum_ok), sum(coalesce(mos_ok,0)) "
                         "FROM uberblocks WHERE pool_id=?", (p["id"],)).fetchone()
        w(f"- state **{p['state']}**, last txg {p['txg']}, ashift {p['ashift']}, host `{p['hostname']}`")
        w(f"- uberblocks: {ubs[0]} (txg {ubs[1]}–{ubs[2]}), {ubs[3]} valid, {ubs[4]} with a readable MOS")
        carved = db.execute("SELECT kind, count(*) FROM carved WHERE pool_id=? GROUP BY kind", (p["id"],)).fetchall()
        if carved:
            w("- carved metadata blocks: " + ", ".join(f"{k} {n}" for k, n in carved))
        w("")
        w("### Datasets\n")
        w("| Dataset | Kind | Status | Found via | Created | Destroyed |")
        w("|---|---|---|---|---|---|")
        for d in db.execute("SELECT * FROM datasets WHERE pool_id=? ORDER BY name, creation_txg", (p["id"],)):
            destroyed = f"txg {d['destroy_txg']} ({_ts(d['destroy_time'])})" if d["destroy_txg"] else ""
            w(f"| `{d['name']}` | {d['kind']} | {d['status']} | {d['origin']} | txg {d['creation_txg']} "
              f"({_ts(d['creation_time'])}) | {destroyed} |")
        w("")

    for v in db.execute("SELECT * FROM volumes"):
        n = v["n_blocks"] or 0
        bs = v["volblocksize"]
        w(f"## Volume `{v['name']}`\n")
        w(f"{_h(v['volsize'])}, {bs // 1024} KiB blocks, {n} logical blocks, map status **{v['status']}**.\n")
        roots = db.execute("SELECT provenance, txg FROM volume_roots WHERE volume_id=? ORDER BY txg DESC",
                           (v["id"],)).fetchall()
        if roots:
            w(f"Built from {len(roots)} tree generation(s), newest txg {roots[0]['txg']} "
              f"({roots[0]['provenance']}), oldest txg {roots[-1]['txg']}.\n")
        w("| Block status | Blocks | Bytes | Share | Meaning |")
        w("|---|---:|---:|---:|---|")
        for st, cnt in db.execute("SELECT status, sum(count) FROM volume_coverage WHERE volume_id=? GROUP BY status "
                                  "ORDER BY sum(count) DESC", (v["id"],)):
            w(f"| {st} | {cnt} | {_h(cnt * bs)} | {100 * cnt / max(1, n):.3f}% | {_REASON.get(st, '')} |")
        w("")
        unver = db.execute("SELECT sum(count) FROM volume_coverage WHERE volume_id=? AND status='unknown'",
                           (v["id"],)).fetchone()[0]
        if unver:
            w(f"> {unver} blocks are mapped but not yet checksum-verified: run the `verify` phase. "
              "Extraction verifies every block regardless.\n")
        gaps = db.execute("SELECT * FROM volume_coverage WHERE volume_id=? AND status NOT IN "
                          "('ok','ok_stale','embedded','hole','discarded','unknown') ORDER BY first_blkid", (v["id"],)).fetchall()
        if gaps:
            w(f"### Unrecoverable ranges ({len(gaps)})\n")
            w("| Volume offset | Length | Reason |")
            w("|---:|---:|---|")
            for g in gaps[:max_items]:
                w(f"| {g['first_blkid'] * bs:#x} | {_h(g['count'] * bs)} | {g['reason']} |")
            if len(gaps) > max_items:
                w(f"\n…and {len(gaps) - max_items} more (see `volume_coverage` in the map).")
            w("")
        for part in db.execute("SELECT * FROM partitions WHERE volume_id=? ORDER BY idx", (v["id"],)):
            w(f"- Partition {part['idx']}: {part['type_name']} at {part['start']:#x}, {_h(part['length'])}, "
              f"**{100 * (part['coverage'] or 0):.3f}%** recoverable")
        w("")
        cov = Coverage(db, v["id"])
        for fs in db.execute("SELECT * FROM filesystems WHERE volume_id=?", (v["id"],)):
            _fs_section(db, fs, cov, w, max_items)

    zpl = db.execute("SELECT * FROM filesystems WHERE dataset_id IS NOT NULL").fetchall()
    if zpl:
        w("## ZFS filesystem datasets\n")
        for fs in zpl:
            _fs_section(db, fs, None, w, max_items)

    out_md.write_text("\n".join(L) + "\n", encoding="utf-8")
    if csv_path:
        _write_csv(db, csv_path)


def _fs_section(db: MapDB, fs, cov: Coverage | None, w, max_items: int) -> None:
    info = json.loads(fs["info_json"] or "{}")
    where = f"dataset {fs['label']}" if fs["dataset_id"] else f"volume offset {fs['start']:#x}"
    w(f"### Filesystem {fs['id']}: {fs['fstype']} ({where})\n")
    w(f"UUID `{fs['uuid']}`, label `{fs['label'] or ''}`, state **{fs['state']}**.")
    for warn in info.get("warnings", []):
        w(f"\n> ⚠ {warn}")
    w("")
    if fs["plugin"] is None:
        w("Identified by signature only (no inventory plugin). Extract it as a partition or volume image.\n")
        return
    w("| Type | Status | Count | Size |")
    w("|---|---|---:|---:|")
    for r in db.execute("SELECT type, status, count(*) n, sum(size) s FROM fs_entries WHERE fs_id=? "
                        "GROUP BY type, status ORDER BY type, status", (fs["id"],)):
        w(f"| {r['type']} | {r['status']} | {r['n']} | {_h(r['s'] or 0)} |")
    w("")
    probs = db.execute("SELECT detail, reason FROM unrecoverable WHERE scope='filesystem' AND ref_id=?",
                       (fs["id"],)).fetchall()
    if probs:
        w(f"#### Metadata that could not be read ({len(probs)})\n")
        for p in probs[:max_items]:
            w(f"- `{p['detail']}`: {p['reason']}")
        w("")
    partial = db.execute("SELECT * FROM fs_entries WHERE fs_id=? AND status='partial' ORDER BY size DESC",
                         (fs["id"],)).fetchall()
    if partial:
        w(f"#### Partially recoverable files ({len(partial)})\n")
        w("Lost byte ranges are shown with the recoverable data around them.\n")
        for e in partial[:max_items]:
            lost = _file_lost_ranges(db, cov, e["id"])
            pct = 100 * (e["recoverable_bytes"] or 0) / max(1, e["size"] or 0)
            w(f"- `{e['path']}`: {_h(e['size'])}, {pct:.1f}% recoverable")
            for a, n, st in lost[:10]:
                w(f"  - lost bytes {a:,}–{a + n - 1:,} ({_h(n)}): {_REASON.get(st, st)}")
            if len(lost) > 10:
                w(f"  - …{len(lost) - 10} more lost ranges")
        w("")
    none = db.execute("SELECT path, size FROM fs_entries WHERE fs_id=? AND status='none' ORDER BY size DESC",
                      (fs["id"],)).fetchall()
    if none:
        w(f"#### Unrecoverable files ({len(none)})\n")
        for e in none[:max_items]:
            w(f"- `{e['path']}` ({_h(e['size'])})")
        if len(none) > max_items:
            w(f"- …and {len(none) - max_items} more (see the CSV)")
        w("")


def _file_lost_ranges(db: MapDB, cov: Coverage | None, entry_id: int) -> list[tuple[int, int, str]]:
    out = []
    for x in db.execute("SELECT * FROM fs_extents WHERE entry_id=? AND status!='ok' AND kind='data' "
                        "ORDER BY file_offset", (entry_id,)):
        if x["volume_offset"] is None or cov is None:      # ZPL file: the gap itself is stored
            out.append((x["file_offset"], x["length"], x["status"]))
            continue
        for a, n, st in cov.lost_ranges(x["volume_offset"], x["length"]):
            out.append((x["file_offset"] + (a - x["volume_offset"]), n, st))
    return out


def _write_csv(db: MapDB, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        cw = csv.writer(f)
        cw.writerow(["fs_id", "path", "type", "size", "status", "recoverable_bytes", "deleted", "mtime", "inode"])
        for r in db.execute("SELECT fs_id, path, type, size, status, recoverable_bytes, deleted, mtime, inode "
                            "FROM fs_entries ORDER BY fs_id, path"):
            cw.writerow(list(r))
