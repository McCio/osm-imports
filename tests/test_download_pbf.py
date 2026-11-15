from __future__ import annotations

from dbsn import download_pbf
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
