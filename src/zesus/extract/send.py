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

With ``batch_bytes``, a selection larger than the local disk is sent in batches: stage
about that much, rsync it, delete the staged copies, and repeat. Names and metadata are
restored once at the end. A ledger in the staging directory records what has been sent,
so an interrupted run resumes without re-extracting it.
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

from ..io import guard
from ..map.db import MapDB
from ..zfs.pool import Pool
from .engine import Extractor, LocalNamer, Options, select_entries

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
    ssh_command: str = "ssh"                    # e.g. Windows' ssh.exe, to use its keys from WSL
    batch_bytes: int = 0                        # >0: stage, send and delete this much at a time
    verify: bool = False                        # afterwards, hash the copies on the destination
    verify_only: bool = False                   # only verify an earlier send


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
    verified: int | None = None                 # identical on the destination (None: not checked)
    verify_missing: list[str] = field(default_factory=list)
    verify_different: list[str] = field(default_factory=list)
    verify_unhashed: int = 0                    # sent, but no hash was recorded for them
    verify_report: str | None = None


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
         progress: Callable[[str], None] | None = None, stop=None, meter=None) -> SendResult:
    """*stop*: an Event (or Stop) checked between files and batches. *meter*: a
    zesus.progress.Progress for staging progress."""
    say = progress or (lambda m: log.info("%s", m))
    if opts.verify_only:
        res = SendResult()
    else:
        res = _send(db, pool, fs_id, patterns, opts, say, stop, meter)
    if (opts.verify or opts.verify_only) and not opts.dry_run and not res.errors:
        verify_destination(db, fs_id, patterns, opts, res, say)
    return res


