"""Optional local web UI: browse a map and queue extractions.

    zfsrecover web MAP [--source IMAGE] [--port 8765]

It binds to 127.0.0.1 only. The map is opened read-only, and extraction jobs run in a
background thread using the same engine as the CLI.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATUS_ORDER = ["ok", "ok_stale", "embedded", "hole", "discarded", "unknown", "cksum_mismatch", "zeroed",
                "no_metadata", "unreadable", "decompress_fail"]


def create_app(map_path: str, source: str | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    from ..map.db import MapDB

    app = FastAPI(title="zfs-forensic-recovery", docs_url=None, redoc_url=None)
    lock = threading.Lock()
    jobs: dict[str, dict[str, Any]] = {}

    def db() -> MapDB:
        return MapDB(map_path, readonly=True)

    def rows(sql: str, params=()) -> list[dict]:
        d = db()
        try:
            return [dict(r) for r in d.execute(sql, params).fetchall()]
        finally:
            d.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("zfsrecover.web").joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/api/overview")
    def overview() -> dict:
        pools = rows("SELECT id,name,guid,state,version,hostname,txg,ashift FROM pools")
        for p in pools:
            p["uberblocks"] = rows("SELECT txg,timestamp,checksum_ok,mos_ok FROM uberblocks WHERE pool_id=? "
                                   "ORDER BY txg DESC", (p["id"],))
            p["carved"] = {r["kind"]: r["n"] for r in rows(
                "SELECT kind, count(*) n FROM carved WHERE pool_id=? GROUP BY kind", (p["id"],))}
        return {
            "map": map_path, "source": source,
            "pools": pools,
            "datasets": rows("SELECT id,pool_id,dsobj,name,kind,origin,status,creation_txg,creation_time,"
                             "destroy_txg,destroy_time,seen_txg FROM datasets ORDER BY name"),
            "volumes": rows("SELECT * FROM volumes"),
            "partitions": rows("SELECT * FROM partitions ORDER BY volume_id, idx"),
            "filesystems": rows("SELECT id,volume_id,partition_id,start,length,fstype,plugin,label,uuid,"
                                "block_size,state,info_json FROM filesystems"),
            "progress": rows("SELECT phase, count(*) n, max(finished_at) last FROM progress WHERE state='done' "
                             "GROUP BY phase"),
        }

    @app.get("/api/volumes/{vid}/coverage")
    def coverage(vid: int, buckets: int = 400) -> dict:
        v = rows("SELECT n_blocks, volblocksize FROM volumes WHERE id=?", (vid,))
        if not v:
            raise HTTPException(404)
        n = v[0]["n_blocks"] or 1
        buckets = max(10, min(buckets, 4000))
        per = max(1, -(-n // buckets))
        agg = [dict.fromkeys(STATUS_ORDER, 0) for _ in range(-(-n // per))]
        for r in rows("SELECT first_blkid, count, status FROM volume_coverage WHERE volume_id=?", (vid,)):
            a, e = r["first_blkid"], r["first_blkid"] + r["count"]
            while a < e:
                b = a // per
                nxt = min(e, (b + 1) * per)
                agg[b][r["status"]] = agg[b].get(r["status"], 0) + (nxt - a)
                a = nxt
        return {"blocks_per_bucket": per, "block_size": v[0]["volblocksize"], "buckets": agg,
                "legend": STATUS_ORDER}

    @app.get("/api/volumes/{vid}/gaps")
    def gaps(vid: int, limit: int = 500) -> list[dict]:
        return rows("SELECT first_blkid, count, status, reason FROM volume_coverage WHERE volume_id=? AND status "
                    "NOT IN ('ok','ok_stale','embedded','hole','discarded') ORDER BY count DESC LIMIT ?", (vid, limit))

    @app.get("/api/fs/{fid}/ls")
    def ls(fid: int, path: str = "/") -> dict:
        path = "/" + path.strip("/")
        me = rows("SELECT * FROM fs_entries WHERE fs_id=? AND path=? LIMIT 1", (fid, path))
        if not me:
            raise HTTPException(404, f"{path} not found")
        kids = []
        if me[0]["type"] == "dir":
            prefix = path.rstrip("/") + "/"
            kids = rows("SELECT id,name,path,type,size,mtime,status,recoverable_bytes,deleted,link_target "
                        "FROM fs_entries WHERE fs_id=? AND parent_inode=? AND path LIKE ? ORDER BY type!='dir', name",
                        (fid, me[0]["inode"], prefix + "%"))
            if path == "/":
                kids += rows("SELECT id,name,path,type,size,mtime,status,recoverable_bytes,deleted,link_target "
                             "FROM fs_entries WHERE fs_id=? AND (path LIKE '/$orphans/%' OR path LIKE '/$deleted/%') "
                             "ORDER BY path LIMIT 2000", (fid,))
        return {"entry": me[0], "children": kids}

    @app.get("/api/fs/{fid}/search")
    def search(fid: int, q: str = "", status: str = "", limit: int = 500) -> list[dict]:
        sql = "SELECT id,path,type,size,mtime,status,recoverable_bytes,deleted FROM fs_entries WHERE fs_id=?"
        params: list[Any] = [fid]
        if q:
            sql += " AND path LIKE ?"
            params.append(f"%{q}%")
        if status:
            sql += f" AND status IN ({','.join('?' * len(status.split(',')))})"
            params += status.split(",")
        sql += " ORDER BY path LIMIT ?"
        params.append(min(limit, 5000))
        return rows(sql, params)

    @app.get("/api/fs/{fid}/stats")
    def fs_stats(fid: int) -> dict:
        return {"by_status": rows("SELECT type, status, count(*) n, sum(size) bytes FROM fs_entries WHERE fs_id=? "
                                  "GROUP BY type, status", (fid,)),
                "problems": rows("SELECT detail, reason FROM unrecoverable WHERE scope='filesystem' AND ref_id=? "
                                 "LIMIT 200", (fid,))}

    @app.get("/api/entry/{eid}")
    def entry(eid: int) -> dict:
        e = rows("SELECT * FROM fs_entries WHERE id=?", (eid,))
        if not e:
            raise HTTPException(404)
        return {"entry": e[0], "extents": rows("SELECT * FROM fs_extents WHERE entry_id=? ORDER BY file_offset "
                                               "LIMIT 2000", (eid,))}

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict]:
        return rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ extraction jobs
    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        with lock:
            return [{k: v for k, v in j.items() if k != "thread"} for j in jobs.values()]

    @app.post("/api/extract")
    def extract(req: dict) -> dict:
        if not source:
            raise HTTPException(400, "start the web UI with --source to enable extraction")
        out = req.get("out")
        if not out:
            raise HTTPException(400, "output directory required")
        jid = uuid.uuid4().hex[:8]
        job = {"id": jid, "request": req, "state": "queued", "started": time.time(), "message": "", "outputs": []}

        def run() -> None:
            from ..extract.engine import Extractor, Options
            from ..io.source import RawSource
            from ..zfs.pool import open_pools
            job["state"] = "running"
            try:
                d = MapDB(map_path, readonly=True)
                src = RawSource(source)
                pool = open_pools(src)[0]
                ex = Extractor(d, pool, Options(out_dir=Path(out), fill=req.get("fill", "zero"),
                                                force=bool(req.get("force"))))
                kind = req.get("kind")
                if kind == "volume":
                    v = d.execute("SELECT * FROM volumes WHERE id=?", (int(req["volume_id"]),)).fetchone()
                    ex.extract_volume_range(v["id"], 0, v["volsize"], v["name"].replace("/", "_") + ".img",
                                            "volume", v["name"])
                elif kind == "partition":
                    p = d.execute("SELECT * FROM partitions WHERE id=?", (int(req["partition_id"]),)).fetchone()
                    ex.extract_volume_range(p["volume_id"], p["start"], p["length"],
                                            f"volume{p['volume_id']}-part{p['idx']}.img", "partition",
                                            f"volume {p['volume_id']} partition {p['idx']}")
                elif kind == "files":
                    ex.extract_files(int(req["fs_id"]), patterns=req.get("paths") or None,
                                     statuses=set(req.get("statuses") or ["full", "partial", "none"]))
                else:
                    raise ValueError(f"unknown kind {kind!r}")
                ex.write_manifest({"map": map_path, "source": source})
                job["outputs"] = [{"path": r.path, "status": r.status, "lost_bytes": r.lost_bytes}
                                  for r in ex.records]
                job["state"] = "done"
                job["message"] = f"{len(ex.records)} outputs"
            except Exception as exc:
                job["state"] = "failed"
                job["message"] = f"{exc}"
                log.error("job %s failed: %s", jid, traceback.format_exc())
            job["finished"] = time.time()

        t = threading.Thread(target=run, daemon=True)
        with lock:
            jobs[jid] = job
        t.start()
        return {"id": jid}

    return app


def serve(map_path: str, source: str | None, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding to %s exposes evidence metadata on the network", host)
    log.info("web UI at http://%s:%d/", host, port)
    uvicorn.run(create_app(map_path, source), host=host, port=port, log_level="warning")


