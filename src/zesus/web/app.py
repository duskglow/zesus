"""Optional local web UI: browse a map, run scans, and queue extractions.

    zesus web MAP [--source IMAGE ...] [--port 8765]

It binds to 127.0.0.1 by default. Browsing opens the map read-only. Jobs (scan, extract,
send) run on background threads using the same engines as the CLI. Each job reports
progress (percent, rate, ETA, elapsed) and can be cancelled. Evidence is the images
given with --source, or else the member files recorded in the map. Either way it is
checked against the map and opened read-only.
"""

from __future__ import annotations

import json
import logging
import os
import sys
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


def create_app(map_path: str, source: str | list[str] | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    from ..map.db import MapDB

    app = FastAPI(title="Zesus", docs_url=None, redoc_url=None)
    lock = threading.Lock()
    jobs: dict[str, Job] = {}
    sources = [source] if isinstance(source, str) else list(source or [])
    rates: dict[str, float] = {}        # measured output throughput (bytes/s) by job kind

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

    def open_evidence_for(d):
        from ..evidence import EvidenceError, open_evidence, pool_for_map
        paths = evidence_paths()
        if not paths:
            raise HTTPException(400, "no evidence: start the web UI with --source IMAGE (one per member disk)")
        try:
            ss = open_evidence(d, paths)
            return ss, pool_for_map(d, ss)
        except EvidenceError as exc:
            raise HTTPException(400, str(exc)) from exc

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
        return resources.files("zesus.web").joinpath("index.html").read_text(encoding="utf-8")

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

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict]:
        return rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ jobs
    def start_job(kind: str, req: dict, fn) -> dict:
        job = Job(kind, req)
        with lock:
            if kind == "scan" and any(j.kind == "scan" and j.state in ("queued", "running") for j in jobs.values()):
                raise HTTPException(409, "a scan is already running")
            jobs[job.id] = job

        def run() -> None:
            job.state = "running"
            try:
                fn(job)
                if job.stop.is_set():
                    job.state = "cancelled"
                elif job.state == "running":
                    job.state = "done"
            except HTTPException as exc:
                job.state, job.message = "failed", str(exc.detail)
            except Exception as exc:
                job.state, job.message = "failed", str(exc)
                log.error("job %s failed: %s", job.id, traceback.format_exc())
            job.finished = time.time()
            job.progress.finish(job.state, job.message or job.state)

        threading.Thread(target=run, daemon=True, name=f"zesus-job-{job.id}").start()
        return {"id": job.id}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        with lock:
            mine = [j.as_dict() for j in jobs.values()]
        return {"jobs": mine, "external": external_progress()}

    def external_progress() -> list[dict]:
        """Scans run from the command line publish progress into the map's meta table."""
        out = []
        now = time.time()
        for r in rows("SELECT key, value FROM meta WHERE key LIKE 'progress:%'"):
            try:
                snap = json.loads(r["value"])
            except ValueError:
                continue
            if snap.get("pid") == os.getpid():
                continue
            snap["stale"] = now - snap.get("updated", 0) > 120 and snap.get("state") == "running"
            if snap.get("state") == "running" or now - snap.get("updated", 0) < 3600:
                out.append(snap)
        return out

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str) -> dict:
        with lock:
            job = jobs.get(jid)
        if job is None:
            raise HTTPException(404, "no such job")
        job.stop.set()
        job.progress.note("cancelling: finishing the current step")
        return {"id": jid, "state": job.state}

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
                    vid, start, length = v["id"], 0, v["volsize"]
                else:
                    pr = d.execute("SELECT * FROM partitions WHERE id=?", (int(req["partition_id"]),)).fetchone()
                    vid, start, length = pr["volume_id"], pr["start"], pr["length"]
                est = estimate_volume_range(d, vid, start, length)
                out_bytes = est["read_bytes"]
            else:
                raise HTTPException(400, f"unknown kind {kind!r}")
        finally:
            d.close()
        rate = rates.get("extract") or measured_rate()
        est["rate"] = rate
        est["eta_s"] = (out_bytes / rate) if rate else None
        est["rate_source"] = "measured by an earlier job" if rate else None
        out = req.get("out")
        if out:
            est["free_bytes"] = free_space(out)
            est["out_problem"] = check_out(out)
        return est

    def measured_rate() -> float | None:
        best = None
        for snap in external_progress():
            if snap.get("unit") == "bytes" and str(snap.get("stage", "")).startswith(("verify", "extract")) \
                    and snap.get("rate"):
                best = snap["rate"]
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

    # ------------------------------------------------------------------ extraction
    @app.post("/api/extract")
    def extract(req: dict) -> dict:
        out = req.get("out")
        if not out:
            raise HTTPException(400, "output directory required")
        if (problem := check_out(out)):
            raise HTTPException(400, problem)
        if not evidence_paths():
            raise HTTPException(400, "no evidence: start the web UI with --source IMAGE (one per member disk)")

        def run(job: Job) -> None:
            from ..extract.engine import Extractor, Options
            d = MapDB(map_path, readonly=True)
            ss, pool = open_evidence_for(d)
            try:
                ex = Extractor(d, pool, Options(out_dir=Path(out), fill=req.get("fill", "zero"),
                                                force=bool(req.get("force")), hash_outputs=bool(req.get("hash", True))),
                               progress=job.progress, stop=job.stop)
                t0 = time.monotonic()
                kind = req.get("kind")
                if kind == "volume":
                    v = d.execute("SELECT * FROM volumes WHERE id=?", (int(req["volume_id"]),)).fetchone()
                    ex.extract_volume_range(v["id"], 0, v["volsize"], v["name"].replace("/", "_") + ".img",
                                            "volume", v["name"])
                elif kind == "partition":
                    pr = d.execute("SELECT * FROM partitions WHERE id=?", (int(req["partition_id"]),)).fetchone()
                    ex.extract_volume_range(pr["volume_id"], pr["start"], pr["length"],
                                            f"volume{pr['volume_id']}-part{pr['idx']}.img", "partition",
                                            f"volume {pr['volume_id']} partition {pr['idx']}")
                elif kind == "files":
                    ex.extract_files(int(req["fs_id"]), patterns=req.get("paths") or None,
                                     statuses=set(req.get("statuses") or ["full", "partial", "none"]))
                else:
                    raise ValueError(f"unknown kind {kind!r}")
                ex.write_manifest({"map": map_path, "source": ss.name, "sources": [m.name for m in ss]})
                written = sum(r.size - r.lost_bytes for r in ex.records if r.kind in ("file", "volume", "partition"))
                dt = time.monotonic() - t0
                if written > (64 << 20) and dt > 1 and not ex.cancelled:
                    rates["extract"] = written / dt
                job.outputs = [{"path": r.path, "status": r.status, "lost_bytes": r.lost_bytes,
                                "size": r.size, "cancelled": bool(r.extra.get("cancelled"))} for r in ex.records]
                full = sum(1 for r in ex.records if r.status == "full")
                job.message = (f"{len(ex.records)} outputs ({full} full)"
                               + ("; CANCELLED: see manifest.json for what was not written" if ex.cancelled else ""))
                ss.verify_unchanged()
            finally:
                ss.close()
                d.close()

        return start_job("extract", req, run)

    # ------------------------------------------------------------------ scans
    @app.post("/api/scan")
    def scan(req: dict) -> dict:
        from ..scan.pipeline import ORDER
        phases = [x for x in (req.get("phases") or []) if x]
        bad = [x for x in phases if x not in ORDER]
        if not phases or bad:
            raise HTTPException(400, f"choose phases from {ORDER}" + (f" (unknown: {bad})" if bad else ""))
        for snap in external_progress():
            if snap.get("state") == "running" and not snap.get("stale"):
                raise HTTPException(409, f"a scan is running in another process (pid {snap.get('pid')})")
        if not evidence_paths():
            raise HTTPException(400, "no evidence: start the web UI with --source IMAGE (one per member disk)")

        def run(job: Job) -> None:
            from ..io.sourceset import SourceSet
            from ..progress import MapPublisher
            from ..scan import pipeline
            ss = SourceSet.open(evidence_paths())
            d = MapDB(map_path)
            pub = MapPublisher(map_path)
            job.progress.listen(pub, 2.0)
            try:
                redo = {x for x in (req.get("redo") or []) if x in ORDER}
                for name in redo:
                    d.reset_phase(name)
                d.commit()
                opts = {"redo": redo, "progress_interval": 30,
                        "limit_blocks": int(req["limit_blocks"]) if req.get("limit_blocks") else None}
                ctx = pipeline.ScanContext(db=d, source=ss, stop=_StopAdapter(job.stop), progress=job.progress,
                                           options=opts)
                pipeline.run(ctx, phases)
                job.message = "stopped; run again to resume" if job.stop.is_set() else f"phases {', '.join(phases)} done"
            finally:
                # publish the final state for other viewers before letting go of the map
                failed = sys.exc_info()[0] is not None
                pub({**job.progress.snapshot(),
                     "state": "failed" if failed else ("cancelled" if job.stop.is_set() else "done")})
                d.commit()
                d.close()
                pub.close()
                ss.close()

        return start_job("scan", req, run)

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
        import shlex

        if not evidence_paths():
            raise HTTPException(400, "no evidence: start the web UI with --source IMAGE (one per member disk)")
        if not req.get("dest") or not req.get("fs_id"):
            raise HTTPException(400, "destination and filesystem are required")
        def run(job: Job) -> None:
            from ..extract.send import SendOptions, send

            def progress(msg: str) -> None:
                job.log = (job.log + [msg])[-40:]
                job.message = msg
                job.progress.note(msg)

            d = MapDB(map_path, readonly=True)
            ss, pool = open_evidence_for(d)
            try:
                opts = SendOptions(
                    dest=req["dest"], staging=Path(req.get("staging") or Path(map_path).resolve().parent / "staging"),
                    ssh_args=shlex.split(req.get("ssh") or ""), overwrite=bool(req.get("overwrite")),
                    include_partial=bool(req.get("include_partial")), dry_run=bool(req.get("dry_run")),
                    sudo=bool(req.get("sudo")))
                res = send(d, pool, int(req["fs_id"]), req.get("paths") or None, opts, progress=progress)
                verb = "would send" if res.dry_run else "sent"
                job.message = (f"{verb} {res.sent} files; metadata restored on {res.restored}; held back "
                               f"{res.held_back_partial} partial, {res.unrecoverable} unrecoverable")
                job.result = res.__dict__
                if res.errors:
                    job.state = "failed"
                    job.message += " | " + " | ".join(res.errors[:3])
                ss.verify_unchanged()
            finally:
                ss.close()
                d.close()

        return start_job("send", req, run)

    return app


class Job:
    """A background job: its request, state, outputs, progress and stop event."""

    def __init__(self, kind: str, request: dict) -> None:
        from ..progress import Progress
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.request = kind, request
        self.state, self.message = "queued", ""
        self.started, self.finished = time.time(), None
        self.outputs: list[dict] = []
        self.log: list[str] = []
        self.result: dict | None = None
        self.stop = threading.Event()
        self.progress = Progress(kind)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "request": self.request, "state": self.state,
                "message": self.message, "started": self.started, "finished": self.finished,
                "outputs": self.outputs[:200], "n_outputs": len(self.outputs), "log": self.log[-12:],
                "result": self.result, "cancelling": self.stop.is_set() and self.state == "running",
                "progress": self.progress.snapshot()}


class _StopAdapter:
    """The scan pipeline expects an object with an ``event`` attribute."""

    def __init__(self, ev: threading.Event) -> None:
        self.event = ev


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


