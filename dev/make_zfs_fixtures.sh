#!/bin/sh
# Build small file-backed ZFS pools with real OpenZFS for the multi-disk tests.
#
# Needs root and a loaded zfs module. On Windows, in WSL (once):
#     sudo apt install zfs-dkms zfsutils-linux      # builds the module for the running kernel
#     sudo modprobe zfs
# then:
#     wsl -e sudo sh dev/make_zfs_fixtures.sh
#
# For each layout it writes tests/fixtures/zfs/<name>/:
#     member<i>.img.gz   one file per vdev child, in child order
#     manifest.json      layout, pool/member guids, and SHA-256 of everything written
#
# Contents are synthetic (a seeded PRNG), never real data. Each pool gets:
#   * zvol "vol"   (lz4, volblocksize 8K): half random, half compressible;
#   * dataset "fs" (lz4): files of many sizes, so blocks use every RAIDZ width;
#   * zvol "gone" and dataset "gonefs", written, synced, then DESTROYED. Carving has
#     to find them again.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
# Pool labels record the creating host's name and hostid. The hostid is fixed here. The
# hostname is taken by the kernel from the system itself, so it can only be neutralized by
# renaming the machine for the duration of the build: set ZFX_HOSTNAME to opt in (the old
# name is restored on exit).
OLD_HOST=$(hostname)
if [ -n "${ZFX_HOSTNAME:-}" ] && [ "$ZFX_HOSTNAME" != "$OLD_HOST" ]; then
    hostname "$ZFX_HOSTNAME"
