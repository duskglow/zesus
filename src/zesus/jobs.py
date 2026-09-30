"""Background jobs that outlive the web server.

A job is an ordinary ``zesus`` command (scan, extract, send, recheck), started as a
detached process. Everything about it lives next to the map, in ``<map>.jobs/``:

* ``<id>.json``: the request, the command line, the process id, when it started;
* ``<id>.log``: its output;
* ``<id>.exit.json``: written by the job when it ends (exit code, last message);
* ``<id>.cancel``: created to ask it to stop. The job checks for it once a second and
  stops at the next safe point, exactly as it would on Ctrl-C.

Its progress is published into the map's ``meta`` table under ``progress:job:<id>``.

So restarting or upgrading the web server neither kills a job nor forgets it: a new
server lists the directory and reads the progress.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

JOB_ENV = "ZESUS_JOB_ID"
JOB_DIR_ENV = "ZESUS_JOB_DIR"


def jobs_dir(map_path: str | Path) -> Path:
    return Path(str(map_path) + ".jobs")


class JobStore:
    def __init__(self, map_path: str | Path) -> None:
        self.map_path = Path(map_path)
        self.dir = jobs_dir(map_path)

    # ------------------------------------------------------------------ starting
    def start(self, kind: str, request: dict, argv: list[str]) -> str:
        """Run ``zesus <argv>`` in the background; return the job id."""
        self.dir.mkdir(parents=True, exist_ok=True)
        jid = uuid.uuid4().hex[:8]
        rec = {"id": jid, "kind": kind, "request": request, "argv": argv, "pid": None, "started": time.time()}
        (self.dir / f"{jid}.json").write_text(json.dumps(rec), encoding="utf-8")
        # Started through a short-lived launcher: the job then has no living parent, so
        # whatever stops the server (a terminal, a service manager, an IDE killing its
        # process tree) does not stop the job with it.
        launcher = _spawn([sys.executable, "-m", "zesus.jobs", "launch", str(self.dir), jid],
                          open(os.devnull, "wb"), dict(os.environ))
        try:
            launcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        return jid

    # ------------------------------------------------------------------ reading
    def ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        recs = []
        for f in self.dir.glob("*.json"):
            if f.name.endswith(".exit.json"):
                continue
            try:
                recs.append((json.loads(f.read_text(encoding="utf-8"))["started"], f.stem))
            except (OSError, ValueError, KeyError):
                continue
        return [jid for _t, jid in sorted(recs)]

    def get(self, jid: str, progress: dict | None = None) -> dict | None:
        f = self.dir / f"{jid}.json"
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ex = self._exit(jid)
        starting = rec.get("pid") is None and time.time() - rec.get("started", 0) < 60
        alive = ex is None and (starting or pid_alive(rec.get("pid")))
        cancel_asked = (self.dir / f"{jid}.cancel").exists()
        if ex is not None:
            state = ex.get("state") or ("done" if ex.get("code") == 0 else "failed")
        elif alive:
            state = "running"
        else:
            state = "failed"
            ex = {"message": "the job's process ended without reporting (killed or crashed); see its log"}
        rec.update(state=state, cancelling=cancel_asked and state == "running",
                   message=(ex or {}).get("message") or (progress or {}).get("message") or "",
                   finished=(ex or {}).get("finished"), progress=progress, log=self.tail(jid, 12),
                   outputs=(ex or {}).get("outputs", []))
        return rec

    def _exit(self, jid: str) -> dict | None:
        try:
            return json.loads((self.dir / f"{jid}.exit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def tail(self, jid: str, n: int) -> list[str]:
        try:
            with open(self.dir / f"{jid}.log", "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 16384))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        return [ln for ln in lines if ln.strip()][-n:]

    # ------------------------------------------------------------------ control
    def cancel(self, jid: str) -> bool:
        if not (self.dir / f"{jid}.json").exists():
            return False
        (self.dir / f"{jid}.cancel").write_text(str(time.time()), encoding="utf-8")
        return True


def _spawn(cmd: list[str], out, env: dict) -> subprocess.Popen:
    """Start *cmd* detached from this process's console, process group and (on Windows)
    job object where that is allowed."""
    kw: dict[str, Any] = {}
    flags = 0
    if sys.platform == "win32":
        # A hidden console (CREATE_NO_WINDOW), not none (DETACHED_PROCESS): the job's own
        # child processes (carving workers) then share it instead of each opening a window.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        kw["creationflags"] = flags | subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                                close_fds=True, **kw)
    except PermissionError:
        kw["creationflags"] = flags          # the job object forbids breaking away
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                                close_fds=True, **kw)


def _launch(job_dir: str, jid: str) -> None:
    """The launcher: start the job itself, record its pid, and exit at once."""
    d = Path(job_dir)
    rec_path = d / f"{jid}.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    env = {**os.environ, JOB_ENV: jid, JOB_DIR_ENV: str(d), "PYTHONUNBUFFERED": "1"}
    with open(d / f"{jid}.log", "wb") as log:
        p = _spawn([sys.executable, "-m", "zesus.cli.main", *rec["argv"]], log, env)
    rec["pid"] = p.pid
    tmp = rec_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, rec_path)


# ---------------------------------------------------------------------- in the job process

def job_id() -> str | None:
    return os.environ.get(JOB_ENV)


def watch_for_cancel(event: threading.Event) -> None:
    """In a job process: set *event* when the web UI asks this job to stop."""
    jid, d = os.environ.get(JOB_ENV), os.environ.get(JOB_DIR_ENV)
    if not jid or not d:
        return
    flag = Path(d) / f"{jid}.cancel"

    def loop() -> None:
        while not event.is_set():
            if flag.exists():
                event.set()
                return
            time.sleep(1)
    threading.Thread(target=loop, daemon=True, name="zesus-cancel-watch").start()


def write_exit(code: int, message: str = "", state: str | None = None, *, overwrite: bool = True,
               **extra: Any) -> None:
    jid, d = os.environ.get(JOB_ENV), os.environ.get(JOB_DIR_ENV)
    if not jid or not d:
        return
    if not overwrite and (Path(d) / f"{jid}.exit.json").exists():
        return
    rec = {"code": code, "message": message, "finished": time.time(), **extra}
    if state:
        rec["state"] = state
    try:
        (Path(d) / f"{jid}.exit.json").write_text(json.dumps(rec, default=str), encoding="utf-8")
    except OSError:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "launch":
        _launch(sys.argv[2], sys.argv[3])
