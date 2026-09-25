-- zfs-forensic-recovery map, schema version 1.
-- The map is an INDEX into the source image. It holds locations, sizes, checksums and
-- interpretations, never file contents.
-- Offsets named *_phys are absolute byte offsets in the source. Offsets named dva_*
-- are ZFS DVA offsets (relative to the vdev's allocatable area, 4 MiB past the vdev start).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER,
    is_device   INTEGER NOT NULL,
    sha256      TEXT,
    opened_at   TEXT NOT NULL
);

-- One row per unit of work. The scanner marks rows done inside the same transaction as
-- that unit's results, so an interrupted scan resumes exactly where it stopped.
CREATE TABLE IF NOT EXISTS progress (
    phase       TEXT NOT NULL,
    unit        TEXT NOT NULL,           -- e.g. chunk start offset, or '*' for the whole phase
    state       TEXT NOT NULL,           -- 'done' | 'failed'
    detail      TEXT,
    finished_at TEXT,
    PRIMARY KEY (phase, unit)
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,
    component TEXT NOT NULL,
    message   TEXT NOT NULL,
    context   TEXT                        -- JSON
);

-- ---------------------------------------------------------------- pool structure
CREATE TABLE IF NOT EXISTS pools (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    guid        TEXT NOT NULL UNIQUE,     -- uint64 as decimal text
    state       TEXT,
    version     INTEGER,
    hostname    TEXT,
    txg         INTEGER,
    ashift      INTEGER,
    config_json TEXT
);

