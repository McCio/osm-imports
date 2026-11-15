"""Tests for absorbing JOSM edits of conflate review files into resolution entries."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET

import pytest
from shapely import from_wkt
from shapely.geometry import MultiPolygon, box, mapping

from dbsn.edits import absorb_edits, diff_edited, drop_edited_copies, edited_sibling
from utils.writers import write_osm_items


def _write(path, features):
    """Write (shapely geom, tags) features the way conflate writes its review files."""
    write_osm_items([(mapping(g), t) for g, t in features], path, upload="never")


def _josm_delete(path, ref):
    """Mark the way tagged ref:dbsn=<ref> the way JOSM saves a deleted primitive."""
    tree = ET.parse(path)
    for way in tree.getroot().iter("way"):
        if any(t.get("k") == "ref:dbsn" and t.get("v") == ref for t in way.iter("tag")):
            way.set("action", "delete")
    tree.write(path)


def _bldg(ref, geom, **tags):
    return geom, {"building": "yes", "ref:dbsn": ref, **tags}


A, B = box(12.0, 45.0, 12.001, 45.001), box(12.01, 45.0, 12.011, 45.001)


def test_new_feature_removed_from_edited_layer_is_skipped(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A), _bldg("B", B)])
    _write(edited, [_bldg("B", B)])

    assert diff_edited(original, edited, "new") == {"dbsn:A": {"decision": "skip"}}


@pytest.mark.parametrize("extra_tags", [{}, {"keep_osm": "yes"}])
def test_new_feature_josm_deleted_primitive_is_skipped(tmp_path, extra_tags):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A), _bldg("B", B)])
    _write(edited, [_bldg("A", A, **extra_tags), _bldg("B", B)])
    _josm_delete(edited, "A")

    assert diff_edited(original, edited, "new") == {"dbsn:A": {"decision": "skip"}}


def _pair(ref, geom, osm, note_extra="", **tags):
    """A `_conflated` review feature: DBSN geometry + merged tags, mate noted as osm=<id>."""
    return geom, {"building": "yes", "ref:dbsn": ref, "note:conflate": f"osm={osm}{note_extra}", **tags}


def test_conflated_pair_removed_deletes_osm_mate_and_excludes_dbsn(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11"), _pair("B", B, "w22")])
    _write(edited, [_pair("B", B, "w22")])

    assert diff_edited(original, edited, "conflated") == {"w11": {"decision": "delete", "dbsn_classid": "A"}}


@pytest.mark.parametrize("value", ["yes", "true", "1", "maybe"])
def test_conflated_pair_removed_with_keep_osm_leaves_osm_alone(tmp_path, value):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11"), _pair("B", B, "w22")])
    _write(edited, [_pair("A", A, "w11", keep_osm=value), _pair("B", B, "w22")])
    _josm_delete(edited, "A")  # tagged, then deleted: the documented encoding

    assert diff_edited(original, edited, "conflated") == {"w11": {"decision": "keep_osm", "dbsn_classid": "A"}}


@pytest.mark.parametrize("value", ["yes", "true", "1", "maybe"])
def test_conflated_pair_tagged_keep_osm_leaves_osm_alone_whatever_else_changed(tmp_path, value):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11"), _pair("B", B, "w22")])
    _write(edited, [_pair("A", A, "w11", keep_osm=value, name="X"), _pair("B", B, "w22")])

    assert diff_edited(original, edited, "conflated") == {"w11": {"decision": "keep_osm", "dbsn_classid": "A"}}


@pytest.mark.parametrize("value", ["no", "false", "0"])
def test_conflated_keep_osm_false_is_no_instruction(tmp_path, value):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11")])
    _write(edited, [_pair("A", A, "w11", keep_osm=value)])

    assert diff_edited(original, edited, "conflated") == {}


def test_conflated_merged_pair_removed_acts_on_every_merged_dbsn_feature(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11", ",merged=X|Y")])
    _write(edited, [])

    assert diff_edited(original, edited, "conflated") == {
        "w11": {"decision": "delete", "dbsn_classid": "A"},
        "dbsn:X": {"decision": "skip"},
        "dbsn:Y": {"decision": "skip"},
    }


def test_conflated_pair_untouched_yields_nothing(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11", **{"note:conflate:geometry": "updated"})])
    _write(edited, [_pair("A", A, "w11")])

    assert diff_edited(original, edited, "conflated") == {}


def test_conflated_pair_edits_become_dbsn_overrides(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    moved = box(12.0005, 45.0, 12.0015, 45.001)
    _write(original, [_pair("A", A, "w11", height="5")])
    _write(edited, [_pair("A", moved, "w11", name="X")])

    entries = diff_edited(original, edited, "conflated")

    assert list(entries) == ["dbsn:A"]
    assert entries["dbsn:A"]["tags"] == {"name": "X", "-height": ""}
    assert from_wkt(entries["dbsn:A"]["geometry"]).equals(moved)


def test_absorb_picks_up_conflated_edits(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11")])
    _write(edited, [])
    resolutions = tmp_path / "VE_resolutions.json"

    assert absorb_edits([original], resolutions) == 1
    assert json.loads(resolutions.read_text()) == {"w11": {"decision": "delete", "dbsn_classid": "A"}}


def test_drop_edited_copies_removes_every_sibling_and_keeps_originals(tmp_path):
    files = [tmp_path / "VE_new.osm", tmp_path / "VE_conflated.osm", tmp_path / "VE_osm_only.osm"]
    for f in files:
        f.write_text("<osm/>")
    for f in files[:2]:
        edited_sibling(f).write_text("user edits")

    dropped = drop_edited_copies(files)

    assert dropped == [edited_sibling(f) for f in files[:2]]
    assert not any(edited_sibling(f).exists() for f in files)
    assert all(f.exists() for f in files)


def _absorb_fixture(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A, height="5"), _bldg("B", B)])
    _write(edited, [_bldg("A", A, name="X")])  # B removed, A: -height +name
    return original, tmp_path / "VE_resolutions.json"


def test_absorb_writes_resolutions_and_is_idempotent(tmp_path):
    original, resolutions = _absorb_fixture(tmp_path)

    absorb_edits([original], resolutions)
    first = json.loads(resolutions.read_text())
    absorb_edits([original], resolutions)

    assert first == {
        "dbsn:A": {"tags": {"name": "X", "-height": ""}},
        "dbsn:B": {"decision": "skip"},
    }
    assert json.loads(resolutions.read_text()) == first


def test_absorb_keeps_existing_entries_and_newest_tag_edit_wins(tmp_path):
    original, resolutions = _absorb_fixture(tmp_path)
    resolutions.write_text(json.dumps({
        "w1": {"decision": "keep_osm"},
        "dbsn:A": {"tags": {"height": "9", "name": "Old"}},  # re-adding height beats the -height edit
    }))

    absorb_edits([original], resolutions)

    assert json.loads(resolutions.read_text()) == {
        "w1": {"decision": "keep_osm"},
        "dbsn:A": {"tags": {"name": "X", "-height": ""}},
        "dbsn:B": {"decision": "skip"},
    }


def test_absorb_without_edited_copy_writes_nothing(tmp_path):
    original = tmp_path / "VE_new.osm"
    _write(original, [_bldg("A", A)])
    resolutions = tmp_path / "VE_resolutions.json"

    assert absorb_edits([original], resolutions) == 0
    assert not resolutions.exists()


def test_absorb_ignores_edited_copy_that_was_never_saved_since_regeneration(tmp_path):
    original, resolutions = _absorb_fixture(tmp_path)
    stamp = original.stat().st_mtime_ns
    os.utime(tmp_path / "VE_new_edited.osm", ns=(stamp, stamp))  # a fresh copy keeps the original's mtime

    assert absorb_edits([original], resolutions) == 0
    assert not resolutions.exists()


def test_new_feature_untouched_yields_nothing(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A, **{"note:conflate:block": "greenfield"})])
    _write(edited, [_bldg("A", A)])  # conflate meta tags are not user edits

    assert diff_edited(original, edited, "new") == {}


def test_new_feature_moved_geometry_is_recorded_as_wkt(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    moved = box(12.0005, 45.0, 12.0015, 45.001)
    _write(original, [_bldg("A", A), _bldg("B", B)])
    _write(edited, [_bldg("A", moved), _bldg("B", B)])

    entries = diff_edited(original, edited, "new")

    assert list(entries) == ["dbsn:A"]
    assert set(entries["dbsn:A"]) == {"geometry"}
    assert from_wkt(entries["dbsn:A"]["geometry"]).equals(moved)


def test_new_multipart_feature_geometry_edit_is_recorded(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    moved_b = box(12.02, 45.0, 12.021, 45.001)
    _write(original, [_bldg("M", MultiPolygon([A, B]))])
    _write(edited, [_bldg("M", MultiPolygon([A, moved_b]))])

    entries = diff_edited(original, edited, "new")

    assert from_wkt(entries["dbsn:M"]["geometry"]).equals(MultiPolygon([A, moved_b]))


def test_new_feature_tag_edits_become_overrides(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A, height="5", name="Old")])
    _write(edited, [_bldg("A", A, name="New", **{"building:levels": "2"})])

    assert diff_edited(original, edited, "new") == {
        "dbsn:A": {"tags": {"name": "New", "building:levels": "2", "-height": ""}},
    }