def _send(db: MapDB, pool: Pool, fs_id: int, patterns: list[str] | None, opts: SendOptions,
          say: Callable[[str], None], stop=None, meter=None) -> SendResult:
    res = SendResult(dry_run=opts.dry_run)
    tools = Tools()
    host, dpath = parse_dest(opts.dest)
    fs = db.execute("SELECT * FROM filesystems WHERE id=?", (fs_id,)).fetchone()
    if fs is None:
        raise KeyError(f"no filesystem {fs_id}")

    statuses = {"full", "partial"} if opts.include_partial else {"full"}
    everything = select_entries(db, fs_id, patterns, None)
    sel = [r for r in everything if r["type"] not in ("file", "symlink") or r["status"] in statuses]
    res.held_back_partial = 0 if opts.include_partial else sum(
        1 for r in everything if r["type"] == "file" and r["status"] == "partial")
    res.unrecoverable = sum(1 for r in everything if r["type"] == "file" and r["status"] == "none")
    namer = LocalNamer()
    local_rel = {r["path"]: namer.local(r["path"]) for r in sel}       # path order: stable names
    opts.staging.mkdir(parents=True, exist_ok=True)
    ledger = opts.staging / f".zesus-sent-fs{fs_id}.txt"
    sent_all: set[str] = set()      # created on the destination by zesus (metadata is restored on these)
    done: set[str] = set()          # sent, or already present at the destination: skip on resume
    if ledger.exists() and not opts.dry_run:
        for line in ledger.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            kind, _, name = line.partition(" ")
            done.add(name)
            if kind == "SENT":
                sent_all.add(name)
        if done:
            say(f"resuming: {len(done)} entries already done (ledger {ledger})")
    batches = _batches(db, sel, local_rel, done, opts.batch_bytes)
    ex = None
    ev = getattr(stop, "event", stop)
    for bi, batch in enumerate(batches, 1):
        if ev is not None and ev.is_set():
            res.errors.append(f"stopped before batch {bi} of {len(batches)}; run the same command to resume")
            say(res.errors[-1])
            return res
        # ---- 1. stage
        ex = Extractor(db, pool, Options(out_dir=opts.staging, skip_existing=True), progress=meter, stop=stop)
        size = sum(r["size"] or 0 for r in batch if r["type"] == "file")
        say(f"batch {bi}/{len(batches)}: staging {sum(1 for r in batch if r['type'] == 'file')} files "
            f"({size / (1 << 30):.1f} GiB) into {opts.staging}")
        ex.extract_files(fs_id, entries=batch, local_rel=local_rel)
        root = ex.fs_root(fs)
        if ex.cancelled:
            # the batch is incomplete: send nothing of it, keep what is staged for the resume
            res.errors.append(f"stopped while staging batch {bi}; run the same command to resume")
            say(res.errors[-1])
            return res
        res.staged += sum(1 for r in ex.records if r.kind == "file")
        ex.write_manifest({"purpose": "staging for zesus send"})
        # ---- 2. rsync
        sent, err = _rsync(tools, opts, host, dpath, root, fs_id, batch, local_rel, say,
                           symlinks=[r["path"] for r in sel if r["type"] == "symlink"])
        if err is not None:
            # Record what did arrive, so a rerun still restores its names and metadata.
            # rsync reports a file only once it is complete, so the list is safe to trust.
            if sent and not opts.dry_run:
                with open(ledger, "a", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
                    f.writelines(f"SENT {x}\n" for x in sent)
            res.errors.append(err)
            say(err)
            return res
        sent_all |= set(sent)
        res.sent += sum(1 for x in sent if not x.endswith("/"))
        if not opts.dry_run:
            with open(ledger, "a", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
                f.writelines(f"SENT {x}\n" for x in sent)
                # files the destination already had: done, but not ours to change
                f.writelines(f"HAD {local_rel[r['path']].as_posix()}\n" for r in batch
                             if r["type"] == "file" and local_rel[r["path"]].as_posix() not in sent)
        if opts.batch_bytes and not opts.dry_run:
            freed = _unstage(root, batch, local_rel)
            say(f"batch {bi}: sent; removed {freed / (1 << 30):.1f} GiB of staged copies")
    say(f"rsync: {res.sent} files {'would be ' if opts.dry_run else ''}sent")
    if ex is None:
        say("nothing left to send")
        ex = Extractor(db, pool, Options(out_dir=opts.staging, skip_existing=True))
    sent = sent_all

    # ---- 3. restore names and metadata
    if not opts.metadata or not sent:
        return res
    owners = opts.sudo if opts.owners is None else opts.owners
    script = build_restore_script(sel, local_rel, set(sent), owners)
    sp = opts.staging / f".zesus-restore-fs{fs_id}.sh"
    sp.write_text(script, encoding="utf-8", newline="\n")
    res.script = str(sp)
    if opts.dry_run:
        say(f"dry run: restore script written to {sp} (not executed)")
        return res
    sh = "sudo -n sh -s" if opts.sudo else "sh -s"
    if host:
        remote = [*opts.ssh_args, host, f"cd {shlex.quote(dpath)} && {sh}"]
        win_ssh = _windows_exe(opts.ssh_command) if tools.mode == "wsl" else None
        # A Windows ssh.exe given to WSL's rsync is run natively here: piping stdin from
        # Python through WSL into a Windows program loses it.
        cmd = [win_ssh, *remote] if win_ssh else tools.cmd([opts.ssh_command, *remote])
    else:
        cmd = tools.cmd(["sh", "-c", f"cd {shlex.quote(tools.path(dpath))} && {sh}"])
    say("restoring original names, symlinks, owners, modes and times on the destination")
    p = subprocess.run(cmd, input=script.encode("utf-8"), capture_output=True)
    out, errtxt = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        res.errors.append(f"restore script failed ({p.returncode}): {errtxt.strip()[-2000:]}")
    m = re.search(r"ZESUS-RESTORED (\d+)", out)
    res.restored = int(m.group(1)) if m else 0
    if p.returncode != 0:                      # stderr alone may just be a login banner
        res.errors += [ln for ln in errtxt.splitlines()[:20] if ln.strip()]
    say(f"restored metadata on {res.restored} entries")
    return res


def _windows_exe(cmd: str) -> str | None:
    """'/mnt/c/Windows/System32/OpenSSH/ssh.exe' -> 'C:/Windows/System32/OpenSSH/ssh.exe'."""
    m = re.match(r"^/mnt/([a-zA-Z])/(.+\.exe)$", cmd)
    return f"{m.group(1).upper()}:/{m.group(2)}" if m and sys.platform == "win32" else None


def _batches(db: MapDB, sel: list, local_rel: dict, done: set[str], batch_bytes: int) -> list[list]:
    """Split a selection into batches of about *batch_bytes* of file data, in on-disk order
    (to keep reads sequential). Directories and symlinks go with the first batch. Entries in
    *done* (already sent) are left out."""
    todo = [r for r in sel if not (r["type"] == "file" and local_rel[r["path"]].as_posix() in done)]
    other = [r for r in todo if r["type"] != "file"]
    files = [r for r in todo if r["type"] == "file"]
    if not batch_bytes:
        return [todo] if files or other else []
    first = {}
    for r in files:
        first[r["id"]] = db.execute("SELECT min(volume_offset) FROM fs_extents WHERE entry_id=?",
                                    (r["id"],)).fetchone()[0] or 0
    files.sort(key=lambda r: first[r["id"]])
    out: list[list] = [list(other)]
    acc = 0
    for r in files:
        if acc >= batch_bytes and out[-1]:
            out.append([])
            acc = 0
        out[-1].append(r)
        acc += r["size"] or 0
    return [b for b in out if b]


def _rsync(tools, opts, host, dpath, root: Path, fs_id: int, batch: list, local_rel: dict,
           say, symlinks: list[str] | None = None) -> tuple[list[str], str | None]:
    # Windows symlink stubs stay local. Exclude the stubs of the *whole* selection: stubs
    # staged in an earlier batch are still in the staging tree during later batches.
    links = symlinks if symlinks is not None else [r["path"] for r in batch if r["type"] == "symlink"]
    stubs = [local_rel[p].as_posix() + ".symlink" for p in links] if sys.platform == "win32" else []
    excludes = opts.staging / f".zesus-send-excludes-fs{fs_id}.txt"
    excludes.write_text("\n".join(["*.gaps.json", "*.zesus-writing", "/manifest.json", *[f"/{s}" for s in stubs]]) + "\n",
                        encoding="utf-8", newline="\n")
    # --partial-dir: an interrupted transfer is kept aside, not under the real name, where
    # --ignore-existing would later mistake it for a complete file and skip it.
    rs = ["rsync", "-rlt", "--partial-dir=.zesus-rsync-partial", "--info=progress2",
          "--out-format=SENT %n", f"--exclude-from={tools.path(excludes)}"]
    if not opts.overwrite:
        rs.append("--ignore-existing")
    if opts.dry_run:
        rs.append("--dry-run")
    if host:
        rs += ["-e", " ".join([shlex.quote(opts.ssh_command), *map(shlex.quote, opts.ssh_args)])]
        if opts.sudo:
            rs.append("--rsync-path=sudo -n rsync")
        target = f"{host}:{dpath.rstrip('/')}/"
    else:
        if tools.mode == "native" and not opts.dry_run:
            Path(dpath).mkdir(parents=True, exist_ok=True)
        target = tools.path(dpath).rstrip("/") + "/"
    root.mkdir(parents=True, exist_ok=True)
    rs += [tools.path(root).rstrip("/") + "/", target]
    say(f"rsync{' (dry run)' if opts.dry_run else ''} → {opts.dest}")
    return _run_rsync(tools.cmd(rs), say)


def _unstage(root: Path, batch: list, local_rel: dict) -> int:
    """Delete the staged copies of a batch's files (after rsync succeeded). Only files this
    tool staged under *root* are removed; directories are kept for later batches."""
    freed = 0
    for r in batch:
        if r["type"] not in ("file", "symlink"):
            continue
        p = root / local_rel[r["path"]]
        cands = (p, Path(str(p) + ".gaps.json")) if r["type"] == "file" else (Path(str(p) + ".symlink"),)
        for q in cands:
            try:
                freed += q.stat().st_size
                q.unlink()
            except FileNotFoundError:
                pass
    return freed


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
        if any(c in ("", ".", "..") for c in orig.split("/")):
            # a damaged or crafted name must never lead the script outside the destination;
            # such an entry keeps its escaped local name
            continue
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


# ---------------------------------------------------------------------- verification

def verify_destination(db: MapDB, fs_id: int, patterns: list[str] | None, opts: SendOptions,
                       res: SendResult, say: Callable[[str], None]) -> None:
    """Hash every sent file *on the destination* and compare it with the SHA-256 recorded
    when zesus extracted and verified it (the staging manifest). Only hashes cross the
    network. Fills res.verified / verify_missing / verify_different, and writes a report
    next to the staging area."""
    import hashlib
    import json
    statuses = {"full", "partial"} if opts.include_partial else {"full"}
    files = [r["path"] for r in select_entries(db, fs_id, patterns, None)
             if r["type"] == "file" and r["status"] in statuses]
    man = opts.staging / "manifest.json"
    recorded: dict[str, str] = {}
    if man.exists():
        for o in json.loads(man.read_text(encoding="utf-8")).get("outputs", []):
            if o.get("kind") == "file" and o.get("sha256"):
                recorded[o["source"]] = o["sha256"]
    want = {p: recorded[p] for p in files if p in recorded}
    res.verify_unhashed = len(files) - len(want)
    say(f"verify: hashing {len(want)} files on the destination")
    rels = [p.lstrip("/") for p in want]
    host, dpath = parse_dest(opts.dest)
    got: dict[str, str] = {}
    if host is None:
        root = Path(dpath)
        for rel in rels:
            q = root / rel
            if q.is_file():
                h = hashlib.sha256()
                with open(q, "rb") as f:
                    for b in iter(lambda: f.read(8 << 20), b""):
                        h.update(b)
                got[rel] = h.hexdigest()
    else:
        got = _remote_hashes(opts, host, dpath, rels)
    res.verify_missing = sorted("/" + r for r in rels if r not in got)
    res.verify_different = sorted("/" + r for r in rels if r in got and got[r] != want["/" + r])
    res.verified = len(rels) - len(res.verify_missing) - len(res.verify_different)
    lines = [f"# Verification of {opts.dest}", "",
             f"* files compared: {len(rels)}",
             f"* identical (SHA-256 matches the verified extraction): **{res.verified}**",
             f"* missing at the destination: **{len(res.verify_missing)}**",
             f"* different at the destination: **{len(res.verify_different)}**",
             f"* sent without a recorded hash (not checked): {res.verify_unhashed}", ""]
    for title, xs in (("Missing", res.verify_missing), ("Different", res.verify_different)):
        if xs:
            lines += [f"## {title}", ""] + [f"* `{x}`" for x in xs[:1000]] + [""]
    rp = opts.staging / f"verify-fs{fs_id}.md"
    guard.assert_not_protected([rp])
    rp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    res.verify_report = str(rp)
    if res.verify_missing or res.verify_different:
        res.errors.append(f"verification: {len(res.verify_missing)} missing, {len(res.verify_different)} "
                          f"different (see {rp})")
    say(f"verify: {res.verified} identical, {len(res.verify_missing)} missing, "
        f"{len(res.verify_different)} different")


def _remote_hashes(opts: SendOptions, host: str, dpath: str, rels: list[str]) -> dict[str, str]:
    """sha256sum on the destination, fed a NUL-separated list; nothing else is transferred."""
    tools = Tools()
    lst = opts.staging / ".zesus-verify-list.bin"
    out = opts.staging / ".zesus-verify-hashes.bin"
    guard.assert_not_protected([lst, out])
    lst.write_bytes(b"\0".join(r.encode("utf-8", "surrogateescape") for r in rels) + b"\0")
    remote = f"cd {shlex.quote(dpath)} && xargs -0 sha256sum -z --"
    win_ssh = _windows_exe(opts.ssh_command) if tools.mode == "wsl" else None
    if win_ssh or tools.mode == "native":
        cmd = [win_ssh or opts.ssh_command, *opts.ssh_args, host, remote]
        with open(lst, "rb") as fi, open(out, "wb") as fo:
            subprocess.run(cmd, stdin=fi, stdout=fo, stderr=subprocess.DEVNULL)
    else:
        ssh = " ".join([shlex.quote(opts.ssh_command), *map(shlex.quote, opts.ssh_args), shlex.quote(host),
                        shlex.quote(remote)])
        subprocess.run(tools.cmd(["sh", "-c", f"{ssh} < {shlex.quote(tools.path(lst))} "
                                              f"> {shlex.quote(tools.path(out))}"]),
                       stderr=subprocess.DEVNULL)
    got: dict[str, str] = {}
    for rec in out.read_bytes().split(b"\0"):
        h, _, name = rec.partition(b"  ")
        if name:
            got[name.decode("utf-8", "surrogateescape")] = h.decode()
    return got
