"""The losses report on a hand-built map: exact zero-filled ranges, and the carve verdict."""

from __future__ import annotations

from zesus.losses import build_report, carve_assessment, file_losses, markdown
from zesus.map.db import MapDB

BS = 16384


def make_map(tmp_path, seen=990, destroy=1000, root=900):
    db = MapDB(tmp_path / "m.sqlite")
    db.execute("INSERT INTO pools(id,name,guid) VALUES(1,'p','1')")
    db.execute("INSERT INTO datasets(id,pool_id,name,kind,origin,status,destroy_txg,seen_txg) "
               "VALUES(1,1,'p/vol','volume','history_log','destroyed',?,?)", (destroy, seen))
    db.execute("INSERT INTO volumes(id,dataset_id,name,volsize,volblocksize,n_blocks,n_verified,n_holes,n_stale,"
               "n_missing,n_damaged,status) VALUES(1,1,'p/vol',?,?,100,90,8,0,0,2,'verified')", (100 * BS, BS))
    db.execute("INSERT INTO volume_roots(volume_id,txg,top_bp,provenance,usable) VALUES(1,?,x'00','ring-mos:990',1)",
               (root,))
    for a, n, s in [(0, 40, "ok"), (40, 2, "cksum_mismatch"), (42, 50, "ok"), (92, 8, "hole")]:
        db.execute("INSERT INTO volume_coverage VALUES(1,?,?,?,?)", (a, n, s, ""))
    db.execute("INSERT INTO filesystems(id,volume_id,start,length,fstype,plugin) VALUES(1,1,0,?,'ext4','ext4')",
               (100 * BS,))
    # a file spanning blocks 38..44 (lost 40..41), and an intact one
    db.execute("INSERT INTO fs_entries(id,fs_id,path,type,size,status,recoverable_bytes) "
               "VALUES(10,1,'/big.bin','file',?,'partial',?)", (7 * BS, 5 * BS))
    db.execute("INSERT INTO fs_extents(entry_id,file_offset,length,volume_offset,kind,status) VALUES(10,0,?,?,'data','partial')",
               (7 * BS, 38 * BS))
    db.execute("INSERT INTO fs_entries(id,fs_id,path,type,size,status,recoverable_bytes) "
               "VALUES(11,1,'/ok.txt','file',100,'full',100)")
    db.commit()
    return db


def test_file_ranges_are_exact(tmp_path):
    db = make_map(tmp_path)
    [f] = file_losses(db)
    assert f.path == "/big.bin" and f.lost_bytes == 2 * BS
    assert f.ranges == [(2 * BS, 2 * BS)]          # file offset of volume blocks 40..41
    assert f.lost_blocks == [40, 41]


def test_carve_verdicts(tmp_path):
    db = make_map(tmp_path)
    assert carve_assessment(db, 1).worth_it == "no"                 # ring reaches the final state
    db.execute("UPDATE datasets SET seen_txg=500")                   # last seen long before the destroy
    assert carve_assessment(db, 1).worth_it == "maybe"
    db.execute("DELETE FROM volume_roots")                           # only carving could reach it
    assert carve_assessment(db, 1).worth_it == "yes"
    db.execute("INSERT INTO progress(phase,unit,state) VALUES('carve','*','done')")
    assert carve_assessment(db, 1).worth_it == "done"


def test_markdown_report(tmp_path):
    text = markdown(build_report(make_map(tmp_path)))
    assert "1 file(s) affected" in text and "`/big.bin`" in text and "0x8000+0x8000" in text
    assert "Could carving recover more?" in text
