"""Pipelines keep input order and bounded look-ahead, stop cleanly, and surface errors."""

from __future__ import annotations

import random
import threading
import time

import pytest

from zesus.parallel import ordered_map, prefetch
from zesus.progress import Progress


def test_ordered_map_returns_input_order_despite_random_finish_times():
    def slow(x):
        time.sleep(random.random() / 200)
        return x * x
    assert list(ordered_map(slow, range(60), workers=8)) == [x * x for x in range(60)]


def test_ordered_map_bounds_work_in_flight():
    live, peak, lock = [0], [0], threading.Lock()

    def job(x):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.002)
        with lock:
            live[0] -= 1
        return x
    assert list(ordered_map(job, range(40), workers=8, ahead=3)) == list(range(40))
    assert peak[0] <= 3


def test_ordered_map_surfaces_errors_at_the_failing_item():
    def job(x):
        if x == 5:
            raise ValueError("boom")
        return x
    got = []
    with pytest.raises(ValueError):
        for r in ordered_map(job, range(10), workers=4):
            got.append(r)
    assert got == [0, 1, 2, 3, 4]


def test_prefetch_reads_at_most_depth_ahead_and_stops_early():
    produced = []

    def source():
        for i in range(100):
            produced.append(i)
            yield i
    it = prefetch(source(), depth=2)
    first = [next(it) for _ in range(3)]
    time.sleep(0.05)
    assert first == [0, 1, 2]
    assert len(produced) <= 3 + 2 + 1          # consumed + queue depth + one being put
    it.close()


def test_prefetch_propagates_errors():
    def source():
        yield 1
        raise OSError("read failed")
    it = prefetch(source())
    assert next(it) == 1
    with pytest.raises(OSError):
        next(it)


def test_progress_rate_and_eta():
    p = Progress("t")
    p.begin("stage", total=1000, unit="bytes")
    p._samples[0] = (p._samples[0][0] - 10, 0.0)      # pretend the stage began 10 s ago
    p.stage_started -= 10
    p.set(500)
    s = p.snapshot()
    assert s["pct"] == 50.0
    assert 40 < s["rate"] < 60 and 8 < s["eta_s"] < 12
    p.finish("done")
    assert p.snapshot()["state"] == "done"
