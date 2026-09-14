"""Tests for DBSN conflation logic — matching, tag merge, triage diff."""

from __future__ import annotations

import pytest
from shapely.geometry import box, Point

# ---------------------------------------------------------------------------
# Triage diff helpers
# ---------------------------------------------------------------------------

def _osm_feat(osm_id: str, tags: dict, dbsn_classid: str | None = None) -> dict:
    t = {"osm:id": osm_id, **tags}
    if dbsn_classid:
        t["ref:dbsn"] = dbsn_classid
    return {"id": osm_id, "tags": t}


def _dbsn_feat(classid: str, tags: dict) -> dict:
    return {"id": f"dbsn:{classid}", "tags": {"ref:dbsn": classid, **tags}}


# ---------------------------------------------------------------------------
# Triage diff
# ---------------------------------------------------------------------------


def test_triage_replace_decision():
    from dbsn.triage import triage

    # Ambiguous had: OSM way w1 matched to DBSN d1
    # Resolved: mapper deleted w1, kept d1 → "replace" decision
    ambiguous = [
        _osm_feat("w1", {"building": "yes"}, dbsn_classid="d1"),
        _dbsn_feat("d1", {"building": "residential"}),
    ]
    resolved = [_dbsn_feat("d1", {"building": "residential"})]  # w1 deleted

    resolutions = triage(ambiguous, resolved, existing={})
    assert resolutions["w1"]["decision"] == "replace"


def test_triage_keep_osm_decision():
    from dbsn.triage import triage

    # Mapper deleted the DBSN feature, kept the OSM way → keep_osm
    ambiguous = [
        _osm_feat("w1", {"building": "yes"}, dbsn_classid="d1"),
        _dbsn_feat("d1", {"building": "residential"}),
    ]
    resolved = [_osm_feat("w1", {"building": "yes"})]  # d1 deleted

    resolutions = triage(ambiguous, resolved, existing={})
    assert resolutions["w1"]["decision"] == "keep_osm"


def test_triage_prior_decisions_preserved():
    from dbsn.triage import triage

    # Second triage run: prior w0 decision must survive even though it's no longer in ambiguous
    existing = {"w0": {"decision": "replace", "dbsn_classid": "d0"}}
    ambiguous = [
        _osm_feat("w1", {"building": "yes"}, dbsn_classid="d1"),
        _dbsn_feat("d1", {"building": "residential"}),
    ]
    resolved = [_dbsn_feat("d1", {"building": "residential"})]

    resolutions = triage(ambiguous, resolved, existing=existing)
    assert resolutions["w0"]["decision"] == "replace"   # preserved
    assert resolutions["w1"]["decision"] == "replace"   # new


def test_triage_both_deleted_is_delete():
    from dbsn.triage import triage

    # Mapper deleted both OSM way and DBSN feature → demolish OSM, don't import DBSN
    ambiguous = [
        _osm_feat("w1", {"building": "yes"}, dbsn_classid="d1"),
        _dbsn_feat("d1", {"building": "residential"}),
    ]
    resolved = []  # both gone

    resolutions = triage(ambiguous, resolved, existing={})
    assert resolutions["w1"]["decision"] == "delete"


def test_triage_revision_overwrites():
    from dbsn.triage import triage

    # Same ID appears in a new triage run with a different decision → overwrite
    existing = {"w1": {"decision": "keep_osm"}}
    ambiguous = [
        _osm_feat("w1", {"building": "yes"}, dbsn_classid="d1"),
        _dbsn_feat("d1", {"building": "residential"}),
    ]
    resolved = [_dbsn_feat("d1", {"building": "residential"})]  # w1 deleted → replace

    resolutions = triage(ambiguous, resolved, existing=existing)
    assert resolutions["w1"]["decision"] == "replace"


# ---------------------------------------------------------------------------
# Feature matching
# ---------------------------------------------------------------------------

_CFG = {"centroid_distance_m": 500, "iou_threshold": 0.3}


def test_match_one_to_one():
    from dbsn.match import match_features

    dbsn = [("d1", box(0, 0, 1, 1))]
    osm = [("w1", box(0.05, 0.05, 1.05, 1.05))]  # slightly offset, high IoU
    result = match_features(dbsn, osm, _CFG)
    assert result.matched == [("d1", "w1")]
    assert result.dbsn_only == []
    assert result.osm_only == []
    assert result.ambiguous == []


def test_match_greedy_best_iou_wins():
    from dbsn.match import match_features

    osm_box = box(0, 0, 1, 1)
    # d1 almost identical to osm_box (high IoU), d2 overlaps less
    dbsn = [("d1", box(0.01, 0.01, 1.01, 1.01)), ("d2", box(0.4, 0.4, 1.4, 1.4))]
    osm = [("w1", osm_box)]
    result = match_features(dbsn, osm, _CFG)
    assert ("d1", "w1") in result.matched
    assert "d2" in result.dbsn_only


def test_match_no_overlap_goes_to_unmatched():
    from dbsn.match import match_features

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
    from dbsn.tag_merge import merge_tags

    merged, conflicts = merge_tags(
        dbsn_tags={"building": "apartments"},
        osm_tags={"building": "yes"},
        specificity=_SPEC,
    )
    assert merged["building"] == "apartments"
    assert conflicts == {}


def test_merge_unlisted_values_conflict():
    from dbsn.tag_merge import merge_tags

    # Neither value in the ranking → conflict, OSM value kept, conflict recorded
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "farm"},
        osm_tags={"building": "barn"},
        specificity=_SPEC,
    )
    assert merged["building"] == "barn"
    assert conflicts == {"building": ("barn", "farm")}


def test_merge_additive_fill():
    from dbsn.tag_merge import merge_tags

    merged, conflicts = merge_tags(
        dbsn_tags={"building": "residential", "source": "DBSN 2024"},
        osm_tags={"building": "yes"},
        specificity=_SPEC,
    )
    assert merged["building"] == "residential"
    assert merged["source"] == "DBSN 2024"
    assert conflicts == {}


def test_merge_osm_more_specific_wins():
    from dbsn.tag_merge import merge_tags

    merged, conflicts = merge_tags(
        dbsn_tags={"building": "residential"},
        osm_tags={"building": "apartments"},
        specificity=_SPEC,
    )
    assert merged["building"] == "apartments"
    assert conflicts == {}
