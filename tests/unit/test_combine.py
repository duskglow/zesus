"""ddrescue mapfiles: which ranges were not rescued."""

from __future__ import annotations

from zesus.combine import BadRanges


def test_parse_ddrescue_mapfile(tmp_path):
    p = tmp_path / "d.map"
    p.write_text("# Mapfile. Created by GNU ddrescue\n# current_pos  current_status  current_pass\n"
                 "0x00120000     +               1\n#      pos        size  status\n"
                 "0x00000000  0x00100000  +\n0x00100000  0x00001000  -\n0x00101000  0x0000F000  /\n"
                 "0x00110000  0x00010000  *\n0x00120000  0x7FEE0000  +\n", encoding="utf-8")
    b = BadRanges.parse(p)
    assert b.ranges == [(0x100000, 0x101000), (0x101000, 0x110000), (0x110000, 0x120000)]
    assert b.overlaps(0x100800, 0x100900) and not b.overlaps(0, 0x100000) and not b.overlaps(0x120000, 0x130000)
