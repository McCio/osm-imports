"""Tests for absorbing JOSM edits of conflate review files into resolution entries."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET

import pytest
from shapely import from_wkt
from shapely.geometry import MultiPolygon, box, mapping

from dbsn.edits import absorb_edits, diff_edited, drop_edited_copies, edited_sibling, unedited_copies
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
DECISION = "note:conflate:decision"


def test_new_feature_removed_from_edited_layer_is_skipped(tmp_path):
    original, edited = tmp_path / "VE_new.osm", tmp_path / "VE_new_edited.osm"
    _write(original, [_bldg("A", A), _bldg("B", B)])
    _write(edited, [_bldg("B", B)])

    assert diff_edited(original, edited, "new") == {"dbsn:A": {"decision": "skip"}}


@pytest.mark.parametrize("extra_tags", [{}, {DECISION: "keep"}])
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


@pytest.mark.parametrize("value", ["keep", "kepp"])
def test_conflated_pair_removed_with_keep_decision_leaves_osm_alone(tmp_path, value):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11"), _pair("B", B, "w22")])
    _write(edited, [_pair("A", A, "w11", **{DECISION: value}), _pair("B", B, "w22")])
    _josm_delete(edited, "A")  # tagged, then deleted: the documented encoding

    assert diff_edited(original, edited, "conflated") == {"w11": {"decision": "keep_osm", "dbsn_classid": "A"}}


@pytest.mark.parametrize("value", ["keep", "KEEP", "kepp"])  # an unknown value warns and keeps: the safe direction
def test_conflated_pair_decided_keep_leaves_osm_alone_whatever_else_changed(tmp_path, value):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11"), _pair("B", B, "w22")])
    _write(edited, [_pair("A", A, "w11", name="X", **{DECISION: value}), _pair("B", B, "w22")])

    assert diff_edited(original, edited, "conflated") == {"w11": {"decision": "keep_osm", "dbsn_classid": "A"}}


def test_conflated_undecided_is_no_instruction(tmp_path):
    original, edited = tmp_path / "VE_conflated.osm", tmp_path / "VE_conflated_edited.osm"
    _write(original, [_pair("A", A, "w11")])
    _write(edited, [_pair("A", A, "w11", **{DECISION: "undecided"})])

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


def _osm(osm_id, geom, **tags):
    """An `_osm_only` review feature: OSM geometry and tags, identity in `osm:id`."""
    return geom, {"building": "yes", "osm:id": osm_id, **tags}


def test_osm_only_feature_removed_is_deleted(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _write(original, [_osm("w11", A), _osm("w22", B)])
    _write(edited, [_osm("w22", B)])

    assert diff_edited(original, edited, "osm_only") == {"w11": {"decision": "delete"}}


def test_osm_only_feature_untouched_yields_nothing(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _write(original, [_osm("w11", A, **{"note:conflate": "contained_by,dbsn=X"})])
    _write(edited, [_osm("w11", A)])

    assert diff_edited(original, edited, "osm_only") == {}


def test_osm_only_tag_edits_become_tag_update(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _write(original, [_osm("w11", A, height="5", name="Old")])
    _write(edited, [_osm("w11", A, name="New")])

    assert diff_edited(original, edited, "osm_only") == {
        "w11": {"decision": "tag_update", "tags": {"name": "New", "-height": ""}},
    }


def test_osm_only_geometry_edit_is_recorded_as_wkt_on_the_osm_id(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    moved = box(12.0005, 45.0, 12.0015, 45.001)
    _write(original, [_osm("w11", A)])
    _write(edited, [_osm("w11", moved, name="X")])

    entries = diff_edited(original, edited, "osm_only")

    assert list(entries) == ["w11"]
    assert entries["w11"]["decision"] == "tag_update"
    assert entries["w11"]["tags"] == {"name": "X"}
    assert from_wkt(entries["w11"]["geometry"]).equals(moved)


def _near_dbsn(ref, geom, osm, **tags):
    """The DBSN member of a near-miss pair in `_ambiguous`."""
    return geom, {"building": "yes", "ref:dbsn": ref, "note:conflate": f"near_miss,osm={osm}", **tags}


def _near_osm(osm_id, geom, ref, **tags):
    """The OSM member of a near-miss pair in `_ambiguous`."""
    return geom, {"building": "yes", "osm:id": osm_id, "note:conflate": f"near_miss,dbsn={ref}", **tags}


def test_ambiguous_near_miss_osm_member_removed_means_match(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11")])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "match", "dbsn_classid": "A"}}


def test_ambiguous_shape_match_osm_member_removed_means_match(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    dbsn = (A, {"building": "yes", "ref:dbsn": "A", "note:conflate": "shape_match,osm=w11,suggest=replace"})
    osm = (B, {"building": "yes", "osm:id": "w11", "note:conflate": "shape_match,dbsn=A,suggest=replace"})
    _write(original, [dbsn, osm])
    _write(edited, [dbsn])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "match", "dbsn_classid": "A"}}


@pytest.mark.parametrize("on_dbsn", [True, False])
def test_ambiguous_explicit_match_on_either_member_matches_the_pair(tmp_path, on_dbsn):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    value = {DECISION: "MATCH"}
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11", **(value if on_dbsn else {})),
                    _near_osm("w11", B, "A", **({} if on_dbsn else value))])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "match", "dbsn_classid": "A"}}


def _nm(dbsn_ids, osm_ids, **per_id):
    """An N:M `ambiguous` group; `per_id` maps a feature id to its extra tags."""
    dbsn = [(A, {"building": "yes", "ref:dbsn": d, "note:conflate": f"ambiguous,osm={','.join(osm_ids)}",
                 **per_id.get(d, {})}) for d in dbsn_ids]
    osm = [(B, {"building": "yes", "osm:id": o, "note:conflate": f"ambiguous,dbsn={','.join(dbsn_ids)}",
                **per_id.get(o, {})}) for o in osm_ids]
    return dbsn, osm


def test_ambiguous_n_to_m_match_pairs_the_one_remaining_mate_and_the_others_are_removed(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [*_nm(["D1", "D2"], ["w1", "w2"])[0], *_nm(["D1", "D2"], ["w1", "w2"])[1]])
    dbsn, osm = _nm(["D1", "D2"], ["w1", "w2"], D1={DECISION: "match"})
    _write(edited, [dbsn[0], osm[0]])

    assert diff_edited(original, edited, "ambiguous") == {
        "w1": {"decision": "match", "dbsn_classid": "D1"},
        "w2": {"decision": "delete"},
        "dbsn:D2": {"decision": "skip"},
    }


def test_ambiguous_n_to_m_match_with_several_mates_left_is_not_absorbed(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    dbsn, osm = _nm(["D1"], ["w1", "w2"])
    _write(original, [*dbsn, *osm])
    dbsn, osm = _nm(["D1"], ["w1", "w2"], D1={DECISION: "match"})
    _write(edited, [*dbsn, *osm])

    assert diff_edited(original, edited, "ambiguous") == {}


def test_ambiguous_n_to_m_osm_member_removed_is_deleted_not_matched(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    dbsn, osm = _nm(["D1"], ["w1", "w2"])
    _write(original, [*dbsn, *osm])
    _write(edited, [*dbsn, osm[0]])

    assert diff_edited(original, edited, "ambiguous") == {"w2": {"decision": "delete"}}


def test_ambiguous_near_miss_match_keeps_the_edit_of_the_dbsn_member(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11", name="X")])

    assert diff_edited(original, edited, "ambiguous") == {
        "w11": {"decision": "match", "dbsn_classid": "A"},
        "dbsn:A": {"tags": {"name": "X"}},
    }


@pytest.mark.parametrize("value", ["delete", "drop"])
def test_ambiguous_near_miss_osm_member_decided_delete_deletes_the_way(tmp_path, value):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A", **{DECISION: value})])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "delete"}}


def test_ambiguous_near_miss_both_members_removed_deletes_the_way_and_skips_the_dbsn_feature(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "delete"}, "dbsn:A": {"decision": "skip"}}


def test_ambiguous_contained_osm_member_removed_deletes_the_way(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context, part = _has_part()
    _write(original, [context, part])
    _write(edited, [context])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "delete"}}


def test_ambiguous_has_part_dbsn_member_removed_is_skipped(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context = (A, {"building": "yes", "ref:dbsn": "A", "note:conflate": "has_part,osm=w11"})
    part = (B, {"building": "roof", "osm:id": "w11", "note:conflate": "contained_by,dbsn=A"})
    _write(original, [context, part])
    _write(edited, [part])

    assert diff_edited(original, edited, "ambiguous") == {"dbsn:A": {"decision": "skip"}}


@pytest.mark.parametrize("value", ["keep", "both"])
def test_ambiguous_near_miss_decided_keep_on_osm_member_keeps_both(tmp_path, value):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A", **{DECISION: value})])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "keep_osm"}}


def test_ambiguous_decided_keep_on_dbsn_member_keeps_every_osm_mate(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    dbsn = {"building": "yes", "ref:dbsn": "A", "note:conflate": "ambiguous,osm=w11,w12"}
    osm = lambda i, g: (g, {"building": "yes", "osm:id": i, "note:conflate": "ambiguous,dbsn=A"})  # noqa: E731
    _write(original, [(A, dbsn), osm("w11", B), osm("w12", B)])
    _write(edited, [(A, {**dbsn, DECISION: "keep"}), osm("w11", B), osm("w12", B)])

    assert diff_edited(original, edited, "ambiguous") == {
        "w11": {"decision": "keep_osm"}, "w12": {"decision": "keep_osm"},
    }


def test_ambiguous_near_miss_with_both_geometries_edited_keeps_both_with_the_edits(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    moved_dbsn, moved_osm = box(12.0005, 45.0, 12.0015, 45.001), box(12.02, 45.0, 12.021, 45.001)
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", moved_dbsn, "w11"), _near_osm("w11", moved_osm, "A")])

    entries = diff_edited(original, edited, "ambiguous")

    assert sorted(entries) == ["dbsn:A", "w11"]
    assert entries["w11"]["decision"] == "tag_update"
    assert from_wkt(entries["w11"]["geometry"]).equals(moved_osm)
    assert "decision" not in entries["dbsn:A"]
    assert from_wkt(entries["dbsn:A"]["geometry"]).equals(moved_dbsn)


@pytest.mark.parametrize("edit_dbsn", [True, False])
def test_ambiguous_near_miss_with_one_member_edited_is_not_absorbed(tmp_path, edit_dbsn):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    moved = box(12.0005, 45.0, 12.0015, 45.001)
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    dbsn_geom, osm_geom = (moved, B) if edit_dbsn else (A, moved)
    _write(edited, [_near_dbsn("A", dbsn_geom, "w11"), _near_osm("w11", osm_geom, "A")])

    assert diff_edited(original, edited, "ambiguous") == {}


def _has_part(decision=None):
    """DBSN feature A with the OSM roof w11 inside it (the roof optionally decided)."""
    context = (A, {"building": "yes", "ref:dbsn": "A", "note:conflate": "has_part,osm=w11"})
    part = (B, {"building": "roof", "osm:id": "w11", "note:conflate": "contained_by,dbsn=A"})
    return context, (part[0], {**part[1], **({DECISION: decision} if decision else {})})


def test_ambiguous_contained_osm_feature_edit_without_a_decision_is_not_absorbed(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context, (geom, tags) = _has_part()
    _write(original, [context, (geom, tags)])
    _write(edited, [context, (geom, {**{k: v for k, v in tags.items() if k != "building"}, "building:part": "yes"})])

    assert diff_edited(original, edited, "ambiguous") == {}


def test_ambiguous_make_part_keeps_a_shape_and_tag_edit_of_the_part(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    moved = box(12.0105, 45.0, 12.0115, 45.001)
    context, part = _has_part()
    _write(original, [context, part])
    _write(edited, [context, (moved, {**_has_part("part")[1][1], "name": "Roof"})])

    entries = diff_edited(original, edited, "ambiguous")

    assert list(entries) == ["w11"]
    assert entries["w11"]["decision"] == "tag_update"
    assert entries["w11"]["tags"] == {"name": "Roof", "building:part": "roof", "-building": ""}
    assert from_wkt(entries["w11"]["geometry"]).equals(moved)


def test_ambiguous_keep_on_osm_member_keeps_its_tag_edit_and_the_edit_of_its_dbsn_mate(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_dbsn("A", A, "w11", name="D"), _near_osm("w11", B, "A", name="O", **{DECISION: "keep"})])

    assert diff_edited(original, edited, "ambiguous") == {
        "w11": {"decision": "tag_update", "tags": {"name": "O"}},
        "dbsn:A": {"tags": {"name": "D"}},
    }


def test_ambiguous_keep_on_has_part_dbsn_member_keeps_its_edit_and_every_osm_mate(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context, part = _has_part()
    _write(original, [context, part])
    _write(edited, [(context[0], {**context[1], DECISION: "keep", "name": "Main"}), part])

    assert diff_edited(original, edited, "ambiguous") == {
        "w11": {"decision": "keep_osm"},
        "dbsn:A": {"tags": {"name": "Main"}},
    }


def test_ambiguous_osm_edit_is_absorbed_when_its_dbsn_mate_was_removed(tmp_path):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    _write(original, [_near_dbsn("A", A, "w11"), _near_osm("w11", B, "A")])
    _write(edited, [_near_osm("w11", B, "A", name="X")])

    assert diff_edited(original, edited, "ambiguous") == {
        "dbsn:A": {"decision": "skip"},
        "w11": {"decision": "tag_update", "tags": {"name": "X"}},
    }


@pytest.mark.parametrize("value", ["make_part", "part"])
def test_ambiguous_contained_feature_decided_make_part_retags_it_as_a_building_part(tmp_path, value):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context, part = _has_part()
    _write(original, [context, part])
    _write(edited, [context, _has_part(value)[1]])

    assert diff_edited(original, edited, "ambiguous") == {
        "w11": {"decision": "tag_update", "tags": {"building:part": "roof", "-building": ""}},
    }


@pytest.mark.parametrize("value", ["delete", "drop"])
def test_ambiguous_contained_feature_decided_delete_deletes_the_way(tmp_path, value):
    original, edited = tmp_path / "VE_ambiguous.osm", tmp_path / "VE_ambiguous_edited.osm"
    context, part = _has_part()
    _write(original, [context, part])
    _write(edited, [context, _has_part(value)[1]])

    assert diff_edited(original, edited, "ambiguous") == {"w11": {"decision": "delete"}}


def _osm_only_with_nodes(path, corners, unsafe, **way_tags):
    """An `_osm_only` file as the writer emits it for way w11: a closed way whose unsafe vertices are identity
    nodes (`osm:id=nXXX`, the OSM tags, a `note:conflate:unsafe` decision); `unsafe` is {corner index: tags}."""
    nodes, refs = [], []
    for i, (lon, lat) in enumerate(corners):
        tags = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in unsafe.get(i, {}).items())
        nodes.append(f'<node id="{-1 - i}" lon="{lon}" lat="{lat}" version="1">{tags}</node>')
        refs.append(f'<nd ref="{-1 - i}"/>')
    tags = {"building": "yes", "osm:id": "w11", **way_tags}
    way_tags_xml = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
    path.write_text(
        f'<?xml version="1.0"?><osm version="0.6" upload="never">{"".join(nodes)}'
        f'<way id="-100" version="1">{"".join(refs)}<nd ref="-1"/>{way_tags_xml}</way></osm>',
        encoding="utf-8",
    )


_SQUARE = [(12.0, 45.0), (12.0001, 45.0), (12.0001, 45.0001), (12.0, 45.0001)]
_SQUARE_EAST = [(lon + 0.00001, lat) for lon, lat in _SQUARE]  # ~1 m east
_ENTRANCE = {"osm:id": "n2", "entrance": "yes", "note:conflate:unsafe": "detach", "note:conflate:unsafe_reason": "tagged"}


def test_osm_only_moved_unsafe_vertex_tagged_carry_is_recorded_with_its_tags(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _osm_only_with_nodes(original, _SQUARE, {1: _ENTRANCE})
    _osm_only_with_nodes(edited, _SQUARE_EAST, {1: {**_ENTRANCE, "note:conflate:unsafe": "carry"}})

    entry = diff_edited(original, edited, "osm_only")["w11"]

    assert entry["decision"] == "tag_update"
    assert entry["carry_nodes"] == {"n2": {"lon": 12.00011, "lat": 45.0, "tags": {"entrance": "yes"}}}


@pytest.mark.parametrize(("shape", "node_tags"), [
    (_SQUARE_EAST, _ENTRANCE),                                          # moved, left on the default detach
    (_SQUARE_EAST, {}),                                                 # moved, node removed from the file: detach
    (_SQUARE, {**_ENTRANCE, "note:conflate:unsafe": "carry"}),          # carry, but unmoved: nothing to decide
], ids=["moved-detach", "moved-node-removed", "unmoved-carry"])
def test_osm_only_unsafe_vertex_without_a_carry_decision_records_no_carry(tmp_path, shape, node_tags):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _osm_only_with_nodes(original, _SQUARE, {1: _ENTRANCE})
    _osm_only_with_nodes(edited, shape, {1: node_tags})

    entries = diff_edited(original, edited, "osm_only")

    assert "carry_nodes" not in entries.get("w11", {})


def test_osm_only_unsafe_vertex_tagged_delete_is_recorded(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _osm_only_with_nodes(original, _SQUARE, {1: _ENTRANCE})
    _osm_only_with_nodes(edited, _SQUARE_EAST, {1: {**_ENTRANCE, "note:conflate:unsafe": "delete"}})

    assert diff_edited(original, edited, "osm_only")["w11"]["delete_nodes"] == ["n2"]


def test_absorb_picks_up_osm_only_edits(tmp_path):
    original, edited = tmp_path / "VE_osm_only.osm", tmp_path / "VE_osm_only_edited.osm"
    _write(original, [_osm("w11", A)])
    _write(edited, [])
    resolutions = tmp_path / "VE_resolutions.json"

    assert absorb_edits([original], resolutions) == 1
    assert json.loads(resolutions.read_text()) == {"w11": {"decision": "delete"}}


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


def test_unedited_copies_are_the_edited_siblings_not_saved_since_the_original_was_written(tmp_path):
    saved, unsaved, fresh, no_copy = (tmp_path / f"VE_{n}.osm" for n in ("new", "conflated", "ambiguous", "osm_only"))
    for path in (saved, unsaved, fresh, no_copy):
        _write(path, [_bldg("A", A)])
    for path in (saved, unsaved, fresh):
        _write(edited_sibling(path), [_bldg("A", A)])
    stamp = saved.stat().st_mtime_ns
    os.utime(saved, ns=(stamp, stamp))
    os.utime(edited_sibling(saved), ns=(stamp + 10**9, stamp + 10**9))  # JOSM saved after the original
    os.utime(edited_sibling(unsaved), ns=(stamp - 10**9, stamp - 10**9))  # copy of an older original
    os.utime(fresh, ns=(stamp, stamp))
    os.utime(edited_sibling(fresh), ns=(stamp, stamp))  # a fresh copy keeps the original's mtime

    assert unedited_copies([saved, unsaved, fresh, no_copy]) == [unsaved]


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
