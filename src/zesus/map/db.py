"""The SQLite map database."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from ..io import guard

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class MapDB:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        guard.assert_not_protected([self.path])
        if readonly:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A just-killed scan can hold file handles for a moment (notably on Windows).
            for attempt in range(10):
                try:
                    self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if attempt == 9:
                        raise RuntimeError(f"cannot open map {self.path}: {exc}. Is another scan still "
                                           "running on it?") from exc
                    time.sleep(1)
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
        exists = cur.fetchone() is not None
        if exists:
            v = self.get_meta("schema_version")
            if v is not None and int(v) != SCHEMA_VERSION:
                raise RuntimeError(f"map {self.path} has schema v{v}; this tool expects v{SCHEMA_VERSION}")
        sql = resources.files("zesus.map").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self._migrate()
        if not exists:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_at", now())
        self.conn.commit()

    def _migrate(self) -> None:
        """In-place upgrades for maps created by earlier development versions."""
        cols = {r[1]: r for r in self.conn.execute("PRAGMA table_info(filesystems)")}
        if "dataset_id" not in cols or cols["volume_id"][3]:          # [3] = notnull
            log.info("migrating map: filesystems table gains dataset_id, volume_id becomes nullable")
            # Standard SQLite table rebuild: build the new table, copy, drop, rename. With
            # legacy_alter_table the final rename does not rewrite other tables' foreign keys.
            self.conn.executescript("""
                PRAGMA foreign_keys=OFF;
                PRAGMA legacy_alter_table=ON;
                CREATE TABLE filesystems_new (
                    id INTEGER PRIMARY KEY, volume_id INTEGER REFERENCES volumes(id),
                    dataset_id INTEGER REFERENCES datasets(id), partition_id INTEGER REFERENCES partitions(id),
                    start INTEGER NOT NULL, length INTEGER NOT NULL, fstype TEXT NOT NULL, plugin TEXT,
                    label TEXT, uuid TEXT, block_size INTEGER, state TEXT, info_json TEXT);
                INSERT INTO filesystems_new(id,volume_id,partition_id,start,length,fstype,plugin,label,uuid,
                                            block_size,state,info_json)
                    SELECT id,volume_id,partition_id,start,length,fstype,plugin,label,uuid,block_size,state,info_json
                    FROM filesystems;
                DROP TABLE filesystems;
                ALTER TABLE filesystems_new RENAME TO filesystems;
                PRAGMA legacy_alter_table=OFF;
                PRAGMA foreign_keys=ON;
            """)
            bad = self.conn.execute("SELECT name FROM sqlite_master WHERE instr(sql, 'filesystems_old') OR instr(sql, 'filesystems_new')").fetchall()
            if bad:
                raise RuntimeError(f"map migration left dangling references in {bad}")
        self.conn.execute("DROP TABLE IF EXISTS volume_blocks")

    # -- meta / progress ----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def is_done(self, phase: str, unit: str = "*") -> bool:
        r = self.conn.execute("SELECT state FROM progress WHERE phase=? AND unit=?", (phase, unit)).fetchone()
        return bool(r and r[0] == "done")

    def done_units(self, phase: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT unit FROM progress WHERE phase=? AND state='done'", (phase,))}

    def mark(self, phase: str, unit: str = "*", state: str = "done", detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO progress(phase,unit,state,detail,finished_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(phase,unit) DO UPDATE SET state=excluded.state, detail=excluded.detail, "
            "finished_at=excluded.finished_at", (phase, unit, state, detail, now()))

    def reset_phase(self, phase: str) -> None:
        self.conn.execute("DELETE FROM progress WHERE phase=?", (phase,))

    def event(self, level: str, component: str, message: str, **context: Any) -> None:
        self.conn.execute("INSERT INTO events(ts,level,component,message,context) VALUES(?,?,?,?,?)",
                          (now(), level, component, message,
                           json.dumps(context, default=str) if context else None))

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(row.values()))
        return int(cur.lastrowid or 0)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def j(obj: Any) -> str:
    """JSON-encode with big ints and bytes handled."""
    def default(o: Any) -> Any:
        if isinstance(o, (bytes, bytearray)):
            return o.hex()
        return str(o)
    return json.dumps(obj, default=default, sort_keys=True)
