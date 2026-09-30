# How to get your data back with Zesus

Short recipes for the common disasters. Each one is a few commands. The
[README](../README.md) and [architecture notes](architecture.md) explain the details, if you
want them.

---

## First: stop the damage (read this before anything else)

Destroyed data stays on disk only until ZFS reuses the space. **Every write to the pool
can overwrite what you want back.**

1. **Stop writing to the pool.** Shut down the VMs, containers and services that use it.
   If you can, export it:

   ```bash
   zpool export tank
   ```

   Do **not** run `zpool scrub`, `zpool trim`, or any "cleanup".
2. **Do not import the pool read-write again** until you have what you need.
3. **Turn off automatic TRIM** if it is on. TRIM tells SSDs to erase freed blocks.
   * Check it with `zpool get autotrim tank`.
   * The next import may run TRIM before you can stop it, so prefer working on an image.
4. **Make an image of the disk** if you have the space. It is the safest option, because
   everything below can then run against the image instead of the disk:

   ```bash
   ddrescue /dev/sdX /mnt/bigdisk/disk.img /mnt/bigdisk/disk.map
   ```

   Keep the image on a **different** disk from the one you are recovering.

Zesus itself never writes to the source. It opens it read-only and blocks any write attempt
in code. But it cannot stop *other* programs or the operating system from writing to it.

---

## Install

```bash
pip install "zesus[all]"
```

Python 3.10 or newer on Linux, macOS or Windows. Nothing needs compiling. You can also run it
from Windows against an image file.

---

## Recipe 1: "I destroyed a zvol" (a VM disk, for example)

This is the case Zesus was built for, and it works even when `zpool import -T` no longer
helps.

```bash
# 1. Scan: reads the whole disk once or twice. Hours for a large disk. Safe to interrupt;
#    re-run the same command to continue where it stopped.
zesus scan /mnt/bigdisk/disk.img -o case.sqlite

# 2. What did it find?
zesus info case.sqlite
```

Look for your volume in the output. The line under it says how much is recoverable:

```
Volume tank/vm-100-disk-0: volsize=880.0 GiB volblocksize=16384 status=verified
  blocks: 57671680  verified=48229181 (83.63%)  older-generation=22  never-written=9440517 (16.37%)  lost=1960 (0.00%)
```

"never-written" is empty space in the volume, so nothing is lost there. What matters is
`lost`. (That example is a real 880 GiB VM disk, recovered after it had fallen off the uberblock
ring. It lost 31 MiB, almost all of it in re-downloadable caches.)

```bash
# 3. Get the whole disk back as an image file.
#    Put it on a disk with enough free space, not the one you are recovering.
zesus extract case.sqlite /mnt/bigdisk/disk.img -o /mnt/other/recovered --volume tank/vm-100-disk-0
```

You get:

* `recovered/tank_vm-100-disk-0.img`: the volume. Unrecoverable blocks are zeros.
* `...img.gaps.json` and `...img.mapfile`: exactly which byte ranges are missing, and why.
* `manifest.json`: a summary with a SHA-256 of the output.

**Using the recovered disk image**

* Proxmox: `qm importdisk <vmid> recovered/....img <storage>`, then attach it to the VM.
* Plain Linux: look at it read-only first:

  ```bash
  sudo losetup -P -r -f --show recovered/tank_vm-100-disk-0.img      # e.g. /dev/loop0
  sudo mount -o ro,noload /dev/loop0p1 /mnt/look   # 'noload' = don't replay the ext4 journal
  ```

* Back into ZFS:

  ```bash
  zfs create -V 880G tank/restored
  dd if=recovered/....img of=/dev/zvol/tank/restored bs=1M conv=sparse
  ```

If the volume held a filesystem that was mounted when it was destroyed, run
`fsck`/`e2fsck` on a **copy** of the image before booting from it.

---

## Recipe 2: "I only need some files out of it"