fi
HOSTID_PARAM=/sys/module/spl/parameters/spl_hostid
OLD_HOSTID=$(cat "$HOSTID_PARAM" 2>/dev/null || echo 0)
[ -w "$HOSTID_PARAM" ] && echo 1514492757 > "$HOSTID_PARAM"      # 0x5a455355, "ZESU"
OUT="$HERE/tests/fixtures/zfs"
TMP=$(mktemp -d /tmp/zfx.XXXXXX)
SIZE=${ZFX_SIZE:-96M}          # per member (the ZFS minimum is 64M, after its partitioning)
POOLS=""
LOOPS=""
cleanup() {
    for p in $POOLS; do zpool destroy -f "$p" 2>/dev/null || true; done
    for l in $LOOPS; do losetup -d "$l" 2>/dev/null || true; done
    rm -rf "$TMP"
    [ -w "$HOSTID_PARAM" ] && echo "$OLD_HOSTID" > "$HOSTID_PARAM" || true
    [ "$(hostname)" != "$OLD_HOST" ] && hostname "$OLD_HOST" || true
}
trap cleanup EXIT
command -v zpool >/dev/null || { echo "zpool not found: install zfsutils-linux" >&2; exit 1; }
command -v losetup >/dev/null || { echo "losetup not found (util-linux)" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
mkdir -p "$OUT"

gen() {  # gen SEED BYTES KIND -> stdout; KIND = random | text | mixed (see dev/zfx_gen.py)
    python3 "$HERE/dev/zfx_gen.py" "$1" "$2" "$3"
}

zvol_dev() {  # wait for the zvol device node
    for _ in $(seq 1 50); do
        [ -e "/dev/zvol/$1" ] && { echo "/dev/zvol/$1"; return; }
        udevadm settle 2>/dev/null || true
        sleep 0.2
    done
    echo "zvol device for $1 did not appear (is udev running?)" >&2
    exit 1
}

sha() { sha256sum "$1" | cut -d' ' -f1; }

build() {  # build NAME ASHIFT VDEVSPEC NMEMBERS
    name=$1 ashift=$2 kind=$3 n=$4
    pool="zfx_$(echo "$name" | tr '-' '_')"
    dir="$TMP/$name"
    mkdir -p "$dir"
    # Members are loop devices, not plain files. Under WSL, OpenZFS opens file vdevs from
    # kernel threads that do not see the distro's filesystem ("cannot create: no such pool
    # or dataset"). As whole disks, ZFS also partitions them, like real member disks.
    members=""
    loops=""
    i=0
    while [ $i -lt "$n" ]; do
        truncate -s "$SIZE" "$dir/member$i.img"
        l=$(losetup -f --show "$dir/member$i.img")
        loops="$loops $l"
        LOOPS="$LOOPS $l"
        members="$members $l"
        i=$((i + 1))
    done
    # shellcheck disable=SC2086
    zpool create -f -o ashift="$ashift" -O compression=lz4 -O atime=off -m none "$pool" $kind $members
    POOLS="$POOLS $pool"
    seed=$(printf '%s' "$name" | cksum | cut -d' ' -f1)

    zfs create -V 2M -o volblocksize=8K -o compression=lz4 "$pool/vol"
    vol=$(zvol_dev "$pool/vol")
    { gen "$seed" 524288 random; gen $((seed + 1)) 1048576 text; } > "$dir/vol.bin"
    dd if="$dir/vol.bin" of="$vol" bs=1M conv=fsync status=none

    zfs create -o mountpoint="$dir/fs" "$pool/fs"
    j=0
    for sz in 0 1 100 511 512 513 4095 4096 4097 12000 40000 131072 131073; do
        gen $((seed + 100 + j)) "$sz" mixed > "$dir/fs/f$j-$sz.bin"
        j=$((j + 1))
    done
    mkdir -p "$dir/fs/sub/deeper"
    gen $((seed + 200)) 70000 text > "$dir/fs/sub/deeper/nested.txt"

    zfs create -V 1M -o volblocksize=16K -o compression=lz4 "$pool/gone"
    gone=$(zvol_dev "$pool/gone")
    gen $((seed + 300)) 1048576 mixed > "$dir/gone.bin"
    dd if="$dir/gone.bin" of="$gone" bs=1M conv=fsync status=none
    zfs create -o mountpoint="$dir/gonefs" "$pool/gonefs"
    gen $((seed + 400)) 200000 mixed > "$dir/gonefs/lost.bin"
    zpool sync "$pool"
    sleep 1
    zpool sync "$pool"

    # manifest: before destroying, record what should be recoverable
    python3 - "$dir" "$name" "$kind" "$ashift" "$n" "$pool" "$seed" <<'PY' > "$dir/manifest.json"
import hashlib, json, os, subprocess, sys
d, name, kind, ashift, n, pool, seed = sys.argv[1:8]
def h(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()
files = {}
for root in ("fs", "gonefs"):
    base = os.path.join(d, root)
    for dp, _dn, fn in os.walk(base):
        for f in fn:
            p = os.path.join(dp, f)
            files[f"{root}/{os.path.relpath(p, base).replace(os.sep, '/')}"] = {"size": os.path.getsize(p), "sha256": h(p)}
def prop(ds, k):
    return subprocess.run(["zfs", "get", "-Hp", "-o", "value", k, ds], capture_output=True, text=True).stdout.strip()
guid = subprocess.run(["zpool", "get", "-Hp", "-o", "value", "guid", pool], capture_output=True, text=True).stdout.strip()
print(json.dumps({
    "name": name, "layout": kind, "ashift": int(ashift), "members": int(n), "pool": pool, "pool_guid": guid,
    "seed": int(seed),
    "content": {"vol": [[0, 524288, "random"], [1, 1048576, "text"]], "gone": [[300, 1048576, "mixed"]]},
    "zvols": {"vol": {"size": os.path.getsize(os.path.join(d, "vol.bin")), "sha256": h(os.path.join(d, "vol.bin")),
                      "volblocksize": 8192, "destroyed": False},
              "gone": {"size": os.path.getsize(os.path.join(d, "gone.bin")), "sha256": h(os.path.join(d, "gone.bin")),
                       "volblocksize": 16384, "destroyed": True}},
    "files": files,
    "zfs_version": subprocess.run(["zfs", "version"], capture_output=True, text=True).stdout.split(),
}, indent=1, sort_keys=True))
PY
    zfs destroy "$pool/gone"
    zfs destroy -r "$pool/gonefs"
    zpool sync "$pool"
    zfs unmount -a 2>/dev/null || true
    zpool export "$pool"
    POOLS=$(echo "$POOLS" | sed "s/ $pool//")
    for l in $loops; do
        losetup -d "$l"
        LOOPS=$(echo "$LOOPS" | sed "s| $l||")
    done

    rm -rf "$OUT/$name"
    mkdir -p "$OUT/$name"
    cp "$dir/manifest.json" "$OUT/$name/"
    i=0
    while [ $i -lt "$n" ]; do
        gzip -9 -n -c "$dir/member$i.img" > "$OUT/$name/member$i.img.gz"
        i=$((i + 1))
    done
    echo "built $name: $(du -sh "$OUT/$name" | cut -f1)"
}

build mirror-a12  12 mirror 2
build raidz1-a12  12 raidz1 4
build raidz1-a9    9 raidz1 3
build raidz2-a12  12 raidz2 5
build raidz3-a12  12 raidz3 7
