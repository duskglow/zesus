# CLAUDE.md

Guidance for Claude Code (and humans) working on **Zesus**, *ZFS Emergency Storage Undelete
Support*. Zesus is a forensic scanner and extractor that recovers destroyed ZFS volumes and
datasets, even after they have left the uberblock ring.

## Non-negotiable rules

1. **Never write to the evidence.**
   * All source access goes through `zesus.io.RawSource` / `SliceSource` (opened `O_RDONLY`).
   * `zesus.io.guard` installs an audit hook that raises `SourceWriteAttempt` on any write-mode
     open, truncate, rename or remove of a protected path.
   * Do not add write paths to `src/zesus/io/`. `tests/unit/test_guard.py` checks this
     statically.
   * Outputs (maps, extractions) must never resolve to the source.
2. **Never guess silently.**
   * Every recovered byte is checksum-verified against a parent block pointer.
   * Anything inferred or unverified is labelled: statuses `ok_stale`, carved provenance,
     `--include-unverified`.
   * Gaps are zero- or pattern-filled *and* reported (`*.gaps.json`, ddrescue mapfile,
     manifest).
3. **Privacy.** Test images may contain someone's personal files. Inspect structure
   (superblocks, directories, counts) freely, but do not print or read file *contents*
   unless the owner asks. Validate data integrity with self-checking formats (archive CRCs,
   checksums) instead.
4. **Parsers must be damage-tolerant.** Truncated, zeroed or random input must not crash a
   phase. Record a problem and continue.

## Layout

```
src/zesus/
  io/          read-only sources (SourceSet: one per member disk) + write guard
  zfs/         on-disk format: label/uberblock, nvlist (XDR + native), blkptr, checksum,
               compress, dnode, objset, zap, dsl, history, zpl (ZFS filesystems), pool, reader,
               vdev (disk/mirror/raidz candidate reads), raidz (column map), gf256 (parity), members
  carve/       scanner (resumable raw carving) · raidz_carve (member rows) · classify · reconstruct (multi-generation
               tree merge + orphan placement + root clustering) · verify · fastsum (numba)
  volume/      LogicalVolume (gap-aware device over a reconstructed zvol) · Coverage
  partitions/  GPT, MBR plugins
  fs/          plugin API (api.py), registry, ext4 plugin (+ virtual JBD2 replay), identifiers
  extract/     extraction engine (+ LocalNamer: collision-safe local names), sparse output,
               send.py (stage → rsync → generated POSIX restore script)
  scan/        pipeline (phase runner, resumable, downstream invalidation) · phases
  map/         schema.sql, db.py (WAL, in-place migrations), codes.py (block statuses)
  cli/         main (scan/info/report/web), extract_cmd (extract/ls), report, fullreport
  web/         FastAPI + single-file vanilla JS UI (localhost only): jobs with progress/cancel
  parallel.py  ordered pipelines, read-ahead, --workers/--io-depth · progress.py · combine.py (mirrors)
dev/           imgtool.py (inspect an image), make_ext4_fixtures.sh
tests/         unit (fixtures in tests/fixtures), integration (needs ZESUS_TEST_IMAGE)
docs/          how-to-use.md (user recipes), architecture.md, map-format.md, writing-plugins.md
```

Scan phases, in order: `history → datasets → carve → reconstruct → verify → contents`.
Re-running a phase resets every later phase except `carve`, which is additive and resumes
by chunk.

## Commands

```bash
pip install -e ".[dev,all]"
pytest -q                                      # unit tests; ~1 s
ZESUS_TEST_IMAGE=/path/pool.img pytest -q      # plus integration tests
ruff check src tests dev examples              # must be clean
python dev/imgtool.py --image IMG uberblocks   # also: labels, history, datasets, objset, zap, dva, bp, carve-sample
wsl -e sh dev/make_ext4_fixtures.sh            # rebuild ext4 test images (needs e2fsprogs)
```

For fast iteration on a big image, use a small slice:
`zesus scan IMG -o t.sqlite --phases history,datasets,reconstruct,verify,contents --limit-blocks 102400`.

## Conventions and gotchas

* **Map schema changes** need a migration in `MapDB._migrate()`. Rebuild tables with
  "create new → copy → drop → rename" and `legacy_alter_table=ON`. A plain `RENAME` rewrites
  other tables' foreign keys. `tests/unit/test_map_migration.py` covers this.
