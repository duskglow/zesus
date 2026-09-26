# Architecture

```
            ┌────────────────────────── zesus scan ───────────────────────────┐
 source ──► │ io (read-only) → zfs core → phases → SQLite map                      │
 (image/    │   history · datasets · carve · reconstruct · verify · contents       │
  device)   └──────────────────────────────────────────────────────────────────────┘
                                             │ map (index only: locations, sizes,
                                             ▼       checksums, statuses)
            ┌──────────────────────── zesus extract ──────────────────────────┐
 source ──► │ map + source → re-verified blocks → images / files + gap reports     │
            └──────────────────────────────────────────────────────────────────────┘
```

## Layers

| Package | Role |
|---|---|
| `io` | `ReadOnlySource` (image files, block devices incl. `\\.\PhysicalDriveN`), `SliceSource`, `SourceSet` (one file per vdev member), the audit-hook write guard. |
| `zfs` | On-disk format: labels/uberblocks, nvlists (XDR + native), block pointers, checksums, compression, dnodes, objsets, ZAP, DSL, pool history, verified block reader. Vdev layer: disk, mirror, RAIDZ1/2/3 (`vdev`, `raidz`, `gf256`); member assessment (`members`). |
| `carve` | Raw-device carving (`scanner`; `raidz_carve` for RAIDZ), multi-generation tree reconstruction (`reconstruct`), data verification (`verify`). |
| `volume` | `LogicalVolume`: a reconstructed zvol as a gap-aware block device; `Coverage` for fast range queries. |
| `partitions` | GPT and MBR plugins. |
| `fs` | Filesystem plugin API, ext4 plugin, signature identifiers. |
| `extract` | Extraction engine, sparse output, manifests, ddrescue mapfiles. |
| `scan` | Phase pipeline with resumable progress. |
| `map` | SQLite schema and access. |
| `web` | Optional local web UI: browse, scan, extract, send; progress and cancel. |
| `parallel`, `progress` | Ordered pipelines with bounded read-ahead; progress (rate, ETA) published to the log and the map. |
| `combine` | `--strategy combined` for mirrors: one vdev image from the healthiest member per region. |

## Scan phases

1. **history**: decodes the pool history log from the newest readable MOS. It records
   every `zfs create`/`destroy` with the dataset name, dsobj and txg, including datasets
   that exist nowhere else any more.
2. **datasets**: walks the DSL from *every* uberblock in the label rings. It merges what
   it finds with the history log by `(dsobj, creation_txg)`. It also stores every distinct
   objset root seen (`dataset_roots`).
3. **carve**: reads the whole vdev sequentially in 64 MiB chunks. A vectorized pre-filter
   finds uncompressed objsets and ZFS-LZ4 frames. The candidates are decompressed and
   classified: objsets, dnode blocks, indirect blocks (all children must be valid block
   pointers with one type and level) and ZAPs. Each chunk commits atomically with its
   progress row.
4. **reconstruct**: for every volume, it collects all surviving *generations* of its
   block tree (ring roots, carved objsets, carved meta-dnode blocks). It walks them
   top-down. Carved orphan indirect blocks are placed by matching `(slot, DVA)` of their
   children against already-placed blocks: copy-on-write changes only the modified
   children, so generations share most pointers. Candidates for every logical block are
   ordered newest-first by birth txg.
5. **verify**: reads every chosen data block in physical order and checks its checksum.
   A block that fails is retried on its other DVAs and then on older generations
   (`ok_stale`). Progress is checkpointed.
6. **contents**: partitions → filesystem detection → plugin inventory → per-file status
   (`full` / `partial` / `none`) computed from the volume's coverage. ZFS filesystem
   datasets are inventoried by `zfs/zpl.py`, with every file block checksum-verified. For
   those files the map stores the unrecoverable ranges directly, since they are not
   inside a volume.

Known limitations:
* Destroyed ZFS *filesystem* datasets are recovered when an uberblock in the ring can
  still reach them. Carved-only reconstruction (as done for volumes) is not implemented
  for them yet.
