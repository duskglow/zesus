"""Send recovered files back where they belong: stage → rsync → restore names and metadata.

Why three steps:
* **stage**: files are extracted (and re-verified) into a local staging directory. It is
  resumable: files already staged with the right size are kept.
* **rsync**: moves them to the destination (``user@host:/path`` over ssh, or a local
  path), resumably and without clobbering existing files unless asked.
* **restore**: the staging filesystem may not be able to represent the originals. NTFS has
  no Unix owners or modes, forbids ``:*?"<>|`` in names, and is case-insensitive. So a
  POSIX shell script is generated from the map, which is the authoritative record, and run
  on the destination. It:
    1. renames escaped or disambiguated names back to the originals;
    2. recreates symlinks;
    3. restores owners (with ``sudo``), modes and mtimes.
  The script is saved next to the staging area so it can be reviewed or re-run.

Safety defaults:
* existing destination files are never overwritten unless ``overwrite`` is set;
* partially recovered files are held back unless ``include_partial`` is set;
* unrecoverable files are never sent;
* metadata is applied only to what this run created, so an existing tree keeps its own
  permissions;
* gap reports, manifests and Windows symlink stubs stay local.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from ..map.db import MapDB
from ..zfs.pool import Pool
from .engine import Extractor, Options

log = logging.getLogger(__name__)


@dataclass
class SendOptions:
    dest: str                                   # "user@host:/path" or a local directory
    staging: Path
    ssh_args: list[str] = field(default_factory=list)   # e.g. ["-p", "2222", "-i", "/home/me/.ssh/key"]
    overwrite: bool = False
    include_partial: bool = False
    dry_run: bool = False
    sudo: bool = False                          # remote rsync + restore script as root (sudo -n)
    metadata: bool = True
    owners: bool | None = None                  # chown; default: only with sudo


@dataclass
class SendResult:
    staged: int = 0
    sent: int = 0
    held_back_partial: int = 0
    unrecoverable: int = 0
    restored: int = 0
    script: str | None = None
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------- tooling

class Tools:
    """Locate rsync/ssh: native binaries, or WSL's on Windows."""

    def __init__(self) -> None:
        self.mode = "native"
        if shutil.which("rsync"):
            return
        if sys.platform == "win32" and shutil.which("wsl"):
            probe = subprocess.run(["wsl", "-e", "sh", "-c", "command -v rsync && command -v ssh"],
                                   capture_output=True, text=True)
            if probe.returncode == 0:
                self.mode = "wsl"
                return
        raise RuntimeError("rsync not found. Install it (on Windows, inside WSL: "
                           "'sudo apt install rsync openssh-client').")

    def cmd(self, args: list[str]) -> list[str]:
        return ["wsl", "-e", *args] if self.mode == "wsl" else args

    def path(self, p: Path | str) -> str:
        """A local path as the tools see it (C:\\x\\y → /mnt/c/x/y under WSL)."""
        if self.mode != "wsl":
            return str(p)
        if isinstance(p, str) and p.startswith("/"):
            return p                                  # already a path inside WSL
        w = PureWindowsPath(os.path.abspath(p))
        return "/mnt/" + w.drive.rstrip(":").lower() + "/" + "/".join(w.parts[1:])


def parse_dest(dest: str) -> tuple[str | None, str]:
    """('user@host', '/path') for remote destinations, (None, path) for local ones."""
    if re.match(r"^[A-Za-z]:[\\/]", dest) or dest.startswith(("/", ".", "\\", "~")):
        return None, dest
    m = re.match(r"^([^:/\\\s]+):(.*)$", dest)
    if m:
        return m.group(1), m.group(2) or "."
    return None, dest


# ---------------------------------------------------------------------- main

