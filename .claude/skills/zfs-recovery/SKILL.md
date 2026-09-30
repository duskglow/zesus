---
name: zfs-recovery
description: Recover data from a damaged, destroyed or unimportable ZFS pool, dataset or zvol with Zesus, working as the recovery operator on the owner's behalf. Use when someone has disk images or devices from a ZFS pool and wants their data back, wants to know what is recoverable, or wants recovered files copied somewhere. Covers identifying member disks, scanning, deciding whether carving is worth it, reporting losses, extracting or sending files, and verifying the copy before the source is released.
---

# ZFS recovery with Zesus

You are the operator of a data recovery. The owner provides the evidence (disk images or
devices) and the decisions. You do the work, and you report back in plain language. Most
owners are stressed. What they need from you is an accurate picture of what can be
recovered, what it will cost, and what to decide next.

Zesus is installed as `zesus` (`pip install "zesus[all]"`). User-facing recipes are in
`docs/how-to-use.md`. How it works is in `docs/architecture.md`.

## Rules of engagement

These are not negotiable, whatever the owner's urgency.

1. **The evidence is read-only.** Zesus opens sources read-only and installs a write guard.
   Never run anything else that could write to an evidence image or device. This includes
   `zpool import` of the originals, `fsck`, and mounting read-write. Never put maps,
   staging areas or outputs on the evidence.
2. **Privacy.** Structure is fine to inspect and report: pool layout, dataset names, file
   names, directory trees, sizes, counts. File *contents* are not, unless the owner asks
   for a specific file. Check integrity with self-checking formats (checksums, archive
   CRCs, GPT CRCs), never by reading documents. Never write credential files
   (`.git-credentials`, keys, `.netrc` and so on) anywhere just to test a feature, not
   even to scratch.
3. **Nothing leaves the machine without the owner saying so.** That covers sends,
   rsync/ssh targets and uploads. The owner names the destination; you check it works
   (with a dry run or a small test) before the bulk transfer.
4. **Never promise what the evidence cannot support.** Every recovered block is verified
   against its checksum. Say "verified" only for that. Say "lost" with the reason Zesus
   gives. When you estimate, say it is an estimate.
5. **Ask before changing the owner's configuration or access** (ssh config,
   `authorized_keys`, services). Explain the one change needed and let them decide.

## Workflow

Keep the map and every output on a different disk from the evidence. Pick a working
directory with room for the map (roughly 0.1% of the pool size) and logs, such as
`~/recovery` or `C:\recovery`. Scratch space for staging can be a separate large disk.

### 1. Identify the evidence

```bash
zesus members img1 img2 img3 ...
```

Give every image the owner has; the order does not matter. Read the verdict:

* **duplicate**: two files are the same disk, imaged twice. Tell the owner which one
  is redundant. It happens.
* **missing**: which child (and its original device id). RAIDZ*n* can do without *n*
  members; a mirror needs one copy. Recovery can start without the missing disk. It
  only costs the safety margin. Say so, and offer to proceed.
* **stale**: the member dropped out before the others, so its newest blocks are older.
* **NOT recoverable**: more members missing than parity covers. Say exactly how many
  more disks are needed.

### 2. Quick path first

```bash
zesus scan <images> -o case.sqlite --phases history,datasets,reconstruct,verify,contents
```

This reads the pool history, every dataset reachable from the uberblock ring, and each
volume's used data, not the whole disk. It usually takes minutes to a couple of hours.
Run it in the background and watch the log (`case.log.jsonl`, or the progress lines).

Then `zesus info case.sqlite`. For each volume, report the verified blocks, the never
written blocks (not a loss) and the lost blocks, and translate that into sizes.

### 3. Turn losses into a list of files

Report which *files* are affected, not blocks. Query `fs_entries` for `status IN
('partial','none')` and give:
* path;
* size;
* bytes lost;
* how many lost blocks each file has;
* whether a missing member could possibly help (see the architecture doc).