CREATE TABLE IF NOT EXISTS vdevs (
    id           INTEGER PRIMARY KEY,
    pool_id      INTEGER NOT NULL REFERENCES pools(id),
    top_id       INTEGER NOT NULL,
    guid         TEXT NOT NULL,
    type         TEXT,
    path         TEXT,
    base_phys    INTEGER,                 -- offset of vdev start within the source (NULL = absent)
    size         INTEGER,
    asize        INTEGER,
    ashift       INTEGER,
    present      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id        INTEGER PRIMARY KEY,
    vdev_id   INTEGER NOT NULL REFERENCES vdevs(id),
    idx       INTEGER NOT NULL,
    phys      INTEGER NOT NULL,
    config_ok INTEGER NOT NULL,
    n_uberblocks INTEGER NOT NULL,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS uberblocks (
    id          INTEGER PRIMARY KEY,
    pool_id     INTEGER NOT NULL REFERENCES pools(id),
    txg         INTEGER NOT NULL,
    timestamp   INTEGER,
    guid_sum    TEXT,
    checksum_ok INTEGER NOT NULL,
    rootbp      BLOB NOT NULL,            -- raw 128-byte blkptr
    phys        INTEGER,                  -- where the uberblock itself lives
    mos_ok      INTEGER,                  -- could the MOS be read through this uberblock?
    UNIQUE (pool_id, txg)
);

-- ---------------------------------------------------------------- datasets
-- One row per dataset *version* we know about. 'origin' says how it was found:
--   live        reachable from the newest uberblock
--   historical  reachable only from an older uberblock in the ring
--   history_log named in the pool history log only (e.g. destroyed)
--   carved      reconstructed from carved blocks
CREATE TABLE IF NOT EXISTS datasets (
    id             INTEGER PRIMARY KEY,
    pool_id        INTEGER NOT NULL REFERENCES pools(id),
    dsobj          INTEGER,
    name           TEXT,
    kind           TEXT,                  -- filesystem | volume | snapshot | unknown
    origin         TEXT NOT NULL,
    status         TEXT NOT NULL,         -- present | destroyed | unknown
    guid           TEXT,
    creation_txg   INTEGER,
    creation_time  INTEGER,
    destroy_txg    INTEGER,
    destroy_time   INTEGER,
    seen_txg       INTEGER,               -- txg of the uberblock / root it was read from
    objset_bp      BLOB,                  -- raw blkptr of the objset root, if known
    props_json     TEXT,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS datasets_dsobj ON datasets(pool_id, dsobj);

-- Every distinct objset root pointer seen for a dataset (one per ring uberblock that
-- could still read it). Reconstruction merges all of them.
CREATE TABLE IF NOT EXISTS dataset_roots (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id),
    seen_txg    INTEGER NOT NULL,          -- uberblock txg it was read through
    bp_birth    INTEGER NOT NULL,
    objset_bp   BLOB NOT NULL,
    PRIMARY KEY (dataset_id, bp_birth)
);

CREATE TABLE IF NOT EXISTS history_records (
    id        INTEGER PRIMARY KEY,
    pool_id   INTEGER NOT NULL REFERENCES pools(id),
    log_offset INTEGER NOT NULL,
    time      INTEGER,
    txg       INTEGER,
    kind      TEXT,                       -- command | internal | ioctl
    dsname    TEXT,
    dsobj     INTEGER,
    summary   TEXT,
    fields_json TEXT
);

-- ---------------------------------------------------------------- carving
-- Candidate metadata blocks found by scanning the raw device.
CREATE TABLE IF NOT EXISTS carved (
    id          INTEGER PRIMARY KEY,
    pool_id     INTEGER NOT NULL REFERENCES pools(id),
    vdev_top    INTEGER NOT NULL,
    dva_offset  INTEGER NOT NULL,          -- DVA-space offset
    phys        INTEGER NOT NULL,          -- absolute source offset
    kind        TEXT NOT NULL,             -- objset | indirect | dnodes | zap | ...
    comp        INTEGER NOT NULL,          -- compression seen (15=lz4, 2=off)
    psize_hint  INTEGER NOT NULL,          -- bytes consumed on disk (from lz4 header), rounded
    lsize       INTEGER NOT NULL,
    child_type  INTEGER,                   -- indirect: dominant DMU type of children
    child_level INTEGER,                   -- indirect: level of children
    n_children  INTEGER,
    n_holes     INTEGER,
    min_birth   INTEGER,
    max_birth   INTEGER,
    os_type     INTEGER,                   -- objset: objset type
    info_json   TEXT
);
CREATE INDEX IF NOT EXISTS carved_kind ON carved(kind, child_type, child_level);
CREATE INDEX IF NOT EXISTS carved_dva ON carved(vdev_top, dva_offset);

-- ---------------------------------------------------------------- objects & volumes
CREATE TABLE IF NOT EXISTS volumes (
    id            INTEGER PRIMARY KEY,
    dataset_id    INTEGER REFERENCES datasets(id),
    name          TEXT,
    volsize       INTEGER,
    volblocksize  INTEGER,
    nlevels       INTEGER,
    root_txg      INTEGER,                 -- birth txg of the objset version used
    n_blocks      INTEGER,                 -- logical blocks (volsize / volblocksize)
    n_verified    INTEGER,
    n_holes       INTEGER,
    n_stale       INTEGER,                 -- verified, but from an older tree version
    n_missing     INTEGER,
    n_damaged     INTEGER,
    status        TEXT,
    notes         TEXT
);

-- Every version ("root") of a volume's block tree that reconstruction used.
CREATE TABLE IF NOT EXISTS volume_roots (
    id          INTEGER PRIMARY KEY,
    volume_id   INTEGER NOT NULL REFERENCES volumes(id),
    txg         INTEGER NOT NULL,           -- birth of the top-level block pointer
    top_bp      BLOB NOT NULL,              -- raw 128-byte blkptr of the top indirect block
    provenance  TEXT NOT NULL,              -- e.g. 'ring-mos:113959', 'carved-objset@0x...'
    usable      INTEGER NOT NULL
);

-- One row per L1 span (up to 2^(indblkshift-7) consecutive logical blocks). Rather than
-- one row per 16K block, the map keeps the level-1 indirect blocks that describe the span.
-- Every L0 location, size and checksum is derived from them and verified on read.
--   candidates  n x 129 bytes: raw 128-byte blkptr of an L1 block + 1 provenance byte
--               (0 = pointed to by a verified parent, 1 = carved and placed by matching)
--   choice      one byte per slot: index of the candidate supplying that slot (255 = none)
--   status      one byte per slot: zfsrecover.map.codes.BlockStatus
CREATE TABLE IF NOT EXISTS volume_spans (
    volume_id   INTEGER NOT NULL REFERENCES volumes(id),
    span        INTEGER NOT NULL,
    first_blkid INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    candidates  BLOB NOT NULL,
    choice      BLOB NOT NULL,
    status      BLOB NOT NULL,
    max_birth   INTEGER,
    PRIMARY KEY (volume_id, span)
);

-- Run-length summary of volume status for fast display and gap reporting.
CREATE TABLE IF NOT EXISTS volume_coverage (
    volume_id   INTEGER NOT NULL REFERENCES volumes(id),
    first_blkid INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    PRIMARY KEY (volume_id, first_blkid)
);

-- ---------------------------------------------------------------- contents
CREATE TABLE IF NOT EXISTS partitions (
    id          INTEGER PRIMARY KEY,
    volume_id   INTEGER NOT NULL REFERENCES volumes(id),
    scheme      TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    start       INTEGER NOT NULL,          -- byte offset within the volume
    length      INTEGER NOT NULL,
    type_id     TEXT,
    type_name   TEXT,
    name        TEXT,
    uuid        TEXT,
    coverage    REAL                       -- fraction of bytes recoverable
);

CREATE TABLE IF NOT EXISTS filesystems (
    id           INTEGER PRIMARY KEY,
    volume_id    INTEGER NOT NULL REFERENCES volumes(id),
    partition_id INTEGER REFERENCES partitions(id),
    start        INTEGER NOT NULL,         -- byte offset within the volume
    length       INTEGER NOT NULL,
    fstype       TEXT NOT NULL,
    plugin       TEXT,                     -- NULL if identified only
    label        TEXT,
    uuid         TEXT,
    block_size   INTEGER,
    state        TEXT,                     -- inventoried | identified | failed
    info_json    TEXT
);

CREATE TABLE IF NOT EXISTS fs_entries (
    id            INTEGER PRIMARY KEY,
    fs_id         INTEGER NOT NULL REFERENCES filesystems(id),
    inode         INTEGER,
    parent_inode  INTEGER,
    name          TEXT,
    path          TEXT,
    type          TEXT,                    -- file | dir | symlink | device | fifo | socket | other
    size          INTEGER,
    mode          INTEGER,
    uid           INTEGER,
    gid           INTEGER,
    atime         INTEGER,
    mtime         INTEGER,
    ctime         INTEGER,
    crtime        INTEGER,
    deleted       INTEGER NOT NULL DEFAULT 0,
    status        TEXT,                    -- full | partial | none | n/a
    recoverable_bytes INTEGER,
    link_target   TEXT,
    extra_json    TEXT
);
CREATE INDEX IF NOT EXISTS fs_entries_path ON fs_entries(fs_id, path);
CREATE INDEX IF NOT EXISTS fs_entries_parent ON fs_entries(fs_id, parent_inode);

-- file_offset → volume byte offset. kind: data | sparse | inline
CREATE TABLE IF NOT EXISTS fs_extents (
    entry_id     INTEGER NOT NULL REFERENCES fs_entries(id),
    file_offset  INTEGER NOT NULL,
    length       INTEGER NOT NULL,
    volume_offset INTEGER,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,            -- ok | missing | partial
    PRIMARY KEY (entry_id, file_offset)
);

-- Human-readable account of what cannot be recovered and why.
CREATE TABLE IF NOT EXISTS unrecoverable (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL,              -- volume | file | dataset | pool
    ref_id     INTEGER,
    start      INTEGER,
    length     INTEGER,
    reason     TEXT NOT NULL,
    detail     TEXT
);
