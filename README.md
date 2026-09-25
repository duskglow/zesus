# zfs-forensic-recovery

Forensic scanner and extractor for ZFS pools. It can recover **destroyed datasets and
zvols** whose blocks are still present in unallocated space, even after they have fallen
off the uberblock ring (where `zpool import -T` can no longer reach them).

> **Status:** early development (alpha). The on-disk map format may still change.

## What it does

* **`zfsrecover-scan`** reads a raw disk image or a block device, **strictly read-only**, and
  writes a SQLite *map* of everything it finds:
  * pools and vdevs;
  * datasets, snapshots and zvols, both live and historical (from every uberblock);
  * datasets named in the pool history log;
  * blocks carved from free space;
  * partitions and filesystems inside zvols;
  * per-file recovery status.
* **`zfsrecover-extract`** uses the map as an index into the original image. It extracts
  zvols, partitions, filesystem images or individual files. It re-verifies every block
  checksum as it reads, and it never silently fills gaps. Every gap is reported, with the
  reason it could not be recovered.

The map holds locations, sizes and checksums, not data. You need both the map and the
source image when you extract.

## Read-only guarantee

* The source is opened `O_RDONLY` through a class that has no write methods.
* A process-wide audit hook (PEP 578) aborts any attempt, from anywhere in the process,
  to open the source for writing, or to truncate, rename or delete it.
* The source's size and mtime are checked again at the end of every run.

## Install (development)

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,all]"
```

## License

Apache-2.0. See [LICENSE](LICENSE).
