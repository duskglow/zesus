"""Maps created by the first development schema must upgrade cleanly."""

from __future__ import annotations

import sqlite3

from zesus.map.db import ADDED_COLUMNS, MapDB

OLD_FS = """CREATE TABLE filesystems (
    id INTEGER PRIMARY KEY, volume_id INTEGER NOT NULL REFERENCES volumes(id),
    partition_id INTEGER REFERENCES partitions(id), start INTEGER NOT NULL, length INTEGER NOT NULL,
    fstype TEXT NOT NULL, plugin TEXT, label TEXT, uuid TEXT, block_size INTEGER, state TEXT, info_json TEXT)"""


def test_filesystems_migration(tmp_path):
    path = tmp_path / "old.sqlite"
    MapDB(path).close()
    con = sqlite3.connect(path)
    con.executescript("PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON; DROP TABLE filesystems;" + OLD_FS + ";")
    con.execute("INSERT INTO pools(id,name,guid) VALUES(1,'p','1')")
    con.execute("INSERT INTO volumes(id,name) VALUES(1,'v')")
    con.execute("INSERT INTO filesystems(id,volume_id,start,length,fstype) VALUES(7,1,0,10,'ext4')")
    con.execute("INSERT INTO fs_entries(id,fs_id,path) VALUES(1,7,'/')")
    con.commit()
    con.close()
    db = MapDB(path)
    cols = {r[1]: r for r in db.execute("PRAGMA table_info(filesystems)")}
    assert "dataset_id" in cols and cols["volume_id"][3] == 0
    assert db.execute("SELECT fstype FROM filesystems WHERE id=7").fetchone()[0] == "ext4"
    db.execute("DELETE FROM fs_entries WHERE fs_id=7")          # must not hit a dangling FK
    assert not db.execute("SELECT name FROM sqlite_master WHERE instr(sql, 'filesystems_old') OR instr(sql, 'filesystems_new')").fetchall()
    db.close()
    MapDB(path).close()                                          # idempotent


def test_multidisk_columns_added_to_v1_map(tmp_path):
    """A map made before multi-disk support gains the new nullable columns, keeps its rows,
    and stays at schema version 1 (older tools can still open it)."""
    path = tmp_path / "v1.sqlite"
    MapDB(path).close()
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA foreign_keys=OFF;
        ALTER TABLE sources DROP COLUMN pool_guid; ALTER TABLE sources DROP COLUMN member_guid;
        ALTER TABLE sources DROP COLUMN vdev_top; ALTER TABLE sources DROP COLUMN child_id;
        ALTER TABLE sources DROP COLUMN role;
        ALTER TABLE vdevs DROP COLUMN child_id; ALTER TABLE vdevs DROP COLUMN source_id;
        ALTER TABLE vdevs DROP COLUMN state; ALTER TABLE carved DROP COLUMN member;
    """)
    con.execute("INSERT INTO sources(path,size,mtime_ns,is_device,opened_at) VALUES('img',10,1,0,'t')")
    con.commit()
    con.close()
    db = MapDB(path)
    for table, col, _decl in ADDED_COLUMNS:
        assert db.has_column(table, col), (table, col)
    assert db.execute("SELECT path, child_id FROM sources").fetchone()[:] == ("img", None)
    assert db.get_meta("schema_version") == "1"
    db.close()
    MapDB(path).close()                                          # idempotent
