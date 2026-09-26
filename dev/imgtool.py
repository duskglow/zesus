#!/usr/bin/env python3
"""Developer helper for inspecting a ZFS image with the library's own readers.

The image path comes from --image, $ZESUS_TEST_IMAGE, or dev/testimage.toml. Everything is
read-only (RawSource + guard).

    python dev/imgtool.py gpt
    python dev/imgtool.py labels
    python dev/imgtool.py uberblocks
    python dev/imgtool.py history [--grep destroy]
    python dev/imgtool.py datasets [--txg N]
    python dev/imgtool.py hexdump 0x100000 256
    python dev/imgtool.py dva 0:5800016000:1000 [--lsize 0x4000 --comp lz4] [--hexdump]
    python dev/imgtool.py bp <256 hex chars>
    python dev/imgtool.py objset --dataset kikuri/vm-100-disk-0 [--txg N] [--obj 1]
    python dev/imgtool.py zap --dataset NAME --obj 2 | --mos-obj 1
    python dev/imgtool.py carve-sample 0x0 0x40000000
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zesus.io import RawSource  # noqa: E402
from zesus.partitions import detect  # noqa: E402
from zesus.zfs import blkptr, compress  # noqa: E402
from zesus.zfs.blkptr import Dva  # noqa: E402
from zesus.zfs.constants import VDEV_LABEL_START_SIZE, Compression  # noqa: E402
from zesus.zfs.pool import Pool, open_pools  # noqa: E402


def image_path(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("ZESUS_TEST_IMAGE"):
        return os.environ["ZESUS_TEST_IMAGE"]
    cfg = Path(__file__).with_name("testimage.toml")
    if cfg.exists():
        import tomllib
        return tomllib.loads(cfg.read_text())["image"]["path"]
    raise SystemExit("no image: pass --image, set ZESUS_TEST_IMAGE, or create dev/testimage.toml")


def hexdump(b: bytes, base: int = 0, limit: int = 512) -> None:
    for i in range(0, min(len(b), limit), 16):
        chunk = b[i:i + 16]
        print(f"{base + i:10x}  {chunk.hex(' '):<48}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)}")


def ts(t) -> str:
    return dt.datetime.fromtimestamp(t).isoformat(sep=" ") if t else "?"


def pool_of(src) -> Pool:
    pools = open_pools(src)
    if not pools:
        raise SystemExit("no pool found")
    return pools[0]


def mos_at(pool: Pool, txg: int | None):
    from zesus.zfs.objset import Objset
    for ub in pool.uberblocks:
        if txg is not None and ub.txg != txg:
            continue
        try:
            return ub, Objset(pool.reader, ub.rootbp, "MOS")
        except Exception as exc:
            if txg is not None:
                raise
            print(f"# txg {ub.txg}: MOS unreadable ({exc}); trying older", file=sys.stderr)
    raise SystemExit("no readable MOS")


def dataset_objset(pool: Pool, name: str, txg: int | None):
    from zesus.zfs.dsl import Dsl
    from zesus.zfs.objset import Objset
    for ub in pool.uberblocks:
        if txg is not None and ub.txg != txg:
            continue
        try:
            mos = Objset(pool.reader, ub.rootbp, "MOS")
            for di in Dsl(mos, pool.name).walk():
                if di.name == name and di.ds:
                    print(f"# {name} via uberblock txg {ub.txg}, objset birth {di.ds.bp.birth}", file=sys.stderr)
                    return Objset(pool.reader, di.ds.bp, name)
        except Exception:
            continue
    raise SystemExit(f"dataset {name} not reachable from any uberblock")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gpt")
    sub.add_parser("labels")
    sub.add_parser("uberblocks")
    h = sub.add_parser("history")
    h.add_argument("--grep")
    d = sub.add_parser("datasets")
    d.add_argument("--txg", type=int)
    x = sub.add_parser("hexdump")
    x.add_argument("offset", type=lambda s: int(s, 0))
    x.add_argument("length", type=lambda s: int(s, 0), nargs="?", default=256)
    v = sub.add_parser("dva")
    v.add_argument("dva", help="vdev:offset:asize (hex), as printed by zdb")
    v.add_argument("--lsize", type=lambda s: int(s, 0))
    v.add_argument("--comp", default="off", choices=[c.name.lower() for c in Compression])
    v.add_argument("--hexdump", action="store_true")
    b = sub.add_parser("bp")
    b.add_argument("hex")
    o = sub.add_parser("objset")
    o.add_argument("--dataset")
    o.add_argument("--txg", type=int)
    o.add_argument("--obj", type=int)
    z = sub.add_parser("zap")
    z.add_argument("--dataset")
    z.add_argument("--txg", type=int)
    z.add_argument("--obj", type=int)
    z.add_argument("--mos-obj", type=int)
    c = sub.add_parser("carve-sample")
    c.add_argument("start", type=lambda s: int(s, 0))
    c.add_argument("length", type=lambda s: int(s, 0))
    a = ap.parse_args()

    src = RawSource(image_path(a.image))

    if a.cmd == "gpt":
        scheme, parts = detect(src)
        print(scheme)
        for p in parts:
            print(f"  {p.index}: start={p.start:#x} len={p.length:#x} ({p.length / 2**30:.2f} GiB) "
                  f"{p.type_name} {p.name!r}")
    elif a.cmd == "labels":
        pool = pool_of(src)
        for im in pool.vdev_images:
            print(f"vdev at {im.base_offset:#x} (size {im.source.size:#x})")
            for lb in im.labels:
                print(f"  label {lb.index} @{lb.offset:#x}: config_ok={lb.config_ok} ubs={len(lb.uberblocks)} "
                      f"{lb.error or ''}")
            import pprint
            pprint.pprint(im.config, width=110)
    elif a.cmd == "uberblocks":
        pool = pool_of(src)
        for u in pool.uberblocks:
            print(f"txg {u.txg:>9}  {ts(u.timestamp)}  ck={'ok' if u.checksum_ok else 'BAD'}  {u.rootbp.describe()}")
    elif a.cmd == "history":
        from zesus.zfs.history import read_history
        from zesus.zfs.zap import read_zap
        pool = pool_of(src)
        _, mos = mos_at(pool, None)
        hist = read_history(mos.object(read_zap(mos.object(1))["history"]))
        for r in hist.records:
            line = f"{ts(r.time)}  txg {r.txg}  {r.summary()}"
            if not a.grep or a.grep.lower() in line.lower():
                print(line)
    elif a.cmd == "datasets":
        from zesus.zfs.dsl import Dsl
        pool = pool_of(src)
        ub, mos = mos_at(pool, a.txg)
        print(f"# uberblock txg {ub.txg}")
        for di in Dsl(mos, pool.name).walk():
            print(f"{di.name:50} dsobj={di.dsobj:<6} "
                  + (f"objset birth={di.ds.bp.birth} created txg {di.ds.creation_txg}" if di.ds else f"ERROR {di.error}"))
    elif a.cmd == "hexdump":
        hexdump(src.pread(a.offset, a.length), a.offset, a.length)
    elif a.cmd == "dva":
        pool = pool_of(src)
        vdev, off, asize = (int(p, 16) for p in a.dva.split(":")[:3])
        raw = pool.reader.read_dva(Dva(vdev, off, asize, False, 0), asize)[0][1]
        im = pool.vdev_images[0]
        print(f"# physical offset in source: {im.base_offset + VDEV_LABEL_START_SIZE + off:#x}")
        data = compress.decompress(Compression[a.comp.upper()], raw, a.lsize or asize) if a.lsize else raw
        hexdump(data, 0, len(data) if a.hexdump else 256)
    elif a.cmd == "bp":
        print(blkptr.parse(bytes.fromhex(a.hex)).describe())
    elif a.cmd in ("objset", "zap"):
        from zesus.zfs.zap import read_zap
        pool = pool_of(src)
        if a.dataset:
            os_ = dataset_objset(pool, a.dataset, a.txg)
        else:
            _, os_ = mos_at(pool, a.txg)
        if a.cmd == "objset":
            print(f"type={os_.phys.type_name}  meta: {os_.phys.meta_dnode.describe()}")
            if a.obj is not None:
                d = os_.dnode(a.obj)
                print(f"object {a.obj}: {d.describe()}")
                for bp in d.blkptrs:
                    print("   ", bp.describe())
            else:
                for obj, d in os_.iter_dnodes():
                    print(f"{obj:>8}  {d.describe()}")
        else:
            obj = a.mos_obj if a.mos_obj is not None else a.obj
            for k, val in sorted(read_zap(os_.object(obj)).items(), key=lambda kv: str(kv[0])):
                print(f"{k} = {val}")
    elif a.cmd == "carve-sample":
        from collections import Counter

        from zesus.carve.classify import PoolLimits
        from zesus.carve.scanner import carve_chunk
        pool = pool_of(src)
        im = pool.vdev_images[0]
        top = pool.vdevs.top[0]
        lim = PoolLimits(1, top.asize, pool.max_txg)
        raw = im.source.pread(VDEV_LABEL_START_SIZE + a.start, a.length + (128 << 10) + 8)
        hits, st = carve_chunk(raw, a.length, a.start, lim, pool.ashift)
        print(f"candidates={st.candidates} decompressed={st.decompressed} hits={st.hits} {st.by_kind}")
        kinds = Counter()
        for off, kind, _comp, psize, c in hits:
            kinds[(kind, c.child_type, c.child_level, c.os_type)] += 1
            print(f"  {off:#14x} {kind:9} psize={psize:#7x} lsize={c.lsize:#7x} "
                  f"type={c.child_type} lvl={c.child_level} os={c.os_type} birth={c.min_birth}..{c.max_birth}")
        print(kinds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