You don't need the whole disk image. Zesus inventories ext4 filesystems inside volumes, and
ZFS filesystem datasets directly.

```bash
zesus extract case.sqlite disk.img --list          # shows volumes, partitions and filesystem IDs
zesus ls case.sqlite / --fs 1 -l                   # browse; '!' = unrecoverable, '~' = partial
zesus ls case.sqlite /home/alice --fs 1 -l

zesus extract case.sqlite disk.img -o out --fs 1 --path /home/alice          # a directory tree
zesus extract case.sqlite disk.img -o out --fs 1 --path '/var/lib/mysql/*'   # a glob
zesus extract case.sqlite disk.img -o out --fs 1 --status full               # only intact files
```

Files keep their timestamps. A partially recoverable file is still written, with zeros in the
missing parts. It gets a `<file>.gaps.json` listing those parts. Add `--partial-suffix .PARTIAL`
if you want such files renamed so they can't be mistaken for good ones.

Prefer clicking? Use the web UI:

```bash
zesus web case.sqlite --source disk.img      # then open http://127.0.0.1:8765/
```

---

## Recipe 2b: "Put the files straight back on the server"

Skip copying things around by hand. `zesus send` stages the recovered files locally (verified
as usual), rsyncs them to the destination, and then restores their original names, symlinks,
permissions and timestamps. With `--sudo` it restores owners too.

```bash
# see what would happen first
zesus send case.sqlite disk.img --fs 1 --path /home/alice --to root@newserver:/ --dry-run

# then do it (owners need root on the destination: --sudo, with passwordless sudo there)
zesus send case.sqlite disk.img --fs 1 --path /home/alice --to root@newserver:/ --sudo
```

* It never overwrites a file that already exists at the destination unless you add
  `--overwrite`.
* Partially recovered files stay behind unless you add `--include-partial`.
* Unrecoverable files are never sent.
* It is safe to run again: already-staged and already-sent files are skipped.
* In the web UI: **Files → "send"** on any row, or **"Send this folder…"**. Use the
  *Preview* button for a dry run.
* Windows needs rsync and ssh inside WSL (`sudo apt install rsync openssh-client`). Your ssh
  keys must be set up there too.
* Staging needs local disk space for the files being sent. Choose where with `--staging`.

---

## Recipe 3: "I destroyed a ZFS filesystem dataset"

Same scan. The dataset appears in `zesus info` as `destroyed`. If it is still reachable, its
files appear as a filesystem with plugin `zpl` in `--list`:

```bash
zesus scan disk.img -o case.sqlite
zesus extract case.sqlite disk.img --list
zesus extract case.sqlite disk.img -o out --fs 3
```

Add `--snapshots` to the scan if you also want the contents of every snapshot inventoried.

---

## Recipe 4: "What exactly can and can't I get back?"

```bash
zesus report case.sqlite -o case-report.md       # also writes case-report.csv (one row per file)
```

The report lists:

* every dataset found (including destroyed ones) and when it was created and destroyed;
* per volume, how much is recovered, never written, or lost, and why;
* partially recoverable files with the exact missing byte ranges;
* files that cannot be recovered at all.

Reasons you may see:

| Reason | What it means |
|---|---|
| overwritten (checksum mismatch) | ZFS reused that space after the destroy. The data is gone. |
| trimmed / zeroed | The SSD was told to discard it (TRIM). The data is gone. |
| no surviving metadata | The data might still be on disk, but nothing left says where it belongs. |
| recovered from an older version | The newest copy was overwritten. You get the previous version of that block. |

---

## Recipe 4b: "My pool had several disks" (mirror or RAIDZ)

Image every member disk, one image file per disk. Then give Zesus all of them, in any
order:

```bash
# Which image is which disk, and can the pool still be read?
zesus members sda.img sdb.img sdc.img sdd.img
```

