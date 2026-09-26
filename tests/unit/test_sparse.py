"""Creating a large output must set its size, not write it."""

from __future__ import annotations

import os
import time

from zesus.extract.sparse import make_sparse, set_size


def test_set_size_extends_without_writing(tmp_path):
    p = tmp_path / "big.img"
    t = time.monotonic()
    with open(p, "wb") as f:
        make_sparse(f)
        set_size(f, 64 << 30)              # 64 GiB: writing zeros would take minutes
        f.seek(5 << 30)
        f.write(b"data")
    assert time.monotonic() - t < 5
    assert os.path.getsize(p) == 64 << 30
    with open(p, "rb") as f:
        f.seek(5 << 30)
        assert f.read(4) == b"data"
        f.seek((64 << 30) - 8)
        assert f.read() == b"\0" * 8
