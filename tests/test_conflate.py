"""Tests for DBSN conflation logic — matching, tag merge."""

from __future__ import annotations

from shapely.geometry import box

from dbsn.conflate import compute_blocks, is_zero_delta, josm_load_urls
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
# Blocks (spatial clustering of conflation outcome buckets)
# ---------------------------------------------------------------------------

def test_blocks_two_clusters_are_separate_blocks():
    # Cluster of 2 new features + distant cluster of 1 matched
    new = [box(0, 0, 1, 1), box(0.5, 0, 1.5, 1)]      # overlapping → one block
    matched = [box(0, 3, 1, 4)]                        # far away → own block
    blocks = compute_blocks({"new": new, "matched": matched}, {})
    assert len(blocks) == 2
    counts = sorted(tuple(sorted(b.counts.items())) for b in blocks)
    assert counts == [(("matched", 1),), (("new", 2),)]


def test_blocks_nearby_features_merge_into_one_block():
    # buffer joins features closer than ~2× the buffer distance
    new = [box(0, 0, 1, 1), box(0, 0.002, 1, 1.002)]  # ~220 m apart in lat
    blocks = compute_blocks({"new": new}, {"block_buffer_m": 300})
    assert len(blocks) == 1
    assert blocks[0].counts["new"] == 2


def test_blocks_empty_buckets_no_blocks():
    assert compute_blocks({"new": [], "matched": []}, {}) == []


# ---------------------------------------------------------------------------
# Greenfield classification (block ready for bulk import)
# ---------------------------------------------------------------------------

def _cluster(n_new, n_matched=0, n_osm_only=0, n_ambiguous=0):
    """One tight cluster of features in a single block."""
    return {
        "new": [box(0, i * 0.001, 1, i * 0.001 + 1) for i in range(n_new)],
        "matched": [box(0, i * 0.001, 1, i * 0.001 + 1) for i in range(n_matched)],
        "osm_only": [box(0, i * 0.001, 1, i * 0.001 + 1) for i in range(n_osm_only)],
        "ambiguous": [box(0, i * 0.001, 1, i * 0.001 + 1) for i in range(n_ambiguous)],
    }


_GF_CFG = {"greenfield_min_new": 10, "greenfield_max_other_ratio": 0.05}


def test_greenfield_pure_new_cluster():
    blocks = compute_blocks(_cluster(15), _GF_CFG)
    assert len(blocks) == 1
    assert blocks[0].is_greenfield


def test_greenfield_small_cluster_rejected_by_min_new():
    blocks = compute_blocks(_cluster(3), _GF_CFG)
    assert not blocks[0].is_greenfield


def test_greenfield_other_ratio_rejects_contaminated_block():
    # 12 new + 1 matched = 8% other > 5% → rejected
    blocks = compute_blocks(_cluster(12, n_matched=1), _GF_CFG)
    assert not blocks[0].is_greenfield


def test_greenfield_big_block_tolerates_few_conflicts():
    # 200 new + 6 matched + 4 osm_only = 5% other → greenfield
    blocks = compute_blocks(_cluster(200, n_matched=6, n_osm_only=4), _GF_CFG)
    assert blocks[0].is_greenfield


def test_greenfield_matched_only_block_not_greenfield():
    blocks = compute_blocks(_cluster(0, n_matched=10), _GF_CFG)
    assert not blocks[0].is_greenfield


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


def test_shape_match_pairs_shifted_identical_building():
    # IoU far below near-miss band → both unmatched by IoU; identical shape,
    # displacement within centroid gate + shape_extra_distance_m
    dbsn = [("d1", box(0.98, 0, 1.98, 1))]
    osm = [("w1", box(0, 0, 1, 1))]
    cfg = {**_CFG, "shape_extra_distance_m": 150_000}
    result = match_features(dbsn, osm, cfg)
    assert result.shape_matched == [("d1", "w1")]
    assert result.dbsn_only == []
    assert result.osm_only == []


def test_shape_match_rejects_different_size():
    dbsn = [("d1", box(0.98, 0, 2.98, 2))]  # 4x area of OSM box
    osm = [("w1", box(0, 0, 1, 1))]
    cfg = {**_CFG, "shape_extra_distance_m": 150_000}
    result = match_features(dbsn, osm, cfg)
    assert result.shape_matched == []
    assert result.dbsn_only == ["d1"]
    assert result.osm_only == ["w1"]


# ---------------------------------------------------------------------------
# JOSM remote control URL building
# ---------------------------------------------------------------------------

def test_josm_load_urls_builds_one_url_per_layer(tmp_path):
    from urllib.parse import quote
    pbf = tmp_path / "VE.osm.pbf"
    pbf.touch()
    amb = tmp_path / "VE_ambiguous.osm"
    amb.touch()
    urls = josm_load_urls([("osm-estratti", pbf), ("ambiguous", amb)], host="localhost:8111")
    assert len(urls) == 2
    assert urls[0].startswith("http://localhost:8111/import?")
    assert "layer_name=osm-estratti" in urls[0]
    # expected URL derived from the same resolve() the impl performs — portable
    assert f"url={quote(pbf.resolve().as_uri(), safe='')}" in urls[0]
    assert "new_layer=true" in urls[0]
    assert "layer_name=ambiguous" in urls[1]


def test_josm_load_urls_escapes_layer_name(tmp_path):
    urls = josm_load_urls([("a b&c", tmp_path / "x.osm")], host="h")
    assert "layer_name=a%20b%26c" in urls[0]


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
