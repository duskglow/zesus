"""Web API: browsing, estimates, job control and refusals, against a hand-built map."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from zesus.extract.engine import estimate_files  # noqa: E402
from zesus.map.db import MapDB  # noqa: E402
from zesus.web.app import allowed_hosts, create_app  # noqa: E402


def TC(app, **kw):
    """A client as the page itself: a loopback Host, and the header it sends with changes."""
    return TestClient(app, base_url="http://127.0.0.1:8765", headers={"x-zesus-request": "1"}, **kw)

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
    return TC(create_app(str(mapfile), None))


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


def wait_job(c, jid, timeout=60):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        j = next(x for x in c.get("/api/jobs").json()["jobs"] if x["id"] == jid)
        if j["state"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {jid} still running: {j}")


def test_scan_job_runs_as_its_own_process_and_survives_a_server_restart(tmp_path, mapfile):
    img = tmp_path / "blank.img"
    img.write_bytes(b"\0" * (16 << 20))                # no pool in it: phases finish quickly
    c = TC(create_app(str(mapfile), [str(img)]))
    jid = c.post("/api/scan", json={"phases": ["history", "datasets"]}).json()["id"]
    j = wait_job(c, jid)
    assert j["state"] == "done", j
    assert j["argv"][0] == "scan" and j["pid"]
    # a new server (a restart, or an upgrade) still knows the job and its result
    c2 = TC(create_app(str(mapfile), [str(img)]))
    j2 = next(x for x in c2.get("/api/jobs").json()["jobs"] if x["id"] == jid)
    assert j2["state"] == "done" and j2["message"] == j["message"]
    assert c2.post(f"/api/jobs/{jid}/cancel").status_code == 200   # cancelling a finished job is harmless
    assert c2.post("/api/jobs/nope/cancel").status_code == 404


def test_responses_carry_the_build_and_the_page_is_fixed_at_start(mapfile):
    c = TC(create_app(str(mapfile), None))
    r = c.get("/api/overview")
    build = r.headers["X-Zesus-Build"]
    page = c.get("/").text
    assert f'const BUILD = "{build}"' in page and "__ZESUS_BUILD__" not in page
    assert c.get("/api/build").json()["build"] == build
    other = TC(create_app(str(mapfile), None)).get("/api/build").json()["build"]
    assert other != build                                  # a restarted server is a different build


def test_event_stream_pushes_the_job_list(mapfile):
    import json
    c = TC(create_app(str(mapfile), None))
    with c.stream("GET", "/api/stream?once=true") as r:
        events = []
        for line in r.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if line.startswith("data:") and events[-1] == "jobs":
                data = json.loads(line[5:])
                break
    assert events[:2] == ["hello", "jobs"] and data["jobs"] == [] and data["build"]


def test_refuses_foreign_hosts_origins_and_bare_posts(mapfile):
    """DNS rebinding and cross-site requests must not reach the API: it can read the
    evidence and send recovered files anywhere."""
    app = create_app(str(mapfile), None, allowed_hosts("127.0.0.1", 8765))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/overview").status_code == 200
    assert c.get("/api/overview", headers={"host": "evil.example:8765"}).status_code == 403
    assert c.get("/api/overview", headers={"host": "127.0.0.1:9999"}).status_code == 403
    body = {"kind": "files", "fs_id": 1, "paths": ["/a"]}
    assert c.post("/api/estimate", json=body).status_code == 403                  # no page header
    ok = {"x-zesus-request": "1"}
    assert c.post("/api/estimate", json=body, headers=ok).status_code == 200
    assert c.post("/api/estimate", json=body, headers={**ok, "origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/estimate", json=body, headers={**ok, "origin": "http://127.0.0.1:8765"}).status_code == 200
    assert c.post("/api/send", json={"fs_id": 1, "dest": "x@evil:/"},
                  headers={"host": "evil.example", **ok}).status_code == 403


def test_job_ids_cannot_name_other_files(tmp_path):
    from zesus.jobs import JobStore
    js = JobStore(tmp_path / "m.sqlite")
    js.dir.mkdir()
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    for jid in (r"..\x","../x", "ABCDEF12", "abc"):
        assert js.cancel(jid) is False and js.get(jid) is None and js.tail(jid, 5) == []
    assert not (tmp_path / "x.cancel").exists()
