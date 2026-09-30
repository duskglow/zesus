"""Multi-disk pools made by real OpenZFS (dev/make_zfs_fixtures.sh), end to end.

For each pool (mirror, RAIDZ1/2/3):

* With every member present, every block read while scanning must be parity-consistent.
  This checks the column layout and the Q/R coefficient order against OpenZFS itself.
  No block may need reconstruction.
* With every tolerable set of members removed, the live zvol and files come back
  byte-identical.
* With one member more than parity can cover, extraction reports gaps. Every byte
  written outside a reported gap is still correct: the original content is regenerated
  from its seed and compared byte for byte.
* The destroyed zvol is found again by carving and recovered byte-identical.

Skipped until the fixtures exist (they need a machine with ZFS; see the script).
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "zfs"
sys.path.insert(0, str(ROOT / "dev"))

POOLS = sorted(p.parent.name for p in FIX.glob("*/manifest.json"))
pytestmark = pytest.mark.skipif(not POOLS, reason="no OpenZFS fixtures (run dev/make_zfs_fixtures.sh)")


def manifest(name: str) -> dict:
    return json.loads((FIX / name / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def unpacked(tmp_path_factory):
    cache: dict[str, list[Path]] = {}

    def get(name: str) -> list[Path]:
        if name not in cache:
            d = tmp_path_factory.mktemp(name)
            out = []
            for gz in sorted((FIX / name).glob("member*.img.gz"), key=lambda p: int(p.name[6:-7])):
                dst = d / gz.name[:-3]
                with gzip.open(gz, "rb") as f, open(dst, "wb") as o:
                    shutil.copyfileobj(f, o, 1 << 20)
                out.append(dst)
            cache[name] = out
        return cache[name]
    return get


def nparity(m: dict) -> int:
    return {"mirror": m["members"] - 1, "raidz1": 1, "raidz2": 2, "raidz3": 3}[m["layout"]]


def content(m: dict, which: str) -> bytes:
    from zfx_gen import generate
    return b"".join(generate(m["seed"] + off, n, kind) for off, n, kind in m["content"][which])


def scan(paths: list[Path], mapfile: Path, phases: str) -> None:
    from zesus.cli.main import main
    assert main(["-q", "scan", *map(str, paths), "-o", str(mapfile), "--phases", phases, "--workers", "1"]) == 0


def extract_volume(mapfile: Path, paths: list[Path], name: str, out: Path) -> tuple[Path, dict]:
    from zesus.evidence import open_evidence, pool_for_map
    from zesus.extract.engine import Extractor, Options
    from zesus.map.db import MapDB
    db = MapDB(mapfile, readonly=True)
    vid = next(r[0] for r in db.execute("SELECT id, name FROM volumes") if r[1].endswith("/" + name))
    v = db.execute("SELECT * FROM volumes WHERE id=?", (vid,)).fetchone()
    ss = open_evidence(db, [str(p) for p in paths])
    try:
        ex = Extractor(db, pool_for_map(db, ss), Options(out_dir=out, hash_outputs=False, force=True))
        rec = ex.extract_volume_range(vid, 0, v["volsize"], f"{name}.img", "volume", name)
    finally:
        ss.close()
        db.close()
    return Path(rec.path), {"status": rec.status, "gaps": [(g.offset, g.length) for g in rec.gaps]}


@pytest.mark.parametrize("name", POOLS)
def test_all_members_present_every_live_block_is_parity_consistent(name, unpacked, tmp_path):
    """Live blocks are never freed, so their on-disk parity (or mirror copies) must match
    the data exactly: this checks our column layout and Q/R order against OpenZFS. (Blocks
    of *older* tree versions may legitimately fail this: once freed, their parity sector
    can be reused by a later allocation.)"""
    from zesus.carve.verify import chosen_blocks, load_spans
    from zesus.extract.engine import Extractor, Options
    from zesus.io.sourceset import SourceSet
    from zesus.map.db import MapDB
    from zesus.scan import pipeline
    from zesus.zfs.members import assess
    from zesus.zfs.objset import Objset
    from zesus.zfs.pool import open_pools
    m = manifest(name)
    with SourceSet.open([str(p) for p in unpacked(name)]) as ss:
        pool = open_pools(ss)[0]
        assert str(pool.guid) == m["pool_guid"]
        rep = assess(pool)
        assert rep.readable and all(r.state == "present" for t in rep.tops for r in t.rows)
        top = pool.vdevs.top[0]
        db = MapDB(tmp_path / "m.sqlite")
        pipeline.run(pipeline.ScanContext(db=db, source=ss, pools=[pool], options={}),
                     ["history", "datasets", "reconstruct", "contents"])

        def consistent(offset: int, psize: int) -> bool:
            if top.type == "raidz":
                return top.parity_consistent(offset, psize) is True
            return len({lf.read(offset, psize) for lf in top.children}) == 1

        seen, bad = [0], []

        def check(bp, rd):
            if rd.status.value != "ok" or bp.embedded or bp.is_hole or rd.dva is None or rd.dva.gang:
                return
            assert not rd.repaired, "nothing should need rebuilding with every member present"
            seen[0] += 1
            if not consistent(rd.dva.offset, bp.psize):
                bad.append((bp.birth, rd.dva.offset, bp.psize))
        pool.reader._cache.clear()
        pool.reader.observers.append(check)
        # the newest MOS
        mos = Objset(pool.reader, pool.best_uberblock().rootbp, "MOS")
        for obj in range(1, mos.max_object):
            try:
                mos.dnode(obj)
            except Exception:
                pass
        # every block of every live file
        fid = next(r[0] for r in db.execute("SELECT f.id, d.name FROM filesystems f JOIN datasets d "
                                            "ON d.id=f.dataset_id") if r[1].endswith("/fs"))
        Extractor(db, pool, Options(out_dir=tmp_path / "files", hash_outputs=False)).extract_files(fid)
        # every data block of the live zvol
        vid = next(r[0] for r in db.execute("SELECT id, name FROM volumes") if r[1].endswith("/vol"))
        wl = chosen_blocks(pool, load_spans(db, vid), {0, 1})
        for off, ps in zip(wl["offset"].tolist(), wl["psize"].tolist(), strict=True):
            seen[0] += 1
            if not consistent(int(off), int(ps)):
                bad.append(("zvol", off, ps))
        db.close()
    assert seen[0] > 150, seen
    assert not bad, f"{len(bad)} of {seen[0]} live blocks have parity/copies that do not match: {bad[:5]}"


@pytest.mark.parametrize("name", POOLS)
def test_live_zvol_with_every_tolerable_loss(name, unpacked, tmp_path):
    m = manifest(name)
    paths = unpacked(name)
    mapfile = tmp_path / "m.sqlite"
    scan(paths, mapfile, "history,datasets,reconstruct,verify")
    want = content(m, "vol")
    assert hashlib.sha256(want).hexdigest() == m["zvols"]["vol"]["sha256"]
    for k in range(nparity(m) + 1):
        for missing in itertools.combinations(range(len(paths)), k):
            use = [p for i, p in enumerate(paths) if i not in missing]
            img, info = extract_volume(mapfile, use, "vol", tmp_path / f"out{k}")
            assert info["status"] == "full", (missing, info)
            assert img.read_bytes()[:len(want)] == want, missing


@pytest.mark.parametrize("name", POOLS)
def test_one_loss_too_many_gives_gaps_never_wrong_bytes(name, unpacked, tmp_path):
    m = manifest(name)
    paths = unpacked(name)
    p = nparity(m)
    if p + 1 >= len(paths):
        pytest.skip("no member would remain")
    mapfile = tmp_path / "m.sqlite"
    scan(paths, mapfile, "history,datasets,reconstruct,verify")
    want = content(m, "vol")
    img, info = extract_volume(mapfile, paths[p + 1:], "vol", tmp_path / "out")
    got = img.read_bytes()[:len(want)]
    assert info["gaps"], "losing more members than parity must leave gaps"
    in_gap = bytearray(len(want))
    for off, n in info["gaps"]:
        in_gap[off:off + n] = b"\1" * min(n, max(0, len(want) - off))
    wrong = sum(1 for i in range(0, len(want), 512)
                if not in_gap[i] and got[i:i + 512] != want[i:i + 512])
    assert wrong == 0, f"{wrong} sectors outside reported gaps differ from the original"


@pytest.mark.parametrize("name", POOLS)
@pytest.mark.parametrize("ignore_ring", [False, True], ids=["ring+carved", "carved-only"])
def test_destroyed_zvol_is_carved_and_recovered(name, ignore_ring, unpacked, tmp_path):
    from zesus.cli.main import main
    m = manifest(name)
    paths = unpacked(name)
    mapfile = tmp_path / "m.sqlite"
    args = ["-q", "scan", *map(str, paths), "-o", str(mapfile), "--phases",
            "history,datasets,carve,reconstruct,verify", "--workers", "1"]
    assert main(args + (["--ignore-ring"] if ignore_ring else [])) == 0
    img, info = extract_volume(mapfile, paths, "gone", tmp_path / "out")
    want = content(m, "gone")
    assert info["status"] == "full", info
    assert hashlib.sha256(img.read_bytes()[:len(want)]).hexdigest() == m["zvols"]["gone"]["sha256"]


@pytest.mark.parametrize("name", [n for n in POOLS if n.startswith("mirror")] or ["(no mirror fixture)"])
def test_mirror_combined_strategy_uses_the_healthy_copy(name, unpacked, tmp_path):
    from zesus.combine import combine
    m = manifest(name)
    paths = unpacked(name)
    # member 0's "ddrescue" map says 8 MiB..40 MiB of its vdev was not rescued:
    # those regions must come from member 1
    mf = tmp_path / "m0.map"
    mf.write_text("# Mapfile\n0x0 + 1\n0x0 0x800000 +\n0x800000 0x2000000 -\n0x2800000 0x10000000 +\n",
                  encoding="utf-8")
    res = combine([str(p) for p in paths], tmp_path / "combined.img", {str(paths[0]): str(mf)})
    assert res.regions_from[str(paths[1])] >= 32 and not res.missing
    mapfile = tmp_path / "c.sqlite"
    scan([res.path], mapfile, "history,datasets,reconstruct,verify")
    img, info = extract_volume(mapfile, [res.path], "vol", tmp_path / "out")
    assert info["status"] == "full" and img.read_bytes()[:len(content(m, "vol"))] == content(m, "vol")


@pytest.mark.parametrize("name", [n for n in POOLS if n.startswith("raidz1")] or ["(no raidz1 fixture)"])
def test_losses_report_names_blocks_a_missing_disk_could_restore(name, unpacked, tmp_path):
    from zesus.evidence import open_evidence, pool_for_map
    from zesus.losses import build_report, markdown
    from zesus.map.db import MapDB
    paths = unpacked(name)
    mapfile = tmp_path / "m.sqlite"
    scan(paths, mapfile, "history,datasets,reconstruct")      # mapped with every member...
    scan(paths[2:], mapfile, "verify")                          # ...verified with two raidz1 members gone
    db = MapDB(mapfile, readonly=True)
    ss = open_evidence(db, [str(p) for p in paths[2:]])
    try:
        rep = build_report(db, pool_for_map(db, ss))
    finally:
        ss.close()
    vol = next(v for v in rep.volumes if v["name"].endswith("/vol"))
    assert vol["lost"] > 0
    carve = next(c for c in rep.carve if c.volume.endswith("/vol"))
    assert carve.worth_it == "no"                 # the ring reaches the live zvol's final state
    if "lost_birth_max" in carve.facts:
        assert carve.facts["lost_birth_max"] <= carve.facts["newest_root_txg"]
    assert rep.members_checked and "absent" in rep.member_note
    text = markdown(rep)
    assert "Could carving recover more?" in text and "no." in text


@pytest.mark.parametrize("name", [n for n in POOLS if n.startswith("raidz")] or ["(no raidz fixture)"])
def test_recheck_with_a_late_member_recovers_the_lost_blocks(name, unpacked, tmp_path):
    from zesus.cli.main import main
    from zesus.map.db import MapDB
    m = manifest(name)
    paths = unpacked(name)
    p = nparity(m)
    mapfile = tmp_path / "m.sqlite"
    scan(paths, mapfile, "history,datasets,reconstruct")
    scan(paths[p + 1:], mapfile, "verify")                     # verified with p+1 members missing
    db = MapDB(mapfile, readonly=True)
    lost = db.execute("SELECT n_missing + n_damaged FROM volumes WHERE name LIKE '%/vol'").fetchone()[0]
    db.close()
    assert lost > 0
    assert main(["-q", "recheck", str(mapfile), *map(str, paths), "--workers", "1"]) == 0
    db = MapDB(mapfile, readonly=True)
    assert db.execute("SELECT n_missing + n_damaged FROM volumes WHERE name LIKE '%/vol'").fetchone()[0] == 0
    db.close()
    img, info = extract_volume(mapfile, paths, "vol", tmp_path / "out")
    assert info["status"] == "full" and img.read_bytes()[:len(content(m, "vol"))] == content(m, "vol")
