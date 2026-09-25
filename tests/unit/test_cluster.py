"""Carved tree generations are grouped by shared blocks, never by size alone."""

from __future__ import annotations

from types import SimpleNamespace

from zfsrecover.carve.reconstruct import VolumeReconstructor


def make(sigs: dict[str, set]) -> VolumeReconstructor:
    rec = VolumeReconstructor.__new__(VolumeReconstructor)
    rec.ds = {"name": "vol"}
    rec.signature = lambda root, depth=2: sigs[root.name]      # type: ignore[method-assign]
    return rec


def R(name: str):
    return SimpleNamespace(name=name)


def names(groups):
    return sorted(sorted(r.name for r in g) for g in groups)


def test_generations_chain_but_strangers_stay_apart():
    sigs = {
        "a1": {1, 2, 3}, "a2": {3, 4, 5}, "a3": {5, 6},        # a1-a2-a3 chained by shared blocks
        "b1": {100, 101}, "b2": {101, 102},                     # another zvol of the same size
        "c": {999},                                             # unrelated singleton
    }
    rec = make(sigs)
    groups = rec.cluster([R(n) for n in sigs])
    assert names(groups) == [["a1", "a2", "a3"], ["b1", "b2"], ["c"]]


def test_anchored_cluster_excludes_other_datasets():
    sigs = {"ring": {1, 2}, "x": {2, 7}, "y": {7, 8}, "other": {50}}
    rec = make(sigs)
    (members,) = rec.cluster([R("x"), R("y"), R("other")], anchors=[R("ring")])
    assert sorted(r.name for r in members) == ["x", "y"]
