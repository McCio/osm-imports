import fiona
from fiona.crs import CRS
from shapely.geometry import box, shape

from dbsn import extract

_SCHEMA = {"geometry": "Polygon", "properties": {"classid": "str", "shape_Area": "float"}}
_EXT = "AA_BB_2025-01-01_2025-01-01_edifc.fgb"


def _write(path, rows):
    with fiona.open(path, "w", driver="FlatGeobuf", schema=_SCHEMA, crs=CRS.from_epsg(4326)) as dst:
        for cid, geom, area in rows:
            dst.write({"type": "Feature", "geometry": geom.__geo_interface__,
                       "properties": {"classid": cid, "shape_Area": area}})


def _extend(tmp_path, monkeypatch, rows1, rows2):
    monkeypatch.setattr(extract, "BUILDINGS_DIR", tmp_path)
    p1, p2 = {"code": "AA", "date": "2025-01-01"}, {"code": "BB", "date": "2025-01-01"}
    _write(extract.fgb_path("AA", p1["date"], "edifc"), rows1)
    _write(extract.fgb_path("BB", p2["date"], "edifc"), rows2)
    extract.extend_pair(p1, p2, layer_names=["edifc"])
    with fiona.open(tmp_path / _EXT) as src:
        return list(src)


def test_extend_pair_unions_only_shared_classids(tmp_path, monkeypatch):
    feats = _extend(
        tmp_path, monkeypatch,
        [("A", box(0, 0, 1, 2), 2.0), ("B", box(5, 5, 6, 6), 1.0), (None, box(8, 8, 9, 9), 1.0)],
        [("A", box(1, 0, 2, 2), 1.5), ("C", box(7, 7, 8, 8), 1.0)],
    )
    assert [f["properties"]["classid"] for f in feats] == ["A"]
    assert feats[0]["properties"]["shape_Area"] == 2.0  # properties of the larger part
    assert shape(feats[0]["geometry"]).equals(box(0, 0, 2, 2))


def test_extend_pair_nothing_shared_still_writes_marker(tmp_path, monkeypatch):
    assert _extend(tmp_path, monkeypatch, [("A", box(0, 0, 1, 1), 1.0)], [("C", box(5, 5, 6, 6), 1.0)]) == []