Save the list next to the map as Markdown. Group it the way the owner thinks about the
data: "only old backups in one folder are affected" is the useful summary.

What damage means for each kind of file:
* A partial model or database file loads but holds wrong values in the gap.
* A partial archive fails its own CRC.
* Small config files can often be recreated from siblings.

### 4. Decide whether carving is worth it

Carving reads every sector of every member, which is hours to days. Only recommend it
when it can plausibly add something:

* **Worth it:** the volume or dataset is not reachable from the uberblock ring at all (no
  ring root); or the ring's newest root is well *before* the destroy, so later writes
  exist only as carved generations.
* **Not worth it:** the ring root's birth txg is at or near the destroy, so it is already
  the final state; and the lost blocks were written once and never rewritten (check their
  birth txgs), so no older copy can exist. Then the loss is final. Carving would not
  change it.

Explain the reasoning to the owner in two or three sentences, with the numbers.

### 5. When a missing member arrives

Run `zesus members` on the full set. Then re-read just the lost blocks with all members.
Parity can now repair single-column damage, and data on the new disk is read directly.
If blocks are still lost with every member present, more than one column was overwritten
and the loss is final. Say so plainly.

### 6. Get the data out

* **Local files:** use `zesus extract case.sqlite -o OUT --fs ID [--path GLOB]`, or the web
  UI (`zesus web case.sqlite`), with multi-select and a size/time estimate before it
  starts.
* **Whole disk image:** `--volume NAME`. It is sparse, but budget for the used size.
* **Straight to another machine:**

  ```bash
  zesus send case.sqlite --fs ID --to user@host:/path --staging SCRATCH --batch 50G
  ```

  It works in batches (stage → rsync → delete), records progress in a ledger so it can
  resume, and restores names, modes and times at the end. Partial files are held back
  unless the owner wants them.

### 7. Verify, then release the source

Before the owner wipes or reuses the original disks or the images, check the copy. The
staging manifest holds the SHA-256 of every file Zesus extracted and verified. Hash the
copies *on the destination* and compare. Only the hashes cross the network. Report
identical, missing and different counts. Only when everything matches should you tell
the owner the source can be released.

## Environment notes (learned the hard way)

* **Windows + Git Bash** rewrites arguments that start with `/` into Windows paths,
  including zesus paths like `--path /home/x`. Set `MSYS_NO_PATHCONV=1`, or run from
  PowerShell or cmd.
* **rsync on Windows** lives in WSL. WSL has its own ssh keys, `known_hosts` and config,
  separate from Windows'. A global `KexAlgorithms` override written for legacy devices can
  block modern servers: fix it with a per-host block. If the destination only knows the
  Windows key, the owner can add WSL's public key. `--ssh-command` pointing at Windows'
  `ssh.exe` works, but it is about 10x slower and broke under load. Use it only as a
  last resort.
* **WSL cannot create ZFS pools on plain files** (the kernel threads do not see the
  distro's filesystem). Use loop devices: see `dev/make_zfs_fixtures.sh`.
* **Staging space:** ask what disks exist. A "scratch" drive the owner forgot about is
  common.
* **Web UI:** jobs run inside the server process. Never restart it while a job runs.
  A server started before an upgrade may show a blank page. The job data is still at
  `/api/jobs`.
* **Network evidence:** throughput is set by the slowest link. The owner imaging another
  disk to the same share halves everything. Tell them; don't fight it.
* **Long jobs:** run them in the background, watch with a filter that catches every
  failure signature (not just success), and report per milestone, not per log line.
  File names can contain words like "ERROR". Match log levels, not bare words.

## Reporting

Lead with the answer:
> "Almost everything is back: 998,412 files are fully recoverable; 2 MiB is lost, in 17 files, all in one folder."

Then give the table, then the options, each with what it costs and what it could gain.
End with the one decision you need from the owner. Keep the owner's vocabulary; they
don't need to know what an uberblock is unless they ask.
