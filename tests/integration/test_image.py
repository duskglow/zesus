"""Integration tests against a real ZFS image (ZESUS_TEST_IMAGE). They check general
invariants that hold for any healthy-enough pool, not facts about one specific image."""

from __future__ import annotations

import pytest

from zesus.io import RawSource
from zesus.zfs.dsl import Dsl
from zesus.zfs.objset import Objset
from zesus.zfs.pool import open_pools
from zesus.zfs.zap import read_zap

pytestmark = pytest.mark.image


@pytest.fixture(scope="module")
def pool(request):
    import os
    p = os.environ.get("ZESUS_TEST_IMAGE")
    if not p or not os.path.exists(p):
        pytest.skip("set ZESUS_TEST_IMAGE")
    src = RawSource(p)
    pools = open_pools(src)
    assert pools, "no pool found"
    yield pools[0]
    src.close()


def test_labels_and_uberblocks(pool):
    for im in pool.vdev_images:
        assert sum(lb.config_ok for lb in im.labels) >= 1
    assert any(u.checksum_ok for u in pool.uberblocks)


def test_newest_mos_and_dsl(pool):
    ub = pool.best_uberblock()
    for u in pool.uberblocks:
        try:
            mos = Objset(pool.reader, u.rootbp, "MOS")
            break
        except Exception:
            continue
    else:
        pytest.fail("no readable MOS")
    objdir = read_zap(mos.object(1))
    assert "root_dataset" in objdir and "config" in objdir
    names = [d.name for d in Dsl(mos, pool.name).walk()]
    assert names and names[0] == pool.name
    assert ub.txg >= u.txg


def test_history_decodes(pool):
    from zesus.zfs.history import read_history
    for u in pool.uberblocks:
        try:
            mos = Objset(pool.reader, u.rootbp, "MOS")
            objdir = read_zap(mos.object(1))
            break
        except Exception:
            continue
    if "history" not in objdir:
        pytest.skip("pool has no history object")
    h = read_history(mos.object(objdir["history"]))
    assert h.records and not h.errors
    assert any((r.command or "").startswith("zpool create") or r.internal_name == "create" for r in h.records)
