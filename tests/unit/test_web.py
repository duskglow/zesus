"""Web API: browsing, estimates, job control and refusals, against a hand-built map."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from zesus.extract.engine import estimate_files  # noqa: E402
from zesus.map.db import MapDB  # noqa: E402
from zesus.web.app import create_app  # noqa: E402

BS = 16384


@pytest.fixture
def mapfile(tmp_path):
    path = tmp_path / "m.sqlite"
    db = MapDB(path)
    db.execute("INSERT INTO pools(id,name,guid) VALUES(1,'p','1')")
    db.execute("INSERT INTO volumes(id,name,volsize,volblocksize,n_blocks,status) VALUES(1,'p/vol',?,?,100,'verified')",
               (100 * BS, BS))
    for a, n, s in [(0, 40, "ok"), (40, 10, "cksum_mismatch"), (50, 45, "hole"), (95, 5, "ok_stale")]:
        db.execute("INSERT INTO volume_coverage VALUES(1,?,?,?,?)", (a, n, s, ""))
    db.execute("INSERT INTO filesystems(id,volume_id,start,length,fstype,plugin) VALUES(1,1,0,?,'ext4','ext4')",
               (100 * BS,))
    ents = [(1, 2, 2, "", "/", "dir", 0, "n/a", None), (2, 12, 2, "a", "/a", "dir", 0, "n/a", None),
            (3, 13, 12, "x.bin", "/a/x.bin", "file", 1000, "full", 1000),
            (4, 14, 12, "y.bin", "/a/y.bin", "file", 3000, "partial", 1000),
            (5, 15, 2, "z.txt", "/z.txt", "file", 50, "none", 0)]
    for e in ents:
        db.execute("INSERT INTO fs_entries(id,fs_id,inode,parent_inode,name,path,type,size,status,recoverable_bytes) "
                   "VALUES(?,1,?,?,?,?,?,?,?,?)", e)
    db.commit()
    db.close()
    return path


@pytest.fixture
def client(mapfile):
    return TestClient(create_app(str(mapfile), None))


def test_overview_and_listing(client):
    ov = client.get("/api/overview").json()
    assert ov["evidence"] == [] and ov["members"] is None
    assert [v["name"] for v in ov["volumes"]] == ["p/vol"]
    ls = client.get("/api/fs/1/ls", params={"path": "/a"}).json()
    assert sorted(c["name"] for c in ls["children"]) == ["x.bin", "y.bin"]


def test_file_estimate_matches_the_extractor_selection(client, mapfile):
    req = {"kind": "files", "fs_id": 1, "paths": ["/a", "/z.txt"], "statuses": ["full", "partial", "none"]}
    got = client.post("/api/estimate", json=req).json()
    want = estimate_files(MapDB(mapfile, readonly=True), 1, ["/a", "/z.txt"], {"full", "partial", "none"})
    for k in ("files", "bytes", "recoverable_bytes", "lost_bytes", "entries"):
        assert got[k] == want[k]
    assert (got["files"], got["bytes"], got["recoverable_bytes"]) == (3, 4050, 2000)
    assert got["eta_s"] is None                         # nothing measured yet: say unknown


def test_volume_estimate_uses_coverage(client, tmp_path):
    got = client.post("/api/estimate", json={"kind": "volume", "volume_id": 1, "out": str(tmp_path / "o")}).json()
    assert got["bytes"] == 100 * BS
    assert got["read_bytes"] == 45 * BS                 # ok + ok_stale
    assert got["sparse_bytes"] == 45 * BS               # holes
    assert got["lost_bytes"] == 10 * BS                 # checksum mismatch
    assert got["free_bytes"] and got["out_problem"] is None


def test_jobs_refuse_without_evidence_and_validate_input(client, tmp_path):
    r = client.post("/api/extract", json={"kind": "volume", "volume_id": 1, "out": str(tmp_path / "o")})
    assert r.status_code == 400 and "evidence" in r.json()["detail"]
    r = client.post("/api/extract", json={"kind": "volume", "volume_id": 1, "out": "relative/dir"})
    assert r.status_code == 400
    r = client.post("/api/scan", json={"phases": ["nope"]})
    assert r.status_code == 400
    assert client.post("/api/jobs/doesnotexist/cancel").status_code == 404


def test_scan_job_runs_reports_progress_and_can_be_cancelled(tmp_path, mapfile):
    img = tmp_path / "blank.img"
    img.write_bytes(b"\0" * (16 << 20))                 # no pool in it: phases finish quickly
    c = TestClient(create_app(str(mapfile), [str(img)]))
    jid = c.post("/api/scan", json={"phases": ["history", "datasets"]}).json()["id"]
    assert c.post("/api/scan", json={"phases": ["history"]}).status_code in (200, 409)
    for _ in range(100):
        j = next(x for x in c.get("/api/jobs").json()["jobs"] if x["id"] == jid)
        if j["state"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    assert "progress" in j and j["progress"]["state"] == j["state"]
    assert c.post(f"/api/jobs/{jid}/cancel").status_code == 200    # cancelling a finished job is harmless
