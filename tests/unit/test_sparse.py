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


def test_interrupted_output_never_appears_under_its_real_name(tmp_path):
    import pytest

    from zesus.extract.engine import WRITING_SUFFIX, Extractor, Options
    ex = Extractor(None, None, Options(out_dir=tmp_path))
    target = tmp_path / "f.bin"
    with pytest.raises(RuntimeError), ex._open_out(target, 1 << 20) as f:
        f.write(b"half")
        raise RuntimeError("interrupted")
    assert not target.exists()                    # a resumed run must not trust it
    assert (tmp_path / ("f.bin" + WRITING_SUFFIX)).exists()
    with ex._open_out(target, 4) as f:
        f.write(b"done")
    assert target.read_bytes() == b"done"
