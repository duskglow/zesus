# Writing a filesystem plugin

Plugins live in their own Python package and are found through entry points. Core code
does not change. See `examples/plugin-template/` for a complete, installable skeleton.

## Contract

```python
from zfsrecover.fs.api import FilesystemPlugin, FsHandle, FsInfo, Entry, Extent

class XfsPlugin(FilesystemPlugin):
    name = "xfs"
    description = "SGI XFS"

    def probe(self, dev) -> float:           # 0..1 confidence
        return 1.0 if dev.pread(0, 4) == b"XFSB" else 0.0

    def open(self, dev) -> FsHandle:
        return XfsHandle(dev)

class XfsHandle(FsHandle):
    def info(self) -> FsInfo: ...
    def iter_entries(self): ...              # yield Entry for every file/dir/link
    def extents(self, entry): ...            # yield Extent(file_offset, length, dev_offset, kind)
    def problems(self): ...                  # [(where, what)] for unreadable metadata
```

```toml
[project.entry-points."zfsrecover.filesystems"]
xfs = "zfsrecover_xfs:XfsPlugin"
```

## Rules

* **Read only through `dev`.** `dev.read(off, n)` returns `(bytes, gaps)`. Gaps are ranges
  that could not be recovered and read as zeros. Check them when parsing metadata, and
  report damaged structures through `problems()` instead of crashing.
* **Never raise on bad data** from `iter_entries`. Skip the damaged part, record a
  problem, and continue with the rest.
* **Describe, don't copy.** `extents()` maps file offsets to device offsets. The core
  computes each file's recovery status from the volume coverage, and the extractor
  copies the bytes. Use `kind="inline"` with `inline=bytes` for data stored inside
  metadata (resident NTFS attributes, ext4 inline data), `"sparse"` for holes, and
  `"unwritten"` for preallocated space.
* Deleted or orphaned entries are welcome. Set `deleted=True` and use a path under
  `/$deleted/` or `/$orphans/`.

## Signature-only identifiers

If full inventory is out of scope, an `Identifier` (entry-point group
`zfsrecover.identifiers`) still lets users see and extract the filesystem as an image.
See `zfsrecover/fs/identify/__init__.py`.

## Testing

Build a small real image with the filesystem's own mkfs tools (see
`dev/make_ext4_fixtures.sh`), gzip it into `tests/fixtures/`, and assert on paths,
contents, sparse regions and deleted files. `tests/conftest.py` provides `MemDevice`,
which can also inject gaps.
