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
| `io` | `ReadOnlySource` (image files, block devices incl. `\\.\PhysicalDriveN`), `SliceSource`, the audit-hook write guard. |
| `zfs` | On-disk format: labels/uberblocks, nvlists (XDR + native), block pointers, checksums, compression, dnodes, objsets, ZAP, DSL, pool history, verified block reader. |
| `carve` | Raw-device carving (`scanner`, `classify`), multi-generation tree reconstruction (`reconstruct`), data verification (`verify`). |
| `volume` | `LogicalVolume`: a reconstructed zvol as a gap-aware block device; `Coverage` for fast range queries. |
| `partitions` | GPT and MBR plugins. |
| `fs` | Filesystem plugin API, ext4 plugin, signature identifiers. |
| `extract` | Extraction engine, sparse output, manifests, ddrescue mapfiles. |
| `scan` | Phase pipeline with resumable progress. |
| `map` | SQLite schema and access. |
| `web` | Optional local web UI. |

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
* RAIDZ/dRAID and encrypted datasets are detected but not decoded.

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
