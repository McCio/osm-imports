from shapely.geometry import box

from dbsn import download, neighbours


def _prov(code, bbox=None, nbs=()):
    p = {"code": code, "neighbours": list(nbs)}
    if bbox:
        p["bbox"] = bbox
    return p


def _setup(monkeypatch, clip):
    neighbours._clip_cache.clear()
    monkeypatch.setattr(neighbours, "load_area_clip", lambda p, area: clip)
    monkeypatch.setattr(download.DownloadTask, "skip_if", lambda self: True)


def test_without_area_all_neighbours(monkeypatch):
    _setup(monkeypatch, None)
    src = {c: _prov(c) for c in "ABC"}
    assert [n["code"] for n in neighbours.relevant_neighbours(_prov("A", nbs="BC"), src)] == ["B", "C"]


def test_area_keeps_only_close_bboxes(monkeypatch):
    _setup(monkeypatch, box(0, 0, 1, 1))
    src = {
        "NEAR": _prov("NEAR", [1.005, 0, 2, 1]),  # within the 0.009 threshold
        "FAR": _prov("FAR", [3, 0, 4, 1]),
        "NOBBOX": _prov("NOBBOX"),
    }
    got = neighbours.relevant_neighbours(_prov("P", nbs=["NEAR", "FAR", "NOBBOX"]), src, "x")
    assert [n["code"] for n in got] == ["NEAR", "NOBBOX"]


def test_area_clip_unavailable_keeps_all(monkeypatch):
    _setup(monkeypatch, None)
    src = {"FAR": _prov("FAR", [3, 0, 4, 1])}
    assert len(neighbours.relevant_neighbours(_prov("P", nbs=["FAR"]), src, "x")) == 1
