"""Logging setup: a human-readable console narrative plus a JSON-lines file log."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
             "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


def setup(verbosity: int = 0, logfile: str | Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING if verbosity < 0 else
                     logging.INFO if verbosity == 0 else logging.DEBUG)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    if verbosity < 2:
        console.addFilter(lambda r: r.levelno >= logging.INFO and
                          (verbosity >= 1 or not r.name.startswith("zesus.zfs.")
                           or r.levelno >= logging.WARNING))
    root.addHandler(console)
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
