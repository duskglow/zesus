#!/bin/sh
# Build the small ext4 test images in tests/fixtures/ (needs e2fsprogs; no root, no mount).
# Usage: dev/make_ext4_fixtures.sh            (on Windows: wsl -e sh dev/make_ext4_fixtures.sh)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT="$HERE/tests/fixtures"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"

populate() {
    d="$1"
    mkdir -p "$d/dir/sub/deeper" "$d/empty-dir"
    printf 'hello, world\n' > "$d/hello.txt"
    : > "$d/empty.txt"
    # deterministic 300 KiB binary (multi-extent sized)
    python3 -c "import sys; sys.stdout.buffer.write(bytes((i*7+i//251)&255 for i in range(300*1024)))" > "$d/dir/binary.bin"
    printf 'nested file\n' > "$d/dir/sub/deeper/nested.txt"
    # sparse file: 1 MiB hole then data
    python3 -c "
f=open('$d/sparse.bin','wb'); f.seek(1<<20); f.write(b'after the hole'); f.close()"
    ln -s hello.txt "$d/fast-link"
    ln -s "$(python3 -c "print('long/' * 20 + 'target')")" "$d/slow-link"
    ln "$d/hello.txt" "$d/dir/hardlink-to-hello"
    printf 'to be deleted\n%.0s' $(seq 1 400) > "$d/deleted.txt"
    printf 'name with spaces\n' > "$d/dir/a file: with odd*chars?.txt"
}

build() {
    name="$1"; shift
    root="$TMP/$name-root"
    img="$TMP/$name.img"
    mkdir -p "$root"
    populate "$root"
    mke2fs -q -F -t ext4 -b 1024 -N 128 -L "$name" -U 11111111-2222-3333-4444-555555555555 \
        -E root_owner=0:0 "$@" -d "$root" "$img" 8M
    # Deallocate deleted.txt's inode without clearing its block map, then drop the entry.
    ino=$(debugfs -R "stat /deleted.txt" "$img" 2>/dev/null | sed -n 's/^Inode: \([0-9]*\).*/\1/p')
    debugfs -w -R "kill_file /deleted.txt" "$img" >/dev/null 2>&1
    debugfs -w -R "unlink /deleted.txt" "$img" >/dev/null 2>&1
    echo "$name: deleted inode $ino"
    gzip -9 -n -c "$img" > "$OUT/$name.img.gz"
}

build ext4-basic -O ^metadata_csum_seed
build ext4-inline -O inline_data
ls -la "$OUT"
