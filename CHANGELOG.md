# Changelog

## 0.1.0 (unreleased)

* First version: pool/label/uberblock discovery; pool history decoding; DSL traversal
  across the whole uberblock ring; resumable raw-device carving; multi-generation zvol
  reconstruction; data verification with fallback to older generations; GPT/MBR; ext4
  inventory with virtual journal replay; extraction of volumes, partitions and files
  with gap reports; local web UI.
* `zesus send` and the web UI's "send" action: rsync recovered files back to a server, then
  restore original names (including ones Windows can't store), symlinks, modes, times and
  owners from the map.
