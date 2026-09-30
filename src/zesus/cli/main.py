"""Command-line interface.

    zesus scan IMAGE -o MAP [--phases ...]    (also: zesus-scan)
    zesus info MAP
    zesus extract MAP IMAGE ...               (also: zesus-extract)
"""

from __future__ import annotations

import argparse
import logging
import os
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
    p.add_argument("--strategy", choices=["inplace", "combined"], default="inplace",
                   help="inplace (default): read the member images directly. combined (mirrors only): first "
                        "build one vdev image from the healthiest member per region, then scan that")
    p.add_argument("--combined-image", help="with --strategy combined: where to write the combined image "
                                            "(default: next to the map). Reused if it already exists")
    p.add_argument("--mapfile", action="append", default=[], metavar="IMAGE=MAPFILE",
                   help="ddrescue mapfile of a member image: its unrescued ranges are taken from other members")
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
    if args.strategy == "combined":
        args.source = [str(_combined_image(args, mappath))]
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


def _mapfiles(specs: list[str]) -> dict[str, str]:
    out = {}
    for spec in specs:
        img, sep, mp = spec.rpartition("=")
        if not sep:
            raise SystemExit(f"--mapfile wants IMAGE=MAPFILE, got {spec!r}")
        out[img] = mp
    return out


def _combined_image(args: argparse.Namespace, mappath: Path) -> Path:
    from ..combine import CannotCombine, combine
    out = Path(args.combined_image) if args.combined_image else mappath.with_suffix(".combined.img")
    if out.exists() and Path(str(out) + ".provenance.json").exists():
        log.info("reusing combined image %s", out)
        return out
    try:
        combine(args.source, out, _mapfiles(args.mapfile))
    except CannotCombine as exc:
        raise SystemExit(str(exc)) from exc
    return out


def cmd_combine(args: argparse.Namespace) -> int:
    from ..combine import CannotCombine, combine
    from ..progress import Progress, cli_logger
    logsetup.setup(args.verbose)
    prog = Progress("combine")
    prog.listen(cli_logger(log), 30)
    try:
        res = combine(args.sources, args.out, _mapfiles(args.mapfile), progress=prog)
    except CannotCombine as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{res.path}: {res.size} bytes; regions per member {res.regions_from}; "
          f"{len(res.missing)} region(s) on no member (see {res.path}.mapfile)")
    return 0 if not res.missing else 2


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


def cmd_losses(args: argparse.Namespace) -> int:
    from ..losses import build_report, markdown, to_json
    from ..map.db import MapDB
    logsetup.setup(args.verbose)
    db = MapDB(args.map, readonly=True)
    pool, ss = None, None
    if args.evidence is not None:
        from ..evidence import EvidenceError, open_evidence, pool_for_map
        try:
            ss = open_evidence(db, args.evidence)
            pool = pool_for_map(db, ss)
        except EvidenceError as exc:
            raise SystemExit(str(exc)) from exc
    try:
        rep = build_report(db, pool, args.fs)
    finally:
        if ss is not None:
            ss.close()
    text = to_json(rep) if args.json else markdown(rep, ranges=not args.no_ranges)
    if args.out:
        from ..io import guard
        guard.assert_not_protected([Path(args.out)])
        Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
        log.info("written to %s", args.out)
    else:
        print(text)
    return 0


