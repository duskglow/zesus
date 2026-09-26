"""Staging names and the restore script used by `zesus send`."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from zesus.extract.engine import LocalNamer
from zesus.extract.send import build_restore_script, parse_dest


def test_parse_dest():
    assert parse_dest("root@pve:/srv/restore") == ("root@pve", "/srv/restore")
    assert parse_dest("backup-host:restore") == ("backup-host", "restore")
    assert parse_dest("/srv/restore") == (None, "/srv/restore")
    assert parse_dest(r"C:\restore") == (None, r"C:\restore")
    assert parse_dest("./out") == (None, "./out")


def test_namer_case_collisions_are_disambiguated():
    n = LocalNamer(case_insensitive=True)
    a, b, c = n.local("/src/Makefile"), n.local("/src/makefile"), n.local("/src/MAKEFILE")
    assert len({str(a).lower(), str(b).lower(), str(c).lower()}) == 3
    assert n.local("/src/Makefile") == a                       # stable
    assert n.local("/SRC/x") != n.local("/src/x")              # directories too


def test_namer_case_sensitive_keeps_names():
    n = LocalNamer(case_insensitive=False)
    assert n.local("/a/Makefile").name == "Makefile" and n.local("/a/makefile").name == "makefile"


def row(path, type_, mode, mtime=1_700_000_000, target=None):
    return {"path": path, "type": type_, "mode": mode, "uid": 0, "gid": 0, "mtime": mtime, "link_target": target}


ENTRIES = [
    row("/", "dir", 0o40755),
    row("/odd?dir", "dir", 0o40700),
    row("/odd?dir/a: b.txt", "file", 0o100600, 1_600_000_000),
    row("/odd?dir/Makefile", "file", 0o100644),
    row("/odd?dir/makefile", "file", 0o100640),
    row("/odd?dir/link", "symlink", 0o120777, target="a: b.txt"),
]


def stage(tmp: Path):
    """Simulate Windows-style staging (escaped, case-disambiguated names) into tmp/stage."""
    import zesus.extract.engine as eng
    namer = LocalNamer(case_insensitive=True)
    local = {}
    real_platform = sys.platform
    try:
        eng.sys.platform = "win32"                            # force Windows escaping rules
        for r in ENTRIES:
            local[r["path"]] = namer.local(r["path"])
    finally:
        eng.sys.platform = real_platform
    for r in ENTRIES:
        p = tmp / "stage" / local[r["path"]]
        if r["type"] == "dir":
            p.mkdir(parents=True, exist_ok=True)
        elif r["type"] == "file":
            p.write_text(r["path"])
    return local


def test_script_renames_escaped_names(tmp_path):
    local = stage(tmp_path)
    assert local["/odd?dir"].name == "odd%3Fdir"
    sent = {p.as_posix() + ("/" if r["type"] == "dir" else "") for r in ENTRIES
            for p in [local[r["path"]]] if r["path"] != "/" and r["type"] != "symlink"}
    script = build_restore_script(ENTRIES, local, sent, owners=False)
    assert "mv -- 'odd%3Fdir/a%3A b.txt' 'odd%3Fdir/a: b.txt'" in script
    assert "mv -- odd%3Fdir 'odd?dir'" in script
    # the directory rename comes after the renames inside it
    assert script.index("'odd%3Fdir/a: b.txt'") < script.index("mv -- odd%3Fdir 'odd?dir'")
    assert "chown" not in script


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                    reason="needs a POSIX filesystem that allows ':' and '?' in names")
def test_script_runs_and_restores(tmp_path):
    local = stage(tmp_path)
    dest = tmp_path / "dest"
    shutil.copytree(tmp_path / "stage", dest)                  # what rsync would do
    sent = {p.as_posix() + ("/" if r["type"] == "dir" else "") for r in ENTRIES
            for p in [local[r["path"]]] if r["path"] != "/" and r["type"] != "symlink"}
    script = build_restore_script(ENTRIES, local, sent, owners=False)
    out = subprocess.run(["sh", "-s"], input=script.encode(), cwd=dest, capture_output=True, check=True)
    assert b"ZESUS-RESTORED" in out.stdout
    d = dest / "odd?dir"
    assert (d / "a: b.txt").read_text() == "/odd?dir/a: b.txt"
    assert (d / "Makefile").read_text() == "/odd?dir/Makefile"
    assert (d / "makefile").read_text() == "/odd?dir/makefile"
    assert stat.S_IMODE((d / "a: b.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert int((d / "a: b.txt").stat().st_mtime) == 1_600_000_000
    assert os.readlink(d / "link") == "a: b.txt"
