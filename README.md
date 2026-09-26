# Zesus

**ZFS Emergency Storage Undelete Support**: it resurrects dead ZFS volumes and datasets.

Zesus is a forensic scanner and extractor for ZFS pools. It can recover **destroyed datasets and
zvols**, including ones that have fallen off the uberblock ring where
`zpool import -T <txg>` can no longer reach them.

> **Status: alpha.** It has been validated on real pools (single-disk vdevs, OpenZFS 2.x).
> The map format may still change before 1.0.

## How it works

ZFS is copy-on-write and its block pointers are self-describing: each one holds the child's
physical location, size, type, indirection level, birth txg and **checksum**. When a dataset
is destroyed, its blocks are freed but not erased. The disk also still holds many older
*generations* of every metadata block. This tool:

1. reads the **pool history log** to learn what existed and when it was destroyed;
2. walks the dataset tree from **every uberblock** still in the label rings;
3. **carves** the whole device for ZFS metadata: objsets, dnode blocks, indirect blocks, ZAPs;
4. **reconstructs** each volume by merging every surviving generation of its block tree,
   placing orphaned indirect blocks by matching their children;
5. **verifies** every data block against its checksum, falling back to older generations when
   the newest copy was overwritten;
6. looks **inside** each volume for partition tables and filesystems, and inventories ZFS
   filesystem datasets directly. It builds a per-file inventory with recovery status
   (`full` / `partial` / `none`).

Everything goes into a SQLite **map**. The map is an index of locations, sizes, checksums
and statuses, never data. The extractor uses the map plus the original image to write out
volumes, partitions or individual files, re-verifying every block as it goes.

## Safety

* **The source is never written.**
  * It is opened `O_RDONLY` through a class with no write methods.
  * A process-wide audit hook (PEP 578) blocks any attempt, from anywhere in the process,
    to open it for writing, or to truncate, rename or delete it.
  * Its size and mtime are re-checked at the end of every run.
  * For block devices, consider `blockdev --setro /dev/sdX` as well.
* **Nothing is silently guessed.**
  * Unrecoverable ranges are zero-filled (or filled with a visible marker with
    `--fill pattern`).
  * Every such range is listed in a `*.gaps.json` report, a GNU ddrescue-compatible
    `*.mapfile`, and `manifest.json`, with the reason: overwritten, trimmed, no surviving
    metadata, and so on.
  * Data from an older generation is labelled `ok_stale`.

## Install

```bash
pip install "zesus[all]"      # or, from a checkout: pip install -e ".[all]"
```

Python 3.10+. Required dependencies are numpy and lz4. The optional extras are:
`zstandard` for zstd pools, `blake3`, `numba` for faster checksums, and `fastapi`,
`uvicorn` and `jinja2` for the web UI. No compiler is needed.

## Quick start

```bash
# 1. Scan (resumable: re-run the same command after an interruption)
zesus scan /dev/sdb -o case.sqlite            # or a raw image: disk.img

# 2. See what was found
zesus info case.sqlite
zesus extract case.sqlite /dev/sdb --list
zesus ls case.sqlite / -l --fs 1

# 3. Extract
zesus extract case.sqlite /dev/sdb -o out/ --volume 1              # whole zvol as out/<name>.img
zesus extract case.sqlite /dev/sdb -o out/ --partition 1:1         # one partition of it
zesus extract case.sqlite /dev/sdb -o out/ --fs 1 --path '/home/*' # selected files

# 3b. Or send recovered files straight back where they belong (rsync; restores names,
#     permissions, times and, with --sudo, owners)
zesus send case.sqlite /dev/sdb --fs 1 --path /home --to root@server:/ --sudo --dry-run

# 4. Write a recovery report: what was found, what is lost and why (Markdown + per-file CSV)
zesus report case.sqlite -o case-report.md

# 5. Or browse in a local web UI
zesus web case.sqlite --source /dev/sdb       # http://127.0.0.1:8765/
```

The scan phases are `history`, `datasets`, `carve`, `reconstruct`, `verify` and `contents`.
Run a subset with `--phases`, and redo one with `--redo carve`. The carve and verify phases
read the whole device, at roughly the device's sequential speed. Both checkpoint as they go.

Keep the map and the outputs on a **different disk** from the evidence.

## What is supported

| Area | Status |
|---|---|
| Vdevs | single disk / file, mirror. RAIDZ/dRAID: detected, reported as unsupported |
| Features | lz4, gzip, zle, lzjb, zstd; fletcher2/4, sha256, sha512, blake3; embedded data; gang blocks; large dnodes; hole_birth |
| Encryption | detected and reported; decryption not implemented |
| Partition tables | GPT (with backup-header fallback), MBR with logical partitions |
| ZFS filesystem datasets | file inventory and extraction for live, historical and destroyed-but-reachable datasets (snapshots with `--snapshots`); SA and legacy znodes, unlinked set, orphans |
| Filesystems in zvols (inventory) | ext2/3/4, including virtual journal replay, deleted-inode and orphan discovery |
| Filesystems (identify only) | XFS, NTFS, exFAT, FAT, btrfs, LUKS, BitLocker, LVM2, swap, ISO9660, F2FS, HFS+, APFS, md-raid, ZFS |

New filesystems are plugins and need no change to core code. See
[docs/writing-plugins.md](docs/writing-plugins.md) and `examples/plugin-template/`.

## Documentation

* [docs/how-to-use.md](docs/how-to-use.md): **start here if you just want your data back**
* [docs/architecture.md](docs/architecture.md): phases, reconstruction algorithm, block statuses
* [docs/map-format.md](docs/map-format.md): the SQLite map and useful queries
* [docs/writing-plugins.md](docs/writing-plugins.md): adding filesystem support
* [CONTRIBUTING.md](CONTRIBUTING.md) and [AI_POLICY.md](AI_POLICY.md): this project was written with Claude Code; AI-assisted contributions are welcome under the same standards

## Development

```bash
pip install -e ".[dev,all]"
pytest                                  # unit tests (small real ext4 images in tests/fixtures)
ZESUS_TEST_IMAGE=/path/to/pool.img pytest -m image   # integration tests against a real pool image
python dev/imgtool.py --image disk.img uberblocks  # low-level inspection helpers
```

## License

Apache-2.0. See [LICENSE](LICENSE). This project reimplements the documented ZFS on-disk
format. It contains no OpenZFS code.
