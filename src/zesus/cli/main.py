"""Command-line interface.

    zesus scan IMAGE -o MAP [--phases ...]    (also: zesus-scan)
    zesus info MAP
    zesus extract MAP IMAGE ...               (also: zesus-extract)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .. import __version__
from .. import log as logsetup

log = logging.getLogger("zesus")

DEFAULT_PHASES = ["history", "datasets", "carve", "reconstruct", "verify", "contents"]


def _size(s: str) -> int:
    s = s.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s, 0)


def add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", nargs="+",
                   help="raw disk image(s) or block device(s), opened read-only. For multi-disk "
                        "pools give one per vdev member, in any order")
    p.add_argument("-o", "--map", required=True, help="output SQLite map (created or resumed)")
    p.add_argument("--phases", default=",".join(DEFAULT_PHASES),
                   help=f"comma-separated phases to run (default: {','.join(DEFAULT_PHASES)})")
    p.add_argument("--redo", default="", help="comma-separated phases to re-run even if complete")
    p.add_argument("--chunk-size", type=_size, default=64 << 20, help="carving chunk size (default 64M)")
    p.add_argument("--carve-start", type=_size, default=None, help="carve from this DVA offset")
    p.add_argument("--carve-end", type=_size, default=None, help="carve up to this DVA offset")
    p.add_argument("--limit-blocks", type=int, default=None,
                   help="(testing) only reconstruct the first N logical blocks of each volume")
    p.add_argument("--snapshots", action="store_true",
                   help="also inventory the files of every ZFS snapshot (can be large)")
    p.add_argument("--ignore-ring", action="store_true",
                   help="(validation) reconstruct volumes from carved blocks only, as if every "
                        "uberblock had lost track of them")
    p.add_argument("--progress-interval", type=float, default=30, help="seconds between progress lines")
    add_parallel_args(p)


def add_parallel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workers", type=int, default=None,
                   help="threads for CPU-bound work (checksums, decompression, parity); "
                        "default: CPU count - 1, at most 8")
    p.add_argument("--io-depth", type=int, default=None,
                   help="reads in flight per stage (default 1: sequential, best for spinning disks "
                        "and network shares; 2-4 can help on SSD/NVMe)")


def apply_parallel_args(args: argparse.Namespace, paths) -> None:
    from ..parallel import configure, describe_io
    configure(getattr(args, "workers", None), getattr(args, "io_depth", None))
    describe_io(paths)


def cmd_scan(args: argparse.Namespace) -> int:
    from ..carve.scanner import Stop
    from ..io.sourceset import SourceSet
    from ..map.db import MapDB
    from ..progress import MapPublisher, Progress, cli_logger
    from ..scan import pipeline

    mappath = Path(args.map)
    logsetup.setup(args.verbose, args.log or mappath.with_suffix(".log.jsonl"))
    log.info("zesus %s: scan %s -> %s", __version__, " ".join(args.source), mappath)
    src = SourceSet.open(args.source)
    apply_parallel_args(args, [m.name for m in src])
    db = MapDB(mappath)
    stop = Stop()
    stop.install()
    prog = Progress("scan")
    prog.listen(cli_logger(log), args.progress_interval)
    publisher = MapPublisher(mappath)
    prog.listen(publisher, 2.0)
    ctx = pipeline.ScanContext(db=db, source=src, stop=stop, progress=prog, options={
        "chunk_size": args.chunk_size, "carve_start": args.carve_start, "carve_end": args.carve_end,
        "progress_interval": args.progress_interval, "limit_blocks": args.limit_blocks, "ignore_ring": args.ignore_ring, "snapshots": args.snapshots,
        "redo": {x for x in args.redo.split(",") if x}})
    for name in ctx.options["redo"]:
        db.reset_phase(name)
    db.commit()
    phases = [x for x in args.phases.split(",") if x]
    state = "failed"
    try:
        pipeline.run(ctx, phases)
        state = "stopped" if stop.event.is_set() else "done"
    finally:
        prog.finish(state)
        publisher.close()
        db.commit()
        db.close()
        src.close()
    log.info("scan finished; map at %s", mappath)
    return 0


def cmd_members(args: argparse.Namespace) -> int:
    import json

    from ..io.sourceset import SourceSet
    from ..zfs.members import assess
    from ..zfs.pool import open_pools

    logsetup.setup(args.verbose)
    with SourceSet.open(args.sources) as ss:
        pools = open_pools(ss)
        if not pools:
            print("no ZFS pool labels found", file=sys.stderr)
            return 1
        reports = [assess(p) for p in pools]
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
    else:
        for r in reports:
            print("\n".join(r.lines()))
    return 0 if all(r.readable for r in reports) else 2


def cmd_info(args: argparse.Namespace) -> int:
    from ..map.db import MapDB
    from .report import print_info
    logsetup.setup(args.verbose)
    db = MapDB(args.map, readonly=True)
    print_info(db, sys.stdout)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from ..io import guard
    from ..map.db import MapDB
    from .fullreport import write_report
    logsetup.setup(args.verbose)
    out = Path(args.out)
    guard.assert_not_protected([out])
    write_report(MapDB(args.map, readonly=True), out, out.with_suffix(".csv"), args.max_items)
    log.info("report written to %s (+ %s)", out, out.with_suffix(".csv"))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    logsetup.setup(args.verbose, args.log)
    try:
        from ..web.app import serve
    except ImportError as exc:
        raise SystemExit(f"web UI needs extra packages: pip install 'zesus[web]' ({exc})") from exc
    serve(args.map, args.source, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zesus", description="ZFS forensic scanner and extractor")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", dest="verbose", action="store_const", const=-1)
    p.add_argument("--log", help="JSON-lines log file (default: next to the map)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan a source and build/resume a map")
    add_scan_args(s)
    s.set_defaults(func=cmd_scan)
    m = sub.add_parser("members", help="identify vdev member images and whether the pool can be read")
    m.add_argument("sources", nargs="+", help="disk images or devices, one per vdev member (read-only)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_members)
    i = sub.add_parser("info", help="summarize a map")
    i.add_argument("map")
    i.set_defaults(func=cmd_info)
    from .extract_cmd import add_extract_parser
    add_extract_parser(sub)
    rp = sub.add_parser("report", help="write a Markdown recovery report and a per-file CSV")
    rp.add_argument("map")
    rp.add_argument("-o", "--out", required=True, help="report file (.md); a .csv is written alongside")
    rp.add_argument("--max-items", type=int, default=200)
    rp.set_defaults(func=cmd_report)
    wb = sub.add_parser("web", help="browse a map in a local web UI (needs the [web] extra)")
    wb.add_argument("map")
    wb.add_argument("--source", help="image/device, to enable extraction from the UI")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8765)
    wb.set_defaults(func=cmd_web)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        log.error("aborted")
        return 130


def scan_main() -> int:
    return main(["scan", *sys.argv[1:]])


def extract_main() -> int:
    return main(["extract", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
