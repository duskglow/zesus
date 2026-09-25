"""Pool history log (``zpool history -il``): a ring buffer of packed nvlist records.

The object's bonus is ``spa_history_phys_t``:

    sh_pool_create_len   records from pool creation, never overwritten
    sh_phys_max_off      physical size of the log
    sh_bof, sh_eof       logical begin/end offsets of valid data
    sh_records_lost      records dropped by wrap-around

For recovery this log is gold. It records the name, dsobj and txg of every
``zfs create``/``destroy`` and property change, including datasets that no longer
exist anywhere else in the pool.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

from . import nvlist
from .dnode import ObjectReader

log = logging.getLogger(__name__)

MAX_RECORD = 1 << 20


@dataclass
class HistoryRecord:
    offset: int                       # logical offset in the log
    fields: dict[str, Any]

    @property
    def time(self) -> int | None:
        return self.fields.get("history time")

    @property
    def txg(self) -> int | None:
        return self.fields.get("history txg")

    @property
    def command(self) -> str | None:
        return self.fields.get("history command")

    @property
    def internal_name(self) -> str | None:
        return self.fields.get("internal_name")

    @property
    def internal_str(self) -> str | None:
        return self.fields.get("history internal str")

    @property
    def dsname(self) -> str | None:
        return self.fields.get("dsname")

    @property
    def dsid(self) -> int | None:
        return self.fields.get("dsid")

    def summary(self) -> str:
        f = self.fields
        if "history command" in f:
            return f"cmd: {f['history command']}"
        if "internal_name" in f or "history internal event" in f:
            name = f.get("internal_name") or f"event {f.get('history internal event')}"
            ds = f.get("dsname", "")
            dsid = f.get("dsid")
            s = f.get("history internal str", "")
            return f"internal: {name} {ds}{f' (dsobj {dsid})' if dsid is not None else ''} {s}".rstrip()
        if "ioctl" in f:
            return f"ioctl: {f['ioctl']} {f.get('in', '')}"
        return f"record: {sorted(f)}"


@dataclass
class HistoryLog:
    create_len: int
    phys_max: int
    bof: int
    eof: int
    records_lost: int
    records: list[HistoryRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def read_history(obj: ObjectReader) -> HistoryLog:
    bonus = obj.dn.bonus
    if len(bonus) < 40:
        raise ValueError("history object has no spa_history_phys_t bonus")
    create_len, phys_max, bof, eof, lost = struct.unpack_from("<5Q", bonus, 0)
    hl = HistoryLog(create_len, phys_max, bof, eof, lost)
    raw = obj.read(0, min(phys_max, obj.dn.logical_size))

    def phys(logical: int) -> int:
        if logical < create_len:
            return logical
        ring = phys_max - create_len
        return create_len + (logical - create_len) % ring if ring > 0 else logical

    def read_logical(off: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            p = phys(off)
            end = phys_max if p >= create_len else create_len
            take = min(n, end - p)
            if take <= 0:
                break
            out += raw[p:p + take]
            off += take
            n -= take
        return bytes(out)

    def parse_region(start: int, end: int) -> None:
        off = start
        while off + 8 <= end:
            (ln,) = struct.unpack("<Q", read_logical(off, 8))
            if ln == 0 or ln > MAX_RECORD or off + 8 + ln > end:
                hl.errors.append(f"bad record length {ln} at logical {off}")
                break
            try:
                rec = nvlist.unpack(read_logical(off + 8, ln))
                hl.records.append(HistoryRecord(off, rec))
            except Exception as exc:
                hl.errors.append(f"record at {off}: {exc}")
            off += 8 + ln

    # The pool-creation region is kept forever. The ring holds [bof, eof). After a wrap,
    # bof may land mid-record; ZFS advances bof to a record boundary, so start there.
    parse_region(0, min(create_len, eof))
    parse_region(max(bof, create_len), eof)
    if lost:
        log.info("pool history: %d records were lost to wrap-around", lost)
    return hl
