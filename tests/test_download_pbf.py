from __future__ import annotations

import os

from dbsn import conflate, download_pbf
from dbsn.download_pbf import find_pbf


def _touch(root, name):
    d = root / "VE_2026-11-15"
    d.mkdir(exist_ok=True)
    (d / name).touch()


def test_find_pbf_without_slug_ignores_area_clipped(tmp_path, monkeypatch):
    monkeypatch.setattr(download_pbf, "CONFLATE_DIR", tmp_path)
    _touch(tmp_path, "VE_2026-11-15.osm.pbf")
    _touch(tmp_path, "VE_2026-11-15_marghera.osm.pbf")
    assert find_pbf("VE").name == "VE_2026-11-15.osm.pbf"


def test_find_pbf_with_slug_returns_area_clipped(tmp_path, monkeypatch):
    monkeypatch.setattr(download_pbf, "CONFLATE_DIR", tmp_path)
    _touch(tmp_path, "VE_2026-11-15.osm.pbf")
    _touch(tmp_path, "VE_2026-11-15_marghera.osm.pbf")
    assert find_pbf("VE", "marghera").name == "VE_2026-11-15_marghera.osm.pbf"


def test_find_pbf_without_slug_none_when_only_area_clipped(tmp_path, monkeypatch):
    monkeypatch.setattr(download_pbf, "CONFLATE_DIR", tmp_path)
    _touch(tmp_path, "VE_2026-11-15_marghera.osm.pbf")
    assert find_pbf("VE") is None


def test_find_pbf_ignores_conflate_clip_pbf(tmp_path, monkeypatch):
    monkeypatch.setattr(download_pbf, "CONFLATE_DIR", tmp_path)
    _touch(tmp_path, "VE_2026-11-15_marghera_osm-x.clip.osm.pbf")
    assert find_pbf("VE") is None
    assert find_pbf("VE", "marghera") is None


def test_clip_pbf_rebuilds_only_when_older_than_source(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(conflate, "_osmium_extract", lambda src, dst, clip: (calls.append(dst), dst.touch()))
    src, dst = tmp_path / "src.osm.pbf", tmp_path / "dst.clip.osm.pbf"
    src.touch()
    assert conflate._clip_pbf(src, dst, None) == dst
    conflate._clip_pbf(src, dst, None)  # fresh: reused
    assert len(calls) == 1
    os.utime(src, (dst.stat().st_mtime + 10,) * 2)  # source newer: rebuilt
    conflate._clip_pbf(src, dst, None)
    assert len(calls) == 2
