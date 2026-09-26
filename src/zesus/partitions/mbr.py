"""MBR / DOS partition table, including extended and logical partitions."""

from __future__ import annotations

import logging
import struct

from . import BlockReader, Partition

log = logging.getLogger(__name__)

TYPES = {
    0x01: "FAT12", 0x04: "FAT16 <32M", 0x05: "Extended", 0x06: "FAT16", 0x07: "NTFS/exFAT/HPFS",
    0x0B: "FAT32 (CHS)", 0x0C: "FAT32 (LBA)", 0x0E: "FAT16 (LBA)", 0x0F: "Extended (LBA)",
    0x11: "Hidden FAT12", 0x17: "Hidden NTFS", 0x27: "Windows RE", 0x42: "Windows LDM",
    0x82: "Linux swap / Solaris", 0x83: "Linux", 0x85: "Linux extended", 0x8E: "Linux LVM",
    0xA5: "FreeBSD", 0xA6: "OpenBSD", 0xA8: "Darwin UFS", 0xAF: "HFS/HFS+", 0xBF: "Solaris/ZFS",
    0xEE: "GPT protective", 0xEF: "EFI System", 0xFD: "Linux RAID autodetect",
}
EXTENDED = {0x05, 0x0F, 0x85}


class MbrScheme:
    name = "mbr"

    def probe(self, dev: BlockReader) -> bool:
        s = dev.pread(0, 512)
        if len(s) < 512 or s[510:512] != b"\x55\xaa":
            return False
        entries = [s[446 + i * 16: 462 + i * 16] for i in range(4)]
        if any(e[4] == 0xEE for e in entries):
            return False  # protective MBR: GPT owns the disk
        # A FAT/NTFS boot sector also ends in 55AA. Require sane entries.
        ok = False
        for e in entries:
            if e[0] not in (0, 0x80):
                return False
            if e[4]:
                ok = True
        return ok

    def parse(self, dev: BlockReader) -> list[Partition]:
        s = dev.pread(0, 512)
        parts: list[Partition] = []
        for i in range(4):
            e = s[446 + i * 16: 462 + i * 16]
            ptype = e[4]
            lba, n = struct.unpack_from("<II", e, 8)
            if not ptype or not n:
                continue
            parts.append(self._mk(i + 1, lba, n, ptype, e[0] == 0x80))
            if ptype in EXTENDED:
                parts.extend(self._logical(dev, lba))
        return parts

    def _mk(self, idx: int, lba: int, n: int, ptype: int, boot: bool) -> Partition:
        return Partition(scheme="mbr", index=idx, start=lba * 512, length=n * 512,
                         type_id=f"0x{ptype:02x}", type_name=TYPES.get(ptype, "unknown"),
                         flags={"bootable": boot})

    def _logical(self, dev: BlockReader, ext_base: int) -> list[Partition]:
        out: list[Partition] = []
        ebr_lba, idx, seen = ext_base, 5, set()
        while ebr_lba and ebr_lba not in seen and len(out) < 128:
            seen.add(ebr_lba)
            s = dev.pread(ebr_lba * 512, 512)
            if len(s) < 512 or s[510:512] != b"\x55\xaa":
                log.warning("broken EBR chain at LBA %d", ebr_lba)
                break
            e1, e2 = s[446:462], s[462:478]
            lba, n = struct.unpack_from("<II", e1, 8)
            if e1[4] and n:
                out.append(self._mk(idx, ebr_lba + lba, n, e1[4], e1[0] == 0x80))
                idx += 1
            nxt = struct.unpack_from("<I", e2, 8)[0]
            ebr_lba = ext_base + nxt if e2[4] in EXTENDED and nxt else 0
        return out