```
pool tank (guid 6012...), newest txg 48213
  vdev 0: raidz1 width 4, ashift 12: DEGRADED but recoverable: 1 of 4 members missing, rebuilt from parity (no redundancy left)
    child  0  present   sda.img   ...
    child  0  duplicate sdd.img   ...  (same member as sda.img: identical at 12 sampled regions)
    child  1  missing   -         ...
    child  2  present   sdc.img   ...
    child  3  present   sdb.img   ...
```

Zesus matches each image to its place in the vdev using the label on the image itself, so
file names and order do not matter. It tells you:

* when a disk is missing;
* when two images are the same disk (as above: imaged twice);
* when a member is **stale**: it dropped out of the pool earlier, so its newest blocks are
  older than the others'.

With RAIDZ*n*, up to *n* members can be missing. Their data is rebuilt from parity. A mirror
needs just one copy.

Then scan and extract as in Recipe 1, naming every image:

```bash
zesus scan sda.img sdb.img sdc.img -o case.sqlite
zesus extract case.sqlite -o /mnt/other/recovered --volume tank/vm-100-disk-0
```

`extract`, `send` and `web` reuse the member images recorded in the map when you do not
name them. They check that each file still has the size the scan saw.

Every block rebuilt from parity is accepted **only if it matches its block pointer's
checksum**, exactly like a direct read. If a disk is missing and a block cannot be
rebuilt, you get a reported gap, never guessed data. If all disks are present but one
column is silently damaged, Zesus finds which column is wrong the way ZFS does, by trying
each and keeping the one whose checksum matches.

About strategies:

* RAIDZ is always read **in place**, from the member images. There is no meaningful
  "combined" image of a RAIDZ vdev: each block is laid out across the disks in its own
  way, so a missing disk can only be rebuilt one known block at a time.
* A mirror needs nothing special. Zesus reads whichever copy verifies, trying the copy
  with the fewest checksum failures first.

Imaged the missing disk later? Run `zesus members` on the full set, then scan again with
every image. Carving adds what the new disk shows; blocks already found are not
duplicated.

Speed: members are read in parallel, one sequential stream per disk, so a 4-disk RAIDZ
scans in about the time of one disk when the storage can deliver it. The defaults are
safe for spinning disks and network shares. `--workers N` sets how many CPU threads (and
carving processes) to use. `--io-depth` raises the number of reads in flight, which only
helps on SSD/NVMe.

---

## Recipe 5: "I want to check first, cheaply"

Most of the value comes from the fast phases. To see what existed and what was destroyed,
in minutes instead of hours:

```bash
zesus scan disk.img -o case.sqlite --phases history,datasets
zesus info case.sqlite
```

This reads the pool's own history log (`zpool history`), even when the pool will not import.

---

## Working with a live disk instead of an image

It works (`zesus scan /dev/sdb ...`, or `\\.\PhysicalDrive2` on Windows as Administrator), and
Zesus only reads. But an image is still better:

* you can re-run things as often as you like;
* nothing else on the machine can write to it.

On Linux, `sudo blockdev --setro /dev/sdb` is a good extra safety step.

---

## Tips

* **Interrupted?** Re-run the exact same `scan` command. Finished work is not repeated.
  In the web UI (`zesus web case.sqlite`), every scan, extraction and send shows a progress
  bar with rate and time remaining, and has a Cancel button.
* **Many files?** In the web UI, tick files and folders (a folder includes everything under
  it), then "Extract…". Before anything is written, it shows the total size, what cannot
  be recovered, the free space on the output drive and, once a job has measured the
  throughput, an estimated time.
* **Disk space for outputs:** volume images are sparse (unwritten parts take no space), but
  budget for the used size of the volume.
* **Speed:** the scan reads the whole disk once to carve metadata and once to verify data. A
  spinning disk at 160 MB/s needs about 2 hours per TB for each pass.
* **Something odd?** Every run writes `case.log.jsonl` next to the map. Please attach it,
  and the output of `zesus info`, to bug reports. Never attach disk images or maps from real
  cases publicly: they contain your file names.
