"""Command-line interface.

    zfsrecover scan IMAGE -o MAP [--phases ...]    (also: zfsrecover-scan)
    zfsrecover info MAP
    zfsrecover extract MAP IMAGE ...               (also: zfsrecover-extract)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .. import __version__
from .. import log as logsetup

log = logging.getLogger("zfsrecover")

DEFAULT_PHASES = ["history", "datasets", "carve", "reconstruct", "verify", "contents"]


def _size(s: str) -> int:
    s = s.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s, 0)


def add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="raw disk image or block device (opened read-only)")
    p.add_argument("-o", "--map", required=True, help="output SQLite map (created or resumed)")
    p.add_argument("--phases", default=",".join(DEFAULT_PHASES),
                   help=f"comma-separated phases to run (default: {','.join(DEFAULT_PHASES)})")
    p.add_argument("--redo", default="", help="comma-separated phases to re-run even if complete")
    p.add_argument("--chunk-size", type=_size, default=64 << 20, help="carving chunk size (default 64M)")
    p.add_argument("--carve-start", type=_size, default=None, help="carve from this DVA offset")
    p.add_argument("--carve-end", type=_size, default=None, help="carve up to this DVA offset")
    p.add_argument("--limit-blocks", type=int, default=None,
                   help="(testing) only reconstruct the first N logical blocks of each volume")
    p.add_argument("--progress-interval", type=float, default=30, help="seconds between progress lines")


def cmd_scan(args: argparse.Namespace) -> int:
    from ..carve.scanner import Stop
    from ..io.source import RawSource
    from ..map.db import MapDB
    from ..scan import pipeline

    mappath = Path(args.map)
    logsetup.setup(args.verbose, args.log or mappath.with_suffix(".log.jsonl"))
    log.info("zfs-forensic-recovery %s: scan %s -> %s", __version__, args.source, mappath)
    src = RawSource(args.source)
    db = MapDB(mappath)
    stop = Stop()
    stop.install()
    ctx = pipeline.ScanContext(db=db, source=src, stop=stop, options={
        "chunk_size": args.chunk_size, "carve_start": args.carve_start, "carve_end": args.carve_end,
        "progress_interval": args.progress_interval, "limit_blocks": args.limit_blocks,
        "redo": {x for x in args.redo.split(",") if x}})
    for name in ctx.options["redo"]:
        db.reset_phase(name)
    db.commit()
    phases = [x for x in args.phases.split(",") if x]
    try:
        pipeline.run(ctx, phases)
    finally:
        db.commit()
        db.close()
        src.close()
    log.info("scan finished; map at %s", mappath)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    from ..map.db import MapDB
    from .report import print_info
    logsetup.setup(args.verbose)
    db = MapDB(args.map, readonly=True)
    print_info(db, sys.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zfsrecover", description="ZFS forensic scanner and extractor")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", dest="verbose", action="store_const", const=-1)
    p.add_argument("--log", help="JSON-lines log file (default: next to the map)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan a source and build/resume a map")
    add_scan_args(s)
    s.set_defaults(func=cmd_scan)
    i = sub.add_parser("info", help="summarize a map")
    i.add_argument("map")
    i.set_defaults(func=cmd_info)
    from .extract_cmd import add_extract_parser
    add_extract_parser(sub)
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