* **The map is an index, not a data store.** Volumes store L1 candidate pointers per span
  (`volume_spans`), not every L0 pointer. Derive L0 pointers through `LogicalVolume` or
  `carve.verify.chosen_blocks`.
* **Big volumes:** keep per-block work in numpy. A zvol can have 50M+ blocks, so avoid
  Python dicts or loops per block (see `Summary`, `ChildIndex`, and the coverage-driven gap
  loop in the extractor).
* **Spinning disks:** always read in physical order (`read_many`, `windows()`). Parallel
  work goes through `parallel.ordered_map`/`prefetch`: one sequential reader per source
  (`--io-depth`), CPU on workers, results applied in order (maps must equal a serial run).
* **RAIDZ:** never read DVA space linearly. Go through `TopVdev.candidates()` /
  `read_window()`; a rebuilt block counts only if its checksum matches.
* **Windows outputs:** create/extend files with `extract.sparse.set_size`, never
  `truncate()` (the CRT writes zeros). Write text with `newline="\n"` (repo is LF).
* **Carved roots:** associate them with a dataset by *shared tree blocks* (`cluster()`),
  never by size or geometry alone.
* **Windows:** pass `encoding="utf-8"` to every `read_text`/`write_text`, since the default
  is cp1252. Paths from the evidence may be un-decodable: use `surrogateescape`, and
  `safe_name()` when creating files.
* **Plugins** register via entry points (`zesus.filesystems`, `zesus.partitions`,
  `zesus.identifiers`). Core must not import plugin-specific code, except the built-ins
  in the registries.
* Commit messages: imperative subject, a body explaining *why*. Follow [AI_POLICY.md](AI_POLICY.md): state only what
  was actually done and tested, and keep the `Co-Authored-By` trailer on AI-assisted commits.

## Status and next steps

Validated end to end on a real 1 TB single-disk pool:
* A destroyed 880 GiB zvol (GPT + ext4) was rebuilt from 1,202 carved tree generations.
* Carved-only and ring+carved reconstructions matched slot for slot.
* Extracted files passed independent integrity checks.

Multi-disk (v2), validated so far:
* RAIDZ math: an independent reference, every erasure pattern (p = 1..3), and synthetic
  member disks (`tests/unit/test_raidz_*.py`).
* A real 4-wide RAIDZ1 with one member missing (kotori): the history and datasets phases
  found the destroyed zvol. 51k data blocks verified, 72% of them rebuilt from parity.
  900 MiB extracted, with its GPT CRCs intact.
* Single-disk maps and extractions are byte-identical to v1.
* Not yet: real OpenZFS RAIDZ2/3 and mirror pools. `tests/unit/test_zfs_fixtures.py`
  skips until `dev/make_zfs_fixtures.sh` has been run (needs ZFS; in WSL, run
  `sudo apt install zfs-dkms zfsutils-linux`).

Open items, roughly by value:
1. Carved-only reconstruction for destroyed ZFS **filesystem** datasets (the volume path in
   `phase_reconstruct` shows the approach: cluster carved `os_type=2` objsets and walk
   them with `ZplFilesystem`).
2. Run `dev/make_zfs_fixtures.sh` (real OpenZFS mirror/RAIDZ1-3 pools) and commit the
   fixtures, so RAIDZ2/3 are validated against OpenZFS in CI.
3. dRAID and expanded RAIDZ (reported as unsupported). RAIDZ1/2/3 work: see below.
4. More filesystem plugins: XFS and NTFS inventory (identification exists already).
5. Verification of zvol **snapshots** as their own volumes.
6. Encrypted datasets (with a user-supplied key).

Before the next release (web UI):
* Serve the page the backend was started with, and check page/API versions. A server
  started before an upgrade served the new page to an old API, which gave a blank Jobs
  page. On a mismatch, say "restart when idle" instead.
* Run web jobs as separate processes that publish progress to the map (as CLI scans do),
  and keep the job list in the map. A restart then neither kills nor forgets a job, and
  the page can reload itself when the backend comes back.
* Push job updates (server-sent events) instead of polling.
* Faster file extraction: files are read one block at a time through LogicalVolume.
  Batch each file's blocks, in physical order, through the same windowed path volume
  extraction uses.
