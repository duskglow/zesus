"""Human-readable summaries of a map."""

from __future__ import annotations

import datetime as _dt
from typing import TextIO

from ..map.db import MapDB


def _ts(t: int | None) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "?"


def _gib(n: int | None) -> str:
    return f"{n / (1 << 30):.1f} GiB" if n else "?"


def print_info(db: MapDB, out: TextIO) -> None:
    w = out.write
    for p in db.execute("SELECT * FROM pools"):
        w(f"Pool {p['name']}  guid={p['guid']}  state={p['state']}  txg={p['txg']}  ashift={p['ashift']}\n")
        for v in db.execute("SELECT * FROM vdevs WHERE pool_id=?", (p["id"],)):
            w(f"  vdev {v['top_id']}: {v['type']} {v['path']}  at source offset {v['base_phys']:#x}  "
              f"asize={_gib(v['asize'])}\n")
        ubs = db.execute("SELECT min(txg), max(txg), count(*), sum(checksum_ok) FROM uberblocks WHERE pool_id=?",
                         (p["id"],)).fetchone()
        w(f"  uberblocks: {ubs[2]} (txg {ubs[0]}..{ubs[1]}), {ubs[3]} with valid checksums\n")
        rows = db.execute("SELECT * FROM datasets WHERE pool_id=? ORDER BY status, name", (p["id"],)).fetchall()
        if rows:
            w("  datasets:\n")
            for d in rows:
                w(f"    [{d['status']:9}] {d['name'] or '?':40} dsobj={d['dsobj']} kind={d['kind']} "
                  f"origin={d['origin']} created txg {d['creation_txg']} ({_ts(d['creation_time'])})")
                if d["destroy_txg"]:
                    w(f" destroyed txg {d['destroy_txg']} ({_ts(d['destroy_time'])})")
                w("\n")
        carved = db.execute("SELECT kind, count(*) FROM carved WHERE pool_id=? GROUP BY kind", (p["id"],)).fetchall()
        if carved:
            w("  carved blocks: " + ", ".join(f"{k}={n}" for k, n in carved) + "\n")
    for v in db.execute("SELECT * FROM volumes"):
        w(f"Volume {v['name']}: volsize={_gib(v['volsize'])} volblocksize={v['volblocksize']} "
          f"status={v['status']}\n")
        n = v["n_blocks"] or 0
        if n:
            def pct(x: int | None) -> str:
                return f"{100 * (x or 0) / n:.2f}%"
            w(f"  blocks: {n}  verified={v['n_verified']} ({pct(v['n_verified'])})  "
              f"holes/unwritten={v['n_holes']} ({pct(v['n_holes'])})  stale={v['n_stale']}  "
              f"missing={v['n_missing']}  damaged={v['n_damaged']}\n")
    phases = db.execute("SELECT phase, count(*) FROM progress WHERE state='done' GROUP BY phase").fetchall()
    w("Progress: " + ", ".join(f"{ph}={n}" for ph, n in phases) + "\n")
