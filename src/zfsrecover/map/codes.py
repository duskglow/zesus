"""Per-block status codes stored in the map (one byte per logical block)."""

from __future__ import annotations

from enum import IntEnum


class BlockStatus(IntEnum):
    UNKNOWN = 0          # a candidate exists but has not been verified yet
    OK = 1               # checksum verified against the newest known version
    HOLE = 2             # never written: reads as zeros, nothing lost
    EMBEDDED = 3         # data stored inside the block pointer itself
    CKSUM_MISMATCH = 4   # every candidate copy failed its checksum (overwritten)
    ZEROED = 5           # every candidate reads back as zeros (freed and trimmed)
    NO_METADATA = 6      # no surviving indirect block describes this block
    UNREADABLE = 7       # beyond the device, missing vdev, or unsupported layout
    DECOMPRESS_FAIL = 8  # checksum ok but decompression failed (should not happen)
    OK_STALE = 9         # verified, but an older version than the newest known one
    DISCARDED = 10       # written once, later freed by the guest (TRIM/UNMAP): zeros


RECOVERED = {BlockStatus.OK, BlockStatus.EMBEDDED, BlockStatus.OK_STALE}
ZERO_BY_DESIGN = {BlockStatus.HOLE, BlockStatus.DISCARDED}
LOST = {BlockStatus.CKSUM_MISMATCH, BlockStatus.ZEROED, BlockStatus.NO_METADATA,
        BlockStatus.UNREADABLE, BlockStatus.DECOMPRESS_FAIL}

DESCRIPTIONS = {
    BlockStatus.UNKNOWN: "not yet verified",
    BlockStatus.OK: "recovered (checksum verified)",
    BlockStatus.HOLE: "never written (zeros)",
    BlockStatus.EMBEDDED: "recovered (embedded in block pointer)",
    BlockStatus.CKSUM_MISMATCH: "lost: overwritten after the dataset was destroyed (checksum mismatch)",
    BlockStatus.ZEROED: "lost: space was trimmed/zeroed after being freed",
    BlockStatus.NO_METADATA: "lost: no surviving metadata points to this block",
    BlockStatus.UNREADABLE: "lost: location unreadable (device missing or unsupported layout)",
    BlockStatus.DECOMPRESS_FAIL: "lost: data verified but could not be decompressed",
    BlockStatus.OK_STALE: "recovered from an older version (newer version overwritten)",
    BlockStatus.DISCARDED: "discarded by the guest (TRIM/UNMAP): zeros",
}
