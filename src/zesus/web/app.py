"""Optional local web UI: browse a map, run scans, and queue extractions.

    zesus web MAP [--source IMAGE ...] [--port 8765]

It binds to 127.0.0.1 by default. Browsing opens the map read-only. Jobs (scan, extract,
send) are the same zesus commands, run as separate processes that outlive the server
(see zesus.jobs). Each job reports progress (percent, rate, ETA, elapsed), streamed to
the page, and can be cancelled. Evidence is the images
given with --source, or else the member files recorded in the map. Either way it is
checked against the map and opened read-only.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATUS_ORDER = ["ok", "ok_stale", "embedded", "hole", "discarded", "unknown", "cksum_mismatch", "zeroed",
                "no_metadata", "unreadable", "decompress_fail"]


def create_app(map_path: str, source: str | list[str] | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    from .. import __version__
    from ..jobs import JobStore
    from ..map.db import MapDB

    app = FastAPI(title="Zesus", docs_url=None, redoc_url=None)
    sources = [source] if isinstance(source, str) else list(source or [])
    # The page this server was started with, and a build id (version + start) sent with
    # every response. A page from another build (the server was upgraded or restarted)
    # notices the change and reloads itself instead of breaking against a different API.
    BUILD = f"{__version__}+{uuid.uuid4().hex[:8]}"
    PAGE = resources.files("zesus.web").joinpath("index.html").read_text(encoding="utf-8") \
        .replace("__ZESUS_BUILD__", BUILD)

    @app.middleware("http")
    async def build_header(request, call_next):
        response = await call_next(request)
        response.headers["X-Zesus-Build"] = BUILD
        return response

    def evidence_paths() -> list[str]:
        """Evidence for jobs: --source, else the member files the map recorded (if present)."""
        if sources:
            return sources
        from ..evidence import recorded_paths
        d = db()
        try:
            return [x for x in recorded_paths(d) if Path(x).exists()]
        finally:
            d.close()

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
        return PAGE

    @app.get("/api/build")
    def build() -> dict:
        return {"build": BUILD, "version": __version__}

    @app.get("/api/overview")
    def overview() -> dict:
        pools = rows("SELECT id,name,guid,state,version,hostname,txg,ashift FROM pools")
        for p in pools:
            p["uberblocks"] = rows("SELECT txg,timestamp,checksum_ok,mos_ok FROM uberblocks WHERE pool_id=? "
                                   "ORDER BY txg DESC", (p["id"],))
            p["carved"] = {r["kind"]: r["n"] for r in rows(
                "SELECT kind, count(*) n FROM carved WHERE pool_id=? GROUP BY kind", (p["id"],))}
        ev = evidence_paths()
        members = None
        for r in rows("SELECT context FROM events WHERE component='members' ORDER BY id DESC LIMIT 1"):
            try:
                members = json.loads(r["context"] or "{}").get("report")
            except ValueError:
                members = None
        return {
            "map": map_path, "source": " + ".join(ev) if ev else None, "evidence": ev,
            "members": members,
            "pools": pools,
            "datasets": rows("SELECT id,pool_id,dsobj,name,kind,origin,status,creation_txg,creation_time,"
                             "destroy_txg,destroy_time,seen_txg FROM datasets ORDER BY name"),
            "volumes": rows("SELECT * FROM volumes"),
            "partitions": rows("SELECT * FROM partitions ORDER BY volume_id, idx"),
            "filesystems": rows("SELECT id,volume_id,partition_id,start,length,fstype,plugin,label,uuid,"
                                "block_size,state,info_json FROM filesystems"),
            "progress": rows("SELECT phase, count(*) n, max(finished_at) last, max(unit='*') complete "
                             "FROM progress WHERE state='done' GROUP BY phase"),
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

    @app.get("/api/losses")
    def losses() -> dict:
        """Affected files (with zero-filled ranges) and whether carving could recover more.
        Map only; `zesus losses --evidence` adds the per-block missing-member check."""
        from ..losses import build_report
        d = db()
        try:
            return build_report(d).as_dict()
        finally:
            d.close()

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict]:
        return rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ jobs
    # A job is a zesus command run as its own process (see zesus.jobs): restarting or
    # upgrading this server neither kills a job nor forgets it.
    store = JobStore(map_path)

    def progress_snapshots() -> dict[str, dict]:
        out = {}
        for r in rows("SELECT key, value FROM meta WHERE key LIKE 'progress:%'"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except ValueError:
                continue
        return out

    def job_list(snaps: dict | None = None) -> list[dict]:
        snaps = progress_snapshots() if snaps is None else snaps
        return [j for j in (store.get(jid, snaps.get(f"progress:job:{jid}")) for jid in store.ids()) if j]

    def external_progress(snaps: dict | None = None) -> list[dict]:
        """Commands run from a terminal publish their progress into the map too."""
        snaps = progress_snapshots() if snaps is None else snaps
        out, now = [], time.time()
        for key, snap in snaps.items():
            if key.startswith("progress:job:"):
                continue
            snap["stale"] = now - snap.get("updated", 0) > 120 and snap.get("state") == "running"
            age = now - snap.get("updated", 0)
            # finished runs for an hour; a "running" one that stopped reporting for a day
            if age < 3600 or (snap.get("state") == "running" and age < 86400):
                out.append(snap)
        return out

    def jobs_payload() -> dict:
        snaps = progress_snapshots()
        return {"jobs": job_list(snaps), "external": external_progress(snaps), "build": BUILD}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return jobs_payload()

    @app.get("/api/stream")
    async def stream(once: bool = False):
        """Server-sent events: the job list whenever it changes (checked every second).
        *once* ends the stream after the first list (for tests and scripts)."""
        import asyncio

        from starlette.concurrency import run_in_threadpool
        from starlette.responses import StreamingResponse

        async def gen():
            last, idle = None, 0
            yield f"event: hello\ndata: {json.dumps({'build': BUILD})}\n\n"
            while True:
                payload = json.dumps(await run_in_threadpool(jobs_payload), default=str)
                if payload != last:
                    last, idle = payload, 0
                    yield f"event: jobs\ndata: {payload}\n\n"
                    if once:
                        return
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keep-alive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str) -> dict:
        if not store.cancel(jid):
            raise HTTPException(404, "no such job")
        return {"id": jid, "cancelling": True}

    def running(kind: str) -> bool:
        return any(j["kind"] == kind and j["state"] == "running" for j in job_list())

    def start(kind: str, req: dict, argv: list[str]) -> dict:
        return {"id": store.start(kind, req, argv)}

    # ------------------------------------------------------------------ estimates
    @app.post("/api/estimate")
    def estimate(req: dict) -> dict:
        from ..extract.engine import estimate_files, estimate_volume_range
        d = db()
        try:
            kind = req.get("kind")
            if kind == "files":
                est = estimate_files(d, int(req["fs_id"]), req.get("paths") or None,
                                     set(req.get("statuses") or ["full", "partial", "none"]))
                out_bytes = est["recoverable_bytes"]
            elif kind in ("volume", "partition"):
                if kind == "volume":
                    v = d.execute("SELECT id, volsize FROM volumes WHERE id=?", (int(req["volume_id"]),)).fetchone()
                    vid, start_, length = v["id"], 0, v["volsize"]
                else:
                    pr = d.execute("SELECT * FROM partitions WHERE id=?", (int(req["partition_id"]),)).fetchone()
                    vid, start_, length = pr["volume_id"], pr["start"], pr["length"]
                est = estimate_volume_range(d, vid, start_, length)
                out_bytes = est["read_bytes"]
            else:
                raise HTTPException(400, f"unknown kind {kind!r}")
        finally:
            d.close()
        rate = measured_rate()
        est["rate"] = rate
        est["eta_s"] = (out_bytes / rate) if rate else None
        est["rate_source"] = "measured by an earlier job" if rate else None
        out = req.get("out")
        if out:
            est["free_bytes"] = free_space(out)
            est["out_problem"] = check_out(out)
        return est

    def measured_rate() -> float | None:
        """Throughput of the most recent extraction or verification (jobs or terminal runs)."""
        best, when = None, 0.0
        for snap in progress_snapshots().values():
            if snap.get("unit") == "bytes" and str(snap.get("stage", "")).startswith(("verify", "extract")) \
                    and snap.get("rate") and (snap.get("done") or 0) > (64 << 20) and snap.get("updated", 0) > when:
                best, when = snap["rate"], snap.get("updated", 0)
        return best

    def check_out(out: str) -> str | None:
        """Why *out* cannot be used as an output directory, or None."""
        from ..io import guard
        if not Path(out).is_absolute():
            return "use an absolute path for the output directory"
        try:
            guard.assert_not_protected([Path(out)])
        except Exception as exc:
            return str(exc)
        if any(guard.normalize(out) == guard.normalize(e) for e in evidence_paths()):
            return "the output directory is an evidence file"
        return None

    def need_evidence() -> list[str]:
        ev = evidence_paths()
        if not ev:
            raise HTTPException(400, "no evidence: start the web UI with --source IMAGE (one per member disk)")
        return ev

    # ------------------------------------------------------------------ extraction
    @app.post("/api/extract")
    def extract(req: dict) -> dict:
        out = req.get("out")
        if not out:
            raise HTTPException(400, "output directory required")
        if (problem := check_out(out)):
            raise HTTPException(400, problem)
        argv = ["extract", map_path, *need_evidence(), "-o", out, "--fill", req.get("fill", "zero")]
        if req.get("force"):
            argv.append("--force")
        if not req.get("hash", True):
            argv.append("--no-hash")
        kind = req.get("kind")
        if kind == "volume":
            argv += ["--volume", str(int(req["volume_id"]))]
        elif kind == "partition":
            pr = rows("SELECT volume_id, idx FROM partitions WHERE id=?", (int(req["partition_id"]),))
            if not pr:
                raise HTTPException(404, "no such partition")
            argv += ["--partition", f"{pr[0]['volume_id']}:{pr[0]['idx']}"]
        elif kind == "files":
            argv += ["--fs", str(int(req["fs_id"])),
                     "--status", ",".join(req.get("statuses") or ["full", "partial", "none"])]
            for pth in req.get("paths") or []:
                argv += ["--path", pth]
        else:
            raise HTTPException(400, f"unknown kind {kind!r}")
        return start("extract", req, argv)

    # ------------------------------------------------------------------ scans
    @app.post("/api/scan")
    def scan(req: dict) -> dict:
        from ..scan.pipeline import ORDER
        phases = [x for x in (req.get("phases") or []) if x]
        bad = [x for x in phases if x not in ORDER]
        if not phases or bad:
            raise HTTPException(400, f"choose phases from {ORDER}" + (f" (unknown: {bad})" if bad else ""))
        if running("scan") or running("recheck"):
            raise HTTPException(409, "a scan is already running")
        for snap in external_progress():
            if snap.get("state") == "running" and not snap.get("stale"):
                raise HTTPException(409, f"a scan is running in another process (pid {snap.get('pid')})")
        argv = ["scan", *need_evidence(), "-o", map_path, "--phases", ",".join(phases)]
        redo = [x for x in (req.get("redo") or []) if x in ORDER]
        if redo:
            argv += ["--redo", ",".join(redo)]
        if req.get("limit_blocks"):
            argv += ["--limit-blocks", str(int(req["limit_blocks"]))]
        return start("scan", req, argv)

    # ------------------------------------------------------------------ send (rsync)
    @app.get("/api/send/tools")
    def send_tools() -> dict:
        from ..extract.send import Tools
        try:
            return {"ok": True, "mode": Tools().mode}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/send")
    def send_files(req: dict) -> dict:
        if not req.get("dest") or not req.get("fs_id"):
            raise HTTPException(400, "destination and filesystem are required")
        staging = req.get("staging") or str(Path(map_path).resolve().parent / "staging")
        argv = ["send", map_path, *need_evidence(), "--fs", str(int(req["fs_id"])), "--to", req["dest"],
                "--staging", staging]
        for pth in req.get("paths") or []:
            argv += ["--path", pth]
        if req.get("ssh"):
            argv += ["--ssh", req["ssh"]]
        if req.get("batch"):
            argv += ["--batch", str(req["batch"])]
        for flag, opt in (("overwrite", "--overwrite"), ("include_partial", "--include-partial"),
                          ("dry_run", "--dry-run"), ("sudo", "--sudo"), ("verify", "--verify")):
            if req.get(flag):
                argv.append(opt)
        return start("send", req, argv)

    return app


def free_space(path: str) -> int | None:
    import shutil
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def serve(map_path: str, source: str | list[str] | None, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding to %s exposes evidence metadata on the network", host)
    log.info("web UI at http://%s:%d/", host, port)
    uvicorn.run(create_app(map_path, source), host=host, port=port, log_level="warning")


