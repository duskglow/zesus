"""GUID Partition Table. Falls back to the backup header if the primary is damaged."""

from __future__ import annotations

import logging
import struct
import uuid
import zlib

from . import BlockReader, Partition

log = logging.getLogger(__name__)

KNOWN_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux /home",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "bc13c2ff-59e6-4262-a352-b275fd6f7172": "Linux extended boot",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "Windows LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "Windows LDM data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Windows Storage Spaces",
    "6a898cc3-1dd2-11b2-99a6-080020736631": "ZFS (Solaris /usr)",
    "516e7cba-6ecf-11d6-8ff8-00022d09712b": "FreeBSD ZFS",
    "6a945a3b-1dd2-11b2-99a6-080020736631": "Solaris reserved",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "516e7cb4-6ecf-11d6-8ff8-00022d09712b": "FreeBSD UFS",
}
ZFS_TYPES = {"6a898cc3-1dd2-11b2-99a6-080020736631", "516e7cba-6ecf-11d6-8ff8-00022d09712b"}


class GptScheme:
    name = "gpt"

    def __init__(self, sector_sizes: tuple[int, ...] = (512, 4096)) -> None:
        self.sector_sizes = sector_sizes

    def _header(self, dev: BlockReader, lba: int, ss: int) -> dict | None:
        hdr = dev.pread(lba * ss, 92)
        if len(hdr) < 92 or hdr[:8] != b"EFI PART":
            return None
        hsize = struct.unpack_from("<I", hdr, 12)[0]
        if not 92 <= hsize <= ss:
            return None
        full = bytearray(dev.pread(lba * ss, hsize))
        crc = struct.unpack_from("<I", full, 16)[0]
        struct.pack_into("<I", full, 16, 0)
        (my_lba, alt_lba, first, last) = struct.unpack_from("<QQQQ", full, 24)
        disk_guid = uuid.UUID(bytes_le=bytes(full[56:72]))
        pe_lba, n, esz, pe_crc = struct.unpack_from("<QIII", full, 72)
        return {"crc_ok": zlib.crc32(full) == crc, "my_lba": my_lba, "alt_lba": alt_lba,
                "first": first, "last": last, "disk_guid": str(disk_guid), "pe_lba": pe_lba,
                "n": n, "esz": esz, "pe_crc": pe_crc, "ss": ss}

    def _find(self, dev: BlockReader) -> dict | None:
        for ss in self.sector_sizes:
            h = self._header(dev, 1, ss)
            if h and h["crc_ok"]:
                return h
            last = dev.size // ss - 1
            b = self._header(dev, last, ss) if last > 1 else None
            if b and b["crc_ok"]:
                log.warning("primary GPT header damaged; using backup header at LBA %d", last)
                return b
            if h:
                log.warning("GPT header CRC mismatch (sector size %d); using it anyway", ss)
                return h
        return None

    def probe(self, dev: BlockReader) -> bool:
        return self._find(dev) is not None

    def parse(self, dev: BlockReader) -> list[Partition]:
        h = self._find(dev)
        if h is None:
            return []
        ss, n, esz = h["ss"], min(h["n"], 1024), h["esz"]
        if esz < 128 or esz > 4096:
            log.warning("GPT entry size %d implausible", esz)
            return []
        table = dev.pread(h["pe_lba"] * ss, n * esz)
        if zlib.crc32(table[: h["n"] * esz]) != h["pe_crc"]:
            log.warning("GPT partition-entry array CRC mismatch; entries may be damaged")
        parts: list[Partition] = []
        for i in range(n):
            e = table[i * esz:(i + 1) * esz]
            if len(e) < 128 or e[:16] == b"\0" * 16:
                continue
            t = str(uuid.UUID(bytes_le=bytes(e[:16])))
            start, end = struct.unpack_from("<QQ", e, 32)
            if end < start:
                continue
            parts.append(Partition(
                scheme="gpt", index=i + 1, start=start * ss, length=(end - start + 1) * ss,
                type_id=t, type_name=KNOWN_TYPES.get(t, "unknown"),
                name=e[56:128].decode("utf-16-le", "replace").rstrip("\0"),
                uuid=str(uuid.UUID(bytes_le=bytes(e[16:32]))),
                flags={"attributes": struct.unpack_from("<Q", e, 48)[0], "sector_size": ss},
            ))
        return parts