def send(db: MapDB, pool: Pool, fs_id: int, patterns: list[str] | None, opts: SendOptions,
         progress: Callable[[str], None] | None = None) -> SendResult:
    res = SendResult(dry_run=opts.dry_run)
    say = progress or (lambda m: log.info("%s", m))
    tools = Tools()
    host, dpath = parse_dest(opts.dest)
    fs = db.execute("SELECT * FROM filesystems WHERE id=?", (fs_id,)).fetchone()
    if fs is None:
        raise KeyError(f"no filesystem {fs_id}")

    # ---- 1. stage
    statuses = {"full", "partial"} if opts.include_partial else {"full"}
    ex = Extractor(db, pool, Options(out_dir=opts.staging, skip_existing=True))
    say(f"staging into {opts.staging}")
    ex.extract_files(fs_id, patterns=patterns, statuses=statuses)
    root = ex.fs_root(fs)
    res.staged = sum(1 for r in ex.records if r.kind == "file")
    sel = ex.last_selection
    everything = ex.select(fs_id, patterns, None)
    res.held_back_partial = 0 if opts.include_partial else sum(
        1 for r in everything if r["type"] == "file" and r["status"] == "partial")
    res.unrecoverable = sum(1 for r in everything if r["type"] == "file" and r["status"] == "none")
    ex.write_manifest({"purpose": "staging for zesus send"})

    # ---- 2. rsync
    stubs = [ex.local_rel[r["path"]].as_posix() + ".symlink" for r in sel
             if r["type"] == "symlink" and sys.platform == "win32"]
    excludes = opts.staging / f".zesus-send-excludes-fs{fs_id}.txt"
    excludes.write_text("\n".join(["*.gaps.json", "/manifest.json", *[f"/{s}" for s in stubs]]) + "\n",
                        encoding="utf-8", newline="\n")
    rs = ["rsync", "-rlt", "--partial", "--info=progress2", "--out-format=SENT %n",
          f"--exclude-from={tools.path(excludes)}"]
    if not opts.overwrite:
        rs.append("--ignore-existing")
    if opts.dry_run:
        rs.append("--dry-run")
    if host:
        rs += ["-e", " ".join(["ssh", *map(shlex.quote, opts.ssh_args)])]
        if opts.sudo:
            rs.append("--rsync-path=sudo -n rsync")
        target = f"{host}:{dpath.rstrip('/')}/"
    else:
        if tools.mode == "native" and not opts.dry_run:
            Path(dpath).mkdir(parents=True, exist_ok=True)
        target = tools.path(dpath).rstrip("/") + "/"
    rs += [tools.path(root).rstrip("/") + "/", target]
    say(f"rsync{' (dry run)' if opts.dry_run else ''} → {opts.dest}")
    sent, err = _run_rsync(tools.cmd(rs), say)
    if err is not None:
        res.errors.append(err)
        say(err)
        return res
    res.sent = sum(1 for s in sent if not s.endswith("/"))
    say(f"rsync: {res.sent} files {'would be ' if opts.dry_run else ''}sent")

    # ---- 3. restore names and metadata
    if not opts.metadata:
        return res
    owners = opts.sudo if opts.owners is None else opts.owners
    script = build_restore_script(sel, ex.local_rel, set(sent), owners)
    sp = opts.staging / f".zesus-restore-fs{fs_id}.sh"
    sp.write_text(script, encoding="utf-8", newline="\n")
    res.script = str(sp)
    if opts.dry_run:
        say(f"dry run: restore script written to {sp} (not executed)")
        return res
    sh = "sudo -n sh -s" if opts.sudo else "sh -s"
    if host:
        cmd = tools.cmd(["ssh", *opts.ssh_args, host, f"cd {shlex.quote(dpath)} && {sh}"])
    else:
        cmd = tools.cmd(["sh", "-c", f"cd {shlex.quote(tools.path(dpath))} && {sh}"])
    say("restoring original names, symlinks, owners, modes and times on the destination")
    p = subprocess.run(cmd, input=script.encode("utf-8"), capture_output=True)
    out, errtxt = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        res.errors.append(f"restore script failed ({p.returncode}): {errtxt.strip()[-2000:]}")
    m = re.search(r"ZESUS-RESTORED (\d+)", out)
    res.restored = int(m.group(1)) if m else 0
    res.errors += [ln for ln in errtxt.splitlines()[:20] if ln.strip()]
    say(f"restored metadata on {res.restored} entries")
    return res


