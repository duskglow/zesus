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
  blocks: 57671680  verified=48198765 (83.57%)  older-generation=19552  never-written=9440517 (16.37%)  lost=1024 (0.00%)
```

"never-written" is empty space in the volume, so nothing is lost there. What matters is
`lost`.

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
* **Disk space for outputs:** volume images are sparse (unwritten parts take no space), but
  budget for the used size of the volume.
* **Speed:** the scan reads the whole disk once to carve metadata and once to verify data. A
  spinning disk at 160 MB/s needs about 2 hours per TB for each pass.
* **Something odd?** Every run writes `case.log.jsonl` next to the map. Please attach it,
  and the output of `zesus info`, to bug reports. Never attach disk images or maps from real
  cases publicly: they contain your file names.
