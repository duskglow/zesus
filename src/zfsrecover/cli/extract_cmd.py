"""`zfsrecover extract` argument parsing. The engine lives in zfsrecover.extract."""

from __future__ import annotations

import argparse


def add_extract_parser(sub) -> None:
    e = sub.add_parser("extract", help="extract volumes, partitions or files using a map")
    e.add_argument("map")
    e.add_argument("source", help="the same image/device the map was built from")
    e.add_argument("-o", "--out", required=True, help="output directory")
    e.set_defaults(func=cmd_extract)


def cmd_extract(args: argparse.Namespace) -> int:
    raise SystemExit("extract: not implemented yet")
