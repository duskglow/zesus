"""``--strategy combined``: build one vdev image from the members of a mirror.

Every child of a mirror holds the same vdev contents, at the same vdev-relative
offsets. The combined image is that vdev (labels included), so the normal single-disk
pipeline can run on it and it can be re-scanned many times without the member disks.

Each region comes from the healthiest member that has it: a region that a member's
ddrescue mapfile marks as not rescued is taken from another member. Where no member
has a region, it is left as a hole *and* reported. Nothing is inferred. Every block is
still checksum-verified when the image is scanned, exactly as with the members.

RAIDZ cannot be combined this way. Its blocks are split into columns with a layout that
depends on each block's own size and position, so a missing member can only be rebuilt
one known block at a time. RAIDZ is always read in place.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .extract.engine import GapRecord, write_ddrescue_map
from .extract.sparse import make_sparse, set_size
from .io import guard
from .io.sourceset import SourceSet
from .zfs.pool import open_pools

log = logging.getLogger(__name__)

REGION = 1 << 20


class CannotCombine(RuntimeError):
    pass


@dataclass
class BadRanges:
    """Ranges a ddrescue mapfile says were not rescued (anything but '+')."""
    ranges: list[tuple[int, int]]

    @classmethod
    def parse(cls, path: str | Path) -> BadRanges:
        out = []
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if not parts or line.startswith("#") or len(parts) < 3:
                continue
            try:
                pos, size = int(parts[0], 0), int(parts[1], 0)
            except ValueError:
                continue                      # the current_pos/status line
            if parts[2] != "+":
                out.append((pos, pos + size))
        return cls(sorted(out))

    def overlaps(self, a: int, b: int) -> bool:
        return any(s < b and a < e for s, e in self.ranges)


@dataclass
class CombineResult:
    path: Path
    size: int
    regions_from: dict[str, int]
    missing: list[tuple[int, int]]


def combine(paths: list[str], out: str | Path, mapfiles: dict[str, str] | None = None,
            progress=None, stop=None) -> CombineResult:
    mapfiles = mapfiles or {}
    out = Path(out)
    guard.assert_not_protected([out])
    with SourceSet.open(paths) as ss:
        pools = open_pools(ss)
        if len(pools) != 1:
            raise CannotCombine(f"expected the members of one pool, found {len(pools)}")
        pool = pools[0]
        if len(pool.vdevs.top) != 1:
            raise CannotCombine("combining pools with several top-level vdevs is not supported; scan them in place")
        top = next(iter(pool.vdevs.top.values()))
        if top.type == "raidz" or top.type.startswith("draid"):
            raise CannotCombine(
                f"{top.type} cannot be combined into one image: each block's columns follow its own "
                "layout, so a missing disk can only be rebuilt block by block. Use --strategy inplace "
                "(the default), which reads the member images directly.")
        ims = [im for im in pool.vdev_images]
        bad = {im.name: BadRanges.parse(mapfiles[im.name]) if im.name in mapfiles else BadRanges([])
               for im in ims}
        # healthiest first: fewest bad bytes in its mapfile
        ims.sort(key=lambda im: sum(e - s for s, e in bad[im.name].ranges))
        size = min(im.source.size for im in ims)
        used: dict[str, int] = {im.name: 0 for im in ims}
        missing: list[tuple[int, int]] = []
        out.parent.mkdir(parents=True, exist_ok=True)
        if progress:
            progress.begin("combine", size, "bytes")
        with open(out, "wb") as f:
            make_sparse(f)
            set_size(f, size)
            for off in range(0, size, REGION):
                if stop is not None and getattr(stop, "event", stop).is_set():
                    raise CannotCombine("cancelled; the combined image is incomplete and must not be used")
                n = min(REGION, size - off)
                # a member's mapfile is relative to its whole image file, the vdev starts at base_offset
                src = next((im for im in ims if not bad[im.name].overlaps(im.base_offset + off,
                                                                          im.base_offset + off + n)), None)
                if src is None:
                    missing.append((off, n))
                    continue
                data = src.source.pread(off, n)
                used[src.name] += 1
                if any(data):
                    f.seek(off)
                    f.write(data)
                if progress:
                    progress.advance(n)
    write_ddrescue_map(Path(str(out) + ".mapfile"), size,
                       [GapRecord(a, b, "not rescued on any member") for a, b in missing])
    Path(str(out) + ".provenance.json").write_text(json.dumps({
        "pool": pool.name, "vdev_type": top.type, "members": [im.name for im in ims],
        "regions_from": used, "region_size": REGION,
        "missing": [{"offset": a, "length": b} for a, b in missing],
        "note": "regions not rescued on any member are holes; every block is still checksum-verified "
                "when this image is scanned"}, indent=1), encoding="utf-8")
    log.info("combined %s -> %s (%d MiB); regions per member: %s; %d region(s) on no member",
             ", ".join(im.name for im in ims), out, size >> 20, used, len(missing))
    return CombineResult(out, size, used, missing)