def cmd_recheck(args: argparse.Namespace) -> int:
    from ..carve.scanner import Stop
    from ..io.sourceset import SourceSet
    from ..map.db import MapDB
    from ..progress import MapPublisher, Progress, cli_logger
    from ..recheck import recheck
    from ..scan import pipeline

    mappath = Path(args.map)
    logsetup.setup(args.verbose, args.log or mappath.with_suffix(".log.jsonl"))
    src = SourceSet.open(args.sources)
    apply_parallel_args(args, [m.name for m in src])
    db = MapDB(mappath)
    stop = Stop()
    stop.install()
    prog = Progress("recheck")
    prog.listen(cli_logger(log), 30)
    publisher = MapPublisher(mappath)
    prog.listen(publisher, 2.0)
    ctx = pipeline.ScanContext(db=db, source=src, stop=stop, progress=prog, options={"progress_interval": 30})
    try:
        res = recheck(ctx, inventory=not args.no_inventory)
    finally:
        prog.finish("done")
        publisher.close()
        db.commit()
        db.close()
        src.close()
    for v in res.volumes:
        print(f"{v['volume']}: {v['recovered']} of {v['lost_before']} lost blocks recovered, "
              f"{v['lost_after']} still lost")
    return 0


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
    cb = sub.add_parser("combine", help="build one vdev image from the members of a mirror (not RAIDZ)")
    cb.add_argument("sources", nargs="+", help="member images (read-only)")
    cb.add_argument("-o", "--out", required=True, help="combined image to write (not on the evidence)")
    cb.add_argument("--mapfile", action="append", default=[], metavar="IMAGE=MAPFILE",
                    help="ddrescue mapfile of a member image: its unrescued ranges are taken from other members")
    cb.set_defaults(func=cmd_combine)
    lo = sub.add_parser("losses", help="list lost and damaged files, and whether carving or a missing disk "
                                       "could recover more")
    lo.add_argument("map")
    lo.add_argument("--fs", type=int, help="only this filesystem (see extract --list)")
    lo.add_argument("--evidence", nargs="*", metavar="IMAGE",
                    help="also check each lost block against the evidence: could a missing member help? "
                         "With no images, the ones recorded in the map are used")
    lo.add_argument("--json", action="store_true")
    lo.add_argument("--no-ranges", action="store_true", help="omit the zero-filled byte ranges")
    lo.add_argument("-o", "--out", help="write the report to this file instead of printing it")
    lo.set_defaults(func=cmd_losses)
    rc = sub.add_parser("recheck", help="retry only the lost blocks with new evidence (e.g. a member disk "
                                        "that arrived after the scan)")
    rc.add_argument("map")
    rc.add_argument("sources", nargs="+", help="every member image, including the new one (read-only)")
    rc.add_argument("--no-inventory", action="store_true",
                    help="do not rebuild the file inventory afterwards (file statuses stay as they were)")
    add_parallel_args(rc)
    rc.set_defaults(func=cmd_recheck)
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
    wb.add_argument("--source", action="append", default=[],
                    help="image/device (repeat once per member disk) for scans and extraction from the UI; "
                         "default: the files recorded in the map")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8765)
    wb.set_defaults(func=cmd_web)
    return p


class _LastMessage(logging.Handler):
    """Remembers the last INFO+ line: a job's summary for the web UI."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.last = ""
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        self.last = msg
        if record.levelno >= logging.ERROR:
            self.errors.append(msg)


def main(argv: list[str] | None = None) -> int:
    from ..jobs import job_id, write_exit
    args = build_parser().parse_args(argv)
    tracker = None
    if job_id():
        tracker = _LastMessage()
        logging.getLogger("zesus").addHandler(tracker)
    code, state = 1, None
    try:
        code = int(args.func(args) or 0)
        return code
    except KeyboardInterrupt:
        log.error("aborted")
        code, state = 130, "cancelled"
        return 130
    except BaseException as exc:
        if tracker is not None:
            tracker.errors.append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if tracker is not None:
            from ..jobs import JOB_DIR_ENV, JOB_ENV
            flag = Path(os.environ[JOB_DIR_ENV]) / f"{os.environ[JOB_ENV]}.cancel"
            if flag.exists():
                state = "cancelled"
            msg = "; ".join(tracker.errors[-3:]) if (code and tracker.errors) else tracker.last
            if state == "cancelled":
                msg = "stopped on request; run it again to resume where it stopped"
            write_exit(code, msg, state, overwrite=False)


def scan_main() -> int:
    return main(["scan", *sys.argv[1:]])


def extract_main() -> int:
    return main(["extract", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
