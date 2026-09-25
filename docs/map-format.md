# The SQLite map

The authoritative schema is `src/zfsrecover/map/schema.sql`, which is commented. The map
is an **index**: it never holds file contents. The one exception is small metadata values
(pool config, properties, history records). Useful queries:

```sql
-- datasets known to the pool, including destroyed ones
SELECT name, kind, status, origin, creation_txg, destroy_txg FROM datasets;

-- recovery summary per volume
SELECT name, n_blocks, n_verified, n_holes, n_stale, n_missing, n_damaged FROM volumes;

-- why is this range of a volume lost?
SELECT * FROM volume_coverage WHERE volume_id = 1 AND status NOT IN ('ok','hole','embedded');

-- files that are only partially recoverable
SELECT path, size, recoverable_bytes FROM fs_entries WHERE status = 'partial' ORDER BY size DESC;

-- where a file's data lives inside its volume
SELECT file_offset, length, volume_offset, kind, status FROM fs_extents
 WHERE entry_id = (SELECT id FROM fs_entries WHERE path = '/etc/fstab');
```

`volume_spans` rows hold packed binary data (level-1 block pointers plus per-slot choice
and status). Use `zfsrecover.volume.logical.LogicalVolume` rather than decoding them by
hand.
