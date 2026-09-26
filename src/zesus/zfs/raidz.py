"""RAIDZ geometry: a line-for-line port of OpenZFS ``vdev_raidz_map_alloc``.

A block at RAIDZ offset *off* with physical size *psize* is spread over the children of a
``dcols``-wide vdev with ``nparity`` parity columns:

* ``b = off >> ashift`` is the block's first sector in RAIDZ space. That space is laid out
  row-major over the children: sector ``s`` is on child ``s % dcols``, at row ``s // dcols``.
* Column 0..nparity-1 are parity, the rest data. Column ``c`` lives on child
  ``(b % dcols + c) % dcols``, one row further down if that wrapped.
* Each column holds a *contiguous* run of the block's sectors on its child: data column
  ``nparity`` holds the first ``q`` (or ``q+1``) sectors of the block, the next one the
  following run, and so on. "Big" columns (the first ``bc``) get one sector more.
* For single parity, when bit 20 of the offset is set, the parity column and the first
  data column swap children (OpenZFS spreads parity reads that way).

Nothing here reads data. :class:`RaidzMap` only says where each column is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    devidx: int          # child id within the RAIDZ vdev
    offset: int          # child-relative DVA-space offset (add the 4 MiB label area to read)
    size: int            # bytes (0 for skipped columns, which we do not include)


@dataclass(frozen=True)
class RaidzMap:
    offset: int
    psize: int           # rounded up to the sector size
    ashift: int
    dcols: int
    nparity: int
    cols: tuple[Column, ...]      # accessed columns only (acols), parity first
    bigcols: int
    nskip: int
    asize: int                    # allocated size including parity and skip sectors

    @property
    def parity_cols(self) -> tuple[Column, ...]:
        return self.cols[:self.nparity]

    @property
    def data_cols(self) -> tuple[Column, ...]:
        return self.cols[self.nparity:]


def roundup(x: int, y: int) -> int:
    return -(-x // y) * y


def asize_of(psize: int, ashift: int, dcols: int, nparity: int) -> int:
    """``vdev_raidz_asize``: allocated bytes for a block of *psize*."""
    s = ((psize - 1) >> ashift) + 1
    s += nparity * ((s + dcols - nparity - 1) // (dcols - nparity))
    s = roundup(s, nparity + 1)
    return s << ashift


def map_alloc(offset: int, psize: int, ashift: int, dcols: int, nparity: int) -> RaidzMap:
    if dcols <= nparity or nparity < 1 or nparity > 3:
        raise ValueError(f"bad raidz geometry dcols={dcols} nparity={nparity}")
    if psize <= 0:
        raise ValueError("psize must be positive")
    sector = 1 << ashift
    size = roundup(psize, sector)              # zio_vdev_io_start pads I/O to the ashift
    b = offset >> ashift
    s = size >> ashift
    f = b % dcols
    o = (b // dcols) << ashift
    q = s // (dcols - nparity)
    r = s - q * (dcols - nparity)
    bc = 0 if r == 0 else r + nparity
    tot = s + nparity * (q + (0 if r == 0 else 1))
    if q == 0:
        acols = bc
    else:
        acols = dcols
    cols: list[list[int]] = []
    asz = 0
    for c in range(acols):
        col = f + c
        coff = o
        if col >= dcols:
            col -= dcols
            coff += sector
        csize = (q + 1) << ashift if c < bc else q << ashift
        asz += csize
        cols.append([col, coff, csize])
    assert asz == tot << ashift
    nskip = roundup(tot, nparity + 1) - tot
    if nparity == 1 and (offset & (1 << 20)):
        cols[0][0], cols[1][0] = cols[1][0], cols[0][0]
        cols[0][1], cols[1][1] = cols[1][1], cols[0][1]
    return RaidzMap(offset=offset, psize=size, ashift=ashift, dcols=dcols, nparity=nparity,
                    cols=tuple(Column(*c) for c in cols), bigcols=bc, nskip=nskip,
                    asize=roundup(tot, nparity + 1) << ashift)


def child_row_range(start: int, end: int, ashift: int, dcols: int) -> tuple[int, int]:
    """Child-relative byte range covering every sector of RAIDZ range [start, end).

    RAIDZ space is row-major across children, so the range touches the same rows on every
    child (plus one row for columns that wrap).
    """
    sector = 1 << ashift
    first_row = (start >> ashift) // dcols
    last_row = (((end + sector - 1) >> ashift) + dcols - 1) // dcols + 1   # +1: wrapped columns
    return first_row << ashift, last_row << ashift


def first_data_origins(child: int, child_off: int, ashift: int, dcols: int,
                       nparity: int) -> list[int]:
    """RAIDZ offsets of blocks whose first data column would start at (child, child_off).

    Used by carving: a block header found on a member disk sits at the start of the first
    data column. The candidates still need confirming with :func:`map_alloc` for the real
    psize (see :func:`locates_first_data`).
    """
    sector = 1 << ashift
    row = child_off >> ashift
    out = []
    # Normal layout: first data column is column `nparity`, at child (f + nparity) % dcols.
    f = child - nparity
    r = row
    if f < 0:
        f += dcols
        r -= 1                                   # the column wrapped to the next row
    if r >= 0:
        out.append((r * dcols + f) * sector)
    if nparity == 1:
        # Swapped layout (offset bit 20 set): data column 1 sits where column 0 would be.
        out.append((row * dcols + child) * sector)
    return out


def locates_first_data(rm: RaidzMap, child: int, child_off: int) -> bool:
    if len(rm.cols) <= rm.nparity:
        return False
    c = rm.cols[rm.nparity]
    return c.devidx == child and c.offset == child_off
