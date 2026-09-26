# Changelog

## 0.2.0 (unreleased)

* Multi-disk pools: one image per member disk; mirror and RAIDZ1/2/3. This covers missing
  members (up to the parity level), silent single-column damage (combinatorial
  reconstruction), and carving of RAIDZ member disks. `zesus members` identifies the
  images. `--strategy combined` / `zesus combine` builds one image from a mirror's
  healthiest copies.
* Parallelism: pipelined verify/extract, multi-process carving, `--workers` and
  `--io-depth`. Results are identical to a serial run.
* Progress with rate and ETA for scan, verify and extract. Cancelling keeps scans
  resumable and labels cancelled outputs.
* Web UI: run scans, live progress with cancel, multi-select extraction, pre-extraction
  estimates, member health.
* Fix: on Windows, large output images were created by writing zeros (Python's
  `truncate()`). They are now created sparse, instantly.

## 0.1.0 (unreleased)

* First version: pool/label/uberblock discovery; pool history decoding; DSL traversal
  across the whole uberblock ring; resumable raw-device carving; multi-generation zvol
  reconstruction; data verification with fallback to older generations; GPT/MBR; ext4
  inventory with virtual journal replay; extraction of volumes, partitions and files
  with gap reports; local web UI.
* `zesus send` and the web UI's "send" action: rsync recovered files back to a server, then
  restore original names (including ones Windows can't store), symlinks, modes, times and
  owners from the map.
