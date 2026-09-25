"""Signature-only identification of filesystems and containers without a full plugin.

These give the user an accurate picture ("partition 2 is NTFS, 97% recoverable") and a
target for extraction as a raw image, even before an inventory plugin exists.
"""

from __future__ import annotations

import struct
import uuid

from ..api import Device, FsInfo, Identifier


class _Sig(Identifier):
    def __init__(self, name: str, offset: int, magic: bytes, fstype: str | None = None, parse=None) -> None:
        self.name = name
        self.offset, self.magic, self.fstype, self.parse = offset, magic, fstype or name, parse

    def identify(self, dev: Device) -> FsInfo | None:
        b = dev.pread(self.offset, len(self.magic))
        if b != self.magic:
            return None
        info = FsInfo(fstype=self.fstype)
        if self.parse:
            try:
                self.parse(dev, info)
            except Exception:
                pass
        return info


def _xfs(dev: Device, info: FsInfo) -> None:
    sb = dev.pread(0, 512)
    bs, dblocks = struct.unpack_from(">IQ", sb, 4)
    info.block_size, info.size = bs, bs * dblocks
    info.uuid = str(uuid.UUID(bytes=sb[32:48]))
    info.label = sb[108:120].split(b"\0", 1)[0].decode("utf-8", "replace")


def _ntfs(dev: Device, info: FsInfo) -> None:
    bs = dev.pread(0, 512)
    bps, spc = struct.unpack_from("<HB", bs, 11)
    total = struct.unpack_from("<Q", bs, 40)[0]
    info.block_size = bps * spc
    info.size = total * bps
    info.uuid = f"{struct.unpack_from('<Q', bs, 72)[0]:016X}"


def _btrfs(dev: Device, info: FsInfo) -> None:
    sb = dev.pread(0x10000, 4096)
    info.uuid = str(uuid.UUID(bytes=sb[0x20:0x30]))
    info.size = struct.unpack_from("<Q", sb, 0x70)[0]
    info.block_size = struct.unpack_from("<I", sb, 0x90)[0]
    info.label = sb[0x12B:0x12B + 256].split(b"\0", 1)[0].decode("utf-8", "replace")


def _luks(dev: Device, info: FsInfo) -> None:
    hdr = dev.pread(0, 512)
    ver = struct.unpack_from(">H", hdr, 6)[0]
    info.details["version"] = ver
    info.uuid = hdr[168:208].split(b"\0", 1)[0].decode("ascii", "replace")
    info.warnings.append("encrypted container: contents need the passphrase/key")


def _fat(dev: Device, info: FsInfo) -> None:
    bs = dev.pread(0, 512)
    bps, spc = struct.unpack_from("<HB", bs, 11)
    info.block_size = bps * spc
    info.label = bs[71:82].decode("ascii", "replace").strip()


BUILTIN: list[Identifier] = [
    _Sig("xfs", 0, b"XFSB", parse=_xfs),
    _Sig("ntfs", 3, b"NTFS    ", parse=_ntfs),
    _Sig("exfat", 3, b"EXFAT   "),
    _Sig("fat32", 82, b"FAT32   ", "vfat", parse=_fat),
    _Sig("fat16", 54, b"FAT16   ", "vfat", parse=_fat),
    _Sig("fat12", 54, b"FAT12   ", "vfat", parse=_fat),
    _Sig("btrfs", 0x10040, b"_BHRfS_M", parse=_btrfs),
    _Sig("luks", 0, b"LUKS\xba\xbe", "crypto_LUKS", parse=_luks),
    _Sig("bitlocker", 3, b"-FVE-FS-", "BitLocker"),
    _Sig("lvm2", 0x218, b"LVM2 001", "LVM2_member"),
    _Sig("linux-swap", 4086, b"SWAPSPACE2", "swap"),
    _Sig("linux-swap-old", 4086, b"SWAP-SPACE", "swap"),
    _Sig("iso9660", 0x8001, b"CD001", "iso9660"),
    _Sig("f2fs", 0x400, b"\x10\x20\xf5\xf2", "f2fs"),
    _Sig("hfsplus", 0x400, b"H+", "hfsplus"),
    _Sig("apfs", 32, b"NXSB", "apfs"),
    _Sig("zfs-member", 0x4000, b"\x01\x01\x00\x00", "zfs_member"),
    _Sig("md-raid", 0x1000, b"\xfc\x4e\x2b\xa9", "linux_raid_member"),
    _Sig("reiserfs", 0x10034, b"ReIsEr", "reiserfs"),
]