def _run_rsync(cmd: list[str], say: Callable[[str], None]) -> tuple[list[str], str | None]:
    """Run rsync, stream progress, collect 'SENT <name>' lines. Returns (sent, error or None)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sent: list[str] = []
    last = 0.0
    buf = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "surrogateescape").strip()
            if line.startswith("SENT "):
                sent.append(line[5:])
            elif "%" in line and time.monotonic() - last > 2:
                last = time.monotonic()
                say(f"rsync: {line}")
    errtxt = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        return sent, f"rsync exited {proc.returncode}: {errtxt.strip()[-2000:]}"
    return sent, None


def build_restore_script(entries, local_rel: dict[str, Path], sent: set[str], owners: bool) -> str:
    """POSIX sh run in the destination directory.

    *sent* holds rsync's names (local form, directories with a trailing '/'). Only entries
    this run created are touched.
    """
    q = shlex.quote
    rows = []
    for r in entries:
        if r["path"].strip("/") == "":
            continue
        orig = r["path"].strip("/")
        loc = local_rel[r["path"]].as_posix()
        created = (loc + "/" in sent) if r["type"] == "dir" else (loc in sent)
        rows.append((orig, loc, r, created))

    out = ["#!/bin/sh", "# generated by zesus from the recovery map: restores names, symlinks and metadata",
           "export TZ=UTC", "n=0"]
    # 1. names: deepest first, so each rename happens inside a parent that still has its local name
    renames = [(orig, loc, r) for orig, loc, r, c in rows if c and orig.rsplit("/", 1)[-1] != loc.rsplit("/", 1)[-1]]
    for orig, loc, _r in sorted(renames, key=lambda t: -t[1].count("/")):
        parent = loc.rsplit("/", 1)[0] if "/" in loc else ""
        src = loc
        dst = (parent + "/" if parent else "") + orig.rsplit("/", 1)[-1]
        out.append(f"[ -e {q(src)} ] && [ ! -e {q(dst)} ] && mv -- {q(src)} {q(dst)}")

    def stamp(t: int) -> str:
        return time.strftime("%Y%m%d%H%M.%S", time.gmtime(t))

    def meta(path: str, r, link: bool = False) -> list[str]:
        lines = []
        if owners and r["uid"] is not None:
            lines.append(f"chown {'-h ' if link else ''}{r['uid']}:{r['gid'] or 0} {q(path)}")
        if not link and r["mode"]:
            lines.append(f"chmod {r['mode'] & 0o7777:o} {q(path)}")
        if not link and r["mtime"]:
            lines.append(f"touch -m -t {stamp(r['mtime'])} {q(path)}")
        return lines

    # 2. symlinks (sent as real links from POSIX staging, recreated from the map otherwise)
    for orig, _loc, r, _c in rows:
        if r["type"] == "symlink" and r["link_target"] is not None:
            d = orig.rsplit("/", 1)[0] if "/" in orig else "."
            out.append(f"if [ ! -e {q(orig)} ] && [ ! -L {q(orig)} ]; then mkdir -p {q(d)} && "
                       f"ln -s {q(r['link_target'])} {q(orig)} && n=$((n+1)); fi")
            out += [f"[ -L {q(orig)} ] && {line}" for line in meta(orig, r, link=True)]
    # 3. files, then directories deepest first (so directory mtimes stick)
    for orig, _loc, r, c in rows:
        if r["type"] == "file" and c:
            out.append(f"if [ -f {q(orig)} ]; then")
            out += ["  " + line for line in meta(orig, r)]
            out.append("  n=$((n+1)); fi")
    for orig, _loc, r, _c in sorted((x for x in rows if x[2]["type"] == "dir" and x[3]), key=lambda x: -x[0].count("/")):
        out.append(f"if [ -d {q(orig)} ]; then")
        out += ["  " + line for line in meta(orig, r)]
        out.append("  n=$((n+1)); fi")
    out.append('echo "ZESUS-RESTORED $n"')
    return "\n".join(out) + "\n"