* dRAID, RAIDZ vdevs that were expanded (`raidz_expansion`), and encrypted datasets are
  detected but not decoded.
* RAIDZ carving can rebuild headers that sat on a *missing* member only for RAIDZ1 with
  one member missing. With more parity, blocks whose header is on a missing member are
  still read (through parity) when a parent pointer leads to them, but carving does not
  find them.

## Multi-disk vdevs

Each evidence file is matched to its place in the pool by the guid in its own label. A
top-level vdev then turns `(offset, psize)` into **candidate readings**, cheapest first.
The block reader accepts the first candidate that matches the parent block pointer's
checksum, so the vdev layer never decides that data is correct:

* **mirror**: one reading per present child, the child with the fewest checksum failures
  first.
* **RAIDZ**: `zfs/raidz.py` is a port of OpenZFS `vdev_raidz_map_alloc`. It covers columns,
  the wrap to the next row, skip sectors, and the RAIDZ1 "bit 20" parity/data swap. The
  candidates are:
  1. the data columns as read;
  2. then missing columns rebuilt from parity. `zfs/gf256.py` computes, over
     GF(2^8)/0x11d, P = XOR, Q = sum of 2^(n-1-i)*D_i and R = sum of 4^(n-1-i)*D_i, and
     solves any <= p erasures;
  3. then, if every column was present but the checksum failed, each column (pair,
     triple) in turn treated as damaged ("combinatorial reconstruction").

  More losses than parity yields no reading, so the result is a reported gap.

Bulk reads (verify, extract) use windows over DVA space. On RAIDZ a DVA range is the
same row range on every child, so each member is read sequentially and in parallel with
the others. Blocks are then assembled, and rebuilt where needed, from those buffers.

**Carving RAIDZ.** DVA space is not a linear image, so carving works on member rows. A
header found on a member is taken as the start of a first data column. It is accepted
only if the map of the implied block puts that column back exactly there. For RAIDZ1
with one member missing, headers on the missing disk are rebuilt through the parity
window, trying each possible column count.

A block with one data column has parity identical to its data. Its header is therefore
also seen on the parity disk, where it looks like a block one sector away. Such "echoes"
are recognized as two hits that share a sector and have identical content, and only the
better-attested one is kept.

**Validation.** The RAIDZ math is checked against:
* an independent byte-loop reference written from the OpenZFS source;
* every erasure pattern for p = 1..3;
* hand-computed maps;
* synthetic member disks;
* real OpenZFS pools (`dev/make_zfs_fixtures.sh`). On these, every block's on-disk
  parity must equal the parity Zesus computes, which proves the layout and the Q/R
  coefficient order.

## Parallelism

Profiling first showed that:
* carving a single spinning disk is I/O-bound (reads take about 3x the CPU time);
* RAIDZ verify and extract are network-bound when cold and CPU-bound when cached.

So the work is split into stages:
* One thread reads the **next** window or chunk of a plan that is read anyway. It never
  reads speculatively. By default it has one read in flight per evidence file
  (`--io-depth`), which suits spinning disks and network shares.
* CPU work runs on `--workers` threads (checksums, decompression, parity: these kernels
  release the GIL) or processes (carve classification).
* Results are applied **in order**. Checkpoints, map rows and output files are therefore
  exactly what a serial run produces, which the determinism tests check.

## Why the map stores L1 pointers instead of every L0

An 880 GiB zvol with 16 KiB blocks has 57 million logical blocks. The map stores, per
1024-block span, the candidate level-1 indirect block pointers (128 bytes each), plus a
one-byte choice and a one-byte status per slot. That is about 150 MB instead of about 7 GB.
Every L0 location, size and checksum is derived from a checksum-verified parent at read
time. `zesus-extract` never trusts anything it has not just verified.

## Block statuses

See `zesus/map/codes.py`: `ok`, `ok_stale`, `embedded`, `hole`, `discarded`
(recoverable or zero by design); `cksum_mismatch`, `zeroed`, `no_metadata`, `unreadable`,
`decompress_fail` (lost, with reason).
