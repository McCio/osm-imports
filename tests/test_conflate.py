"""Tests for DBSN conflation logic — matching, tag merge."""

from __future__ import annotations

from shapely.geometry import box

from dbsn.conflate import is_zero_delta
from dbsn.match import match_features
from dbsn.tag_merge import merge_tags


# ---------------------------------------------------------------------------
# Zero-delta detection (identical matches dropped from review)
# ---------------------------------------------------------------------------

def test_zero_delta_identical_tags_high_iou():
    assert is_zero_delta(0.96, {"building": "yes"}, {"building": "yes"})


def test_zero_delta_low_iou_not_identical():
    assert not is_zero_delta(0.90, {"building": "yes"}, {"building": "yes"})


def test_zero_delta_merged_adds_tags_not_identical():
    assert not is_zero_delta(0.96, {"building": "yes", "height": "5"}, {"building": "yes"})


def test_zero_delta_threshold_override():
    assert is_zero_delta(0.90, {"building": "yes"}, {"building": "yes"}, identical_iou=0.9)

# ---------------------------------------------------------------------------
# Feature matching
# ---------------------------------------------------------------------------

_CFG = {"centroid_distance_m": 500, "iou_threshold": 0.3}


def test_match_one_to_one():
    dbsn = [("d1", box(0, 0, 1, 1))]
    osm = [("w1", box(0.05, 0.05, 1.05, 1.05))]  # slightly offset, high IoU
    result = match_features(dbsn, osm, _CFG)
    assert result.matched == [("d1", "w1")]
    assert result.dbsn_only == []
    assert result.osm_only == []
    assert result.ambiguous == []


def test_match_reports_per_pair_iou():
    dbsn = [("d1", box(0, 0, 1, 1))]
    osm = [("w1", box(0.1, 0.1, 1.1, 1.1))]
    result = match_features(dbsn, osm, _CFG)
    iou = result.matched_ious[("d1", "w1")]
    inter = box(0, 0, 1, 1).intersection(box(0.1, 0.1, 1.1, 1.1)).area
    union = box(0, 0, 1, 1).union(box(0.1, 0.1, 1.1, 1.1)).area
    assert abs(iou - inter / union) < 1e-9


def test_match_greedy_best_iou_wins():
    osm_box = box(0, 0, 1, 1)
    # d1 almost identical to osm_box (high IoU), d2 overlaps less
    dbsn = [("d1", box(0.01, 0.01, 1.01, 1.01)), ("d2", box(0.4, 0.4, 1.4, 1.4))]
    osm = [("w1", osm_box)]
    result = match_features(dbsn, osm, _CFG)
    assert ("d1", "w1") in result.matched
    assert "d2" in result.dbsn_only


def test_match_no_overlap_goes_to_unmatched():
    dbsn = [("d1", box(0, 0, 1, 1))]
    osm = [("w1", box(10, 10, 11, 11))]  # far away, no overlap
    result = match_features(dbsn, osm, _CFG)
    assert result.matched == []
    assert result.dbsn_only == ["d1"]
    assert result.osm_only == ["w1"]


# ---------------------------------------------------------------------------
# Tag merge
# ---------------------------------------------------------------------------

_SPEC = {
    "building": ["yes", "residential", "apartments"],
    "man_made": ["yes", "bridge", "tower"],
}


def test_merge_dbsn_more_specific_wins():
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "apartments"},
        osm_tags={"building": "yes"},
        specificity=_SPEC,
    )
    assert merged["building"] == "apartments"
    assert conflicts == {}


def test_merge_unlisted_values_conflict():
    # Neither value in the ranking → conflict, OSM value kept, conflict recorded
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "farm"},
        osm_tags={"building": "barn"},
        specificity=_SPEC,
    )
    assert merged["building"] == "barn"
    assert conflicts == {"building": ("barn", "farm")}


def test_merge_additive_fill():
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "residential", "source": "DBSN 2024"},
        osm_tags={"building": "yes"},
        specificity=_SPEC,
    )
    assert merged["building"] == "residential"
    assert merged["source"] == "DBSN 2024"
    assert conflicts == {}


def test_merge_osm_more_specific_wins():
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "residential"},
        osm_tags={"building": "apartments"},
        specificity=_SPEC,
    )
    assert merged["building"] == "apartments"
    assert conflicts == {}
