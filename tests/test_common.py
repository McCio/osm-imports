"""Tests for dbsn.common helpers — pure functions only."""

from __future__ import annotations

import pytest

from shapely.geometry import box

from dbsn import common
from dbsn.common import (
    OVERWRITE_STEPS,
    load_combined_clip,
    parse_osm_area_selector,
    parse_overwrite,
)


def test_parse_overwrite_none_is_empty():
    assert parse_overwrite(None) == frozenset()


def test_parse_overwrite_bare_is_all():
    # bare --overwrite arrives via argparse const="all"
    assert parse_overwrite(["all"]) == frozenset(OVERWRITE_STEPS)


def test_parse_overwrite_csv_and_repeated():
    assert parse_overwrite(["conflate,pbf"]) == frozenset({"conflate", "pbf"})
    assert parse_overwrite(["conflate", "pbf"]) == frozenset({"conflate", "pbf"})


def test_parse_overwrite_rejects_unknown_step():
    with pytest.raises(SystemExit, match="unknown --overwrite step"):
        parse_overwrite(["downloads"])  # typo'd names must fail, not silently no-op


@pytest.mark.parametrize(("selector", "expected"), [
    ("r/6628636", ("r", 6628636, None)),
    ("W/107637324", ("w", 107637324, None)),
    ("Marghera", ("", None, "marghera")),
    ("  Isola dei Petroli ", ("", None, "isola dei petroli")),
    ("Murano/Burano", ("", None, "murano/burano")),  # a name with "/" is not an id
    ("r/abc", ("", None, "r/abc")),
])
def test_parse_osm_area_selector(selector, expected):
    assert parse_osm_area_selector(selector) == expected


PROV = {"code": "VE"}


def test_combined_clip_none_when_no_selector():
    assert load_combined_clip(PROV, None, None) is None


def test_combined_clip_intersects_both(monkeypatch):
    monkeypatch.setattr(common, "load_area_clip", lambda p, a: box(0, 0, 2, 2))
    monkeypatch.setattr(common, "load_osm_area_clip", lambda p, a: box(1, 1, 3, 3))
    assert load_combined_clip(PROV, "x", "y").equals(box(1, 1, 2, 2))


def test_combined_clip_single_source(monkeypatch):
    monkeypatch.setattr(common, "load_area_clip", lambda p, a: box(0, 0, 2, 2))
    monkeypatch.setattr(common, "load_osm_area_clip", lambda p, a: None)
    assert load_combined_clip(PROV, "x", "y").equals(box(0, 0, 2, 2))


def test_combined_clip_warns_on_empty_intersection(monkeypatch, capsys):
    monkeypatch.setattr(common, "load_area_clip", lambda p, a: box(0, 0, 1, 1))
    monkeypatch.setattr(common, "load_osm_area_clip", lambda p, a: box(5, 5, 6, 6))
    assert load_combined_clip(PROV, "x", "y").is_empty
    assert "do not overlap" in capsys.readouterr().err
