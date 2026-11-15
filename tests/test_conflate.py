"""Tests for DBSN conflation logic — matching, tag merge."""

from __future__ import annotations

import io
import os
import socket
import xml.etree.ElementTree as ET
from urllib.parse import quote

from shapely.geometry import box

from dbsn import conflate
from dbsn.conflate import (
    JOSM_DEFAULT_HOST,
    apply_new_resolution,
    compute_blocks,
    edited_sibling,
    ensure_edited_copy,
    is_zero_delta,
    josm_available,
    josm_load_urls,
    josm_open_file_url,
    josm_target_provinces,
    resolve_josm_host,
)
from dbsn.match import match_features
from dbsn.tag_merge import merge_tags
from utils.dag import Result


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
# Resolutions applied to features about to be created
# ---------------------------------------------------------------------------

def test_new_resolution_overrides_tags_and_geometry():
    moved = box(1, 1, 2, 2)
    tags, geom = apply_new_resolution(
        {"building": "yes", "height": "5"}, box(0, 0, 1, 1),
        {"tags": {"name": "X", "-height": ""}, "geometry": moved.wkt},
    )
    assert tags == {"building": "yes", "name": "X"}
    assert geom.equals(moved)


def test_new_resolution_without_geometry_keeps_geometry():
    original = box(0, 0, 1, 1)
    _, geom = apply_new_resolution({"building": "yes"}, original, {"tags": {"name": "X"}})
    assert geom is original


# ---------------------------------------------------------------------------
# Hand-edited geometry of an OSM-only way
# ---------------------------------------------------------------------------

def _osm_way_graph(**overrides):
    """Way 1 = a 0.0001° square on nodes 1-4 (closed by 1), no other way touches it."""
    corners = {1: (12.0, 45.0), 2: (12.0001, 45.0), 3: (12.0001, 45.0001), 4: (12.0, 45.0001)}
    graph = dict(
        way_nodes={1: [1, 2, 3, 4, 1]},
        node_coords=corners,
        node_versions={n: 3 for n in corners},
        node_way_count={n: 1 for n in corners},
        tagged_nodes=set(),
        node_to_ways={n: [1] for n in corners},
        way_tags={1: {"building": "yes"}},
        next_node_id=[-1],
    )
    return {**graph, **overrides}


def test_osm_geometry_edit_moves_the_ways_own_nodes_in_place():
    shifted = box(12.00001, 45.0, 12.00011, 45.0001)  # ~1 m east
    outcome = conflate.osm_geometry_edit_osc("w1", 7, {"building": "yes"}, shifted, box(12.0, 45.0, 12.0001, 45.0001),
                                             **_osm_way_graph())

    _, node_modifies, _, way_modify = outcome
    osm_id, version, nd_refs, tags = way_modify
    assert (osm_id, version, tags) == ("w1", 7, {"building": "yes"})
    assert sorted(nd_refs) == [1, 2, 3, 4]  # the way's own nodes: no new ids, history kept (ring may start anywhere)
    assert {n for n, *_ in node_modifies} == {1, 2, 3, 4}


def test_osm_geometry_edit_is_refused_when_a_node_is_tagged():
    shifted = box(12.00001, 45.0, 12.00011, 45.0001)
    outcome = conflate.osm_geometry_edit_osc("w1", 7, {"building": "yes"}, shifted, box(12.0, 45.0, 12.0001, 45.0001),
                                             **_osm_way_graph(tagged_nodes={2}))

    assert outcome is None


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


def test_josm_open_file_url_quotes_path(tmp_path):
    f = tmp_path / "a b" / "VE_new_edited.osm"
    url = josm_open_file_url(f, host="localhost:8111")
    assert url == f"http://localhost:8111/open_file?filename={quote(str(f.resolve()), safe='')}"
    assert " " not in url


def test_edited_sibling_name(tmp_path):
    assert edited_sibling(tmp_path / "VE_conflated.osm") == tmp_path / "VE_conflated_edited.osm"


def test_ensure_edited_copy_is_byte_copy_and_never_overwrites(tmp_path):
    src = tmp_path / "VE_new.osm"
    src.write_bytes(b"<osm version='0.6' upload=\"never\"></osm>")
    edited = ensure_edited_copy(src)
    assert edited == tmp_path / "VE_new_edited.osm"
    assert edited.read_bytes() == src.read_bytes()  # keeps upload="never"
    edited.write_bytes(b"user edits")
    src.write_bytes(b"regenerated")
    assert ensure_edited_copy(src).read_bytes() == b"user edits"


def test_josm_available_false_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not josm_available(f"127.0.0.1:{port}", timeout=0.5)


def test_resolve_josm_host(monkeypatch):
    monkeypatch.setattr("dbsn.conflate.josm_available", lambda host, timeout=1.0: True)
    assert resolve_josm_host(None, no_josm=True) is None
    assert resolve_josm_host("other:9", no_josm=False) == "other:9"
    assert resolve_josm_host(None, no_josm=False) == JOSM_DEFAULT_HOST
    monkeypatch.setattr("dbsn.conflate.josm_available", lambda host, timeout=1.0: False)
    assert resolve_josm_host(None, no_josm=False) is None
    assert resolve_josm_host("other:9", no_josm=False) == "other:9"


def _staleness_setup(tmp_path, monkeypatch, pbf_mtime: int):
    pbf = tmp_path / "VE_d.osm.pbf"
    pbf.write_bytes(b"")
    out = tmp_path / "VE_d_new.osm"
    out.write_text("<osm/>")
    os.utime(pbf, (pbf_mtime, pbf_mtime))
    os.utime(out, (2000, 2000))
    monkeypatch.setattr(conflate, "find_pbf", lambda *a: pbf)
    return out


def test_staleness_reason_newer_pbf(tmp_path, monkeypatch):
    out = _staleness_setup(tmp_path, monkeypatch, pbf_mtime=3000)
    assert "newer" in conflate._staleness_reason("VE", None, [out])


def test_staleness_reason_fresh_or_incomplete(tmp_path, monkeypatch):
    out = _staleness_setup(tmp_path, monkeypatch, pbf_mtime=1000)
    assert conflate._staleness_reason("VE", None, [out]) is None
    assert conflate._staleness_reason("VE", None, [out, tmp_path / "missing.osm"]) is None


def test_slugs_and_output_paths():
    assert conflate._slugs(None, None) == ("", "")
    assert conflate._slugs("Isola dei Petroli", None) == ("_isola_dei_petroli",) * 2
    assert conflate._slugs(None, "Isola dei Petroli") == ("", "_osm-isoladeipetroli")
    assert conflate._slugs("Marghera", "r/6628636") == ("_marghera", "_marghera_osm-r6628636")
    out = conflate._out("VE", "d", "_new.osm", "_osm-x")
    assert out.parent.name == "VE_d_osm-x" and out.name == "VE_d_osm-x_new.osm"


def test_josm_target_provinces_auto_only_ran_explicit_all():
    provs = [{"code": "VE"}, {"code": "TV"}, {"code": "PD"}]
    results = {
        "conflate:VE": Result("conflate:VE", "done"),
        "conflate:TV": Result("conflate:TV", "skipped"),
    }
    assert josm_target_provinces(provs, results, explicit=False) == [{"code": "VE"}]
    assert josm_target_provinces(provs, results, explicit=True) == provs


def _josm_setup(tmp_path, monkeypatch, edited_older: bool):
    """VE_new.osm + VE_new_edited.osm in tmp_path; urlopen stubbed to record URLs."""
    orig = tmp_path / "VE_new.osm"
    orig.write_text("<osm><node id='-1' lat='45' lon='12'/></osm>")
    edited = tmp_path / "VE_new_edited.osm"
    edited.write_text("user edits")
    os.utime(orig, (2000, 2000))
    os.utime(edited, (1000, 1000) if edited_older else (3000, 3000))
    monkeypatch.setattr(conflate, "_out", lambda code, date, suffix, *a: tmp_path / f"VE{suffix}")
    monkeypatch.setattr(conflate, "find_pbf", lambda *a: None)
    opened = []
    monkeypatch.setattr(conflate.urllib.request, "urlopen",
                        lambda url, timeout: opened.append(url) or io.BytesIO())
    return edited, opened


def test_josm_open_warns_when_edited_older_than_original(tmp_path, monkeypatch, capsys):
    edited, opened = _josm_setup(tmp_path, monkeypatch, edited_older=True)
    conflate._josm_open([{"code": "VE", "date": "d"}], None, None, "h:1")
    assert "older" in capsys.readouterr().out
    assert edited.read_text() == "user edits"  # never overwritten
    assert opened == [josm_open_file_url(edited, "h:1")]


def test_josm_open_skips_review_files_without_data(tmp_path, monkeypatch):
    edited, opened = _josm_setup(tmp_path, monkeypatch, edited_older=False)
    empty = tmp_path / "VE_osm_only.osm"
    empty.write_text("<osm version='0.6' upload=\"never\">\n</osm>")
    conflate._josm_open([{"code": "VE", "date": "d"}], None, None, "h:1")
    assert not edited_sibling(empty).exists()  # no copy made for an empty layer
    assert opened == [josm_open_file_url(edited, "h:1")]  # only the layer with data


def test_josm_open_silent_when_edited_is_current(tmp_path, monkeypatch, capsys):
    _josm_setup(tmp_path, monkeypatch, edited_older=False)
    conflate._josm_open([{"code": "VE", "date": "d"}], None, None, "h:1")
    assert "older" not in capsys.readouterr().out


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


def test_osc_tag_only_way_modify_keeps_the_way_nodes(tmp_path):
    # an osmChange way modify replaces the whole way: without its <nd> refs the API rejects it
    osc = tmp_path / "x.osc"
    conflate._osc_write(osc, [], [("w7", 3, {"building": "yes", "name": "X"})], [],
                        way_nodes={7: [10, 11, 12, 10]})

    way = ET.parse(osc).getroot().find("modify/way")

    assert [nd.get("ref") for nd in way.findall("nd")] == ["10", "11", "12", "10"]
    assert {t.get("k"): t.get("v") for t in way.findall("tag")} == {"building": "yes", "name": "X"}


def test_osc_source_kept_when_osm_geometry_kept_and_dropped_when_geometry_applied(tmp_path):
    tags = {"building": "yes", "source": "survey", "ref:dbsn": "A"}
    osc = tmp_path / "x.osc"
    conflate._osc_write(
        osc, [(tags, box(0, 0, 1e-4, 1e-4))], [("w7", 3, tags)], [],
        geom_way_modifies=[("w8", 2, [20, 21, 22], tags)], way_nodes={7: [10, 11, 12, 10]},
    )

    root = ET.parse(osc).getroot()

    assert {t.get("k") for t in root.find("create/way").findall("tag")} == {"building"}  # DBSN geometry: no source
    geom_way, tag_way = root.findall("modify/way")  # <modify> writes geometry updates first
    assert {t.get("k") for t in geom_way.findall("tag")} == {"building"}
    assert {t.get("k"): t.get("v") for t in tag_way.findall("tag")} == {"building": "yes", "source": "survey"}


def test_osm_source_kept_ignores_the_dbsn_source():
    assert conflate._with_osm_source({"building": "yes", "source": "DBSN"}, {"building": "yes"}) == {"building": "yes"}
    assert conflate._with_osm_source({"building": "yes", "source": "DBSN"}, {"source": "survey"}) == {
        "building": "yes", "source": "survey"}


def test_changeset_text_counts_and_deduplicated_source_with_region_named():
    text = conflate.changeset_text(55, 1, 0, ["DBSN;IGM;Regione", "DBSN;IGM;Regione", "Comune;DBSN"], {}, "Veneto")
    assert text == "comment=Added 55 features, updated 1 feature\nsource=DBSN;IGM;Regione Veneto;Comune\n"


def test_changeset_text_reviewed_import_counts_what_the_resolutions_did():
    resolutions = {
        "dbsn:a": {"decision": "skip"}, "dbsn:b": {"decision": "skip"},
        "dbsn:c": {"geometry": "POLYGON EMPTY"}, "dbsn:d": {"tags": {"name": "X"}},
        "w1": {"decision": "delete", "dbsn_classid": "e"}, "w2": {"decision": "keep_osm", "dbsn_classid": "f"},
        "w3": {"decision": "replace", "dbsn_classid": "g"},
    }
    text = conflate.changeset_text(0, 2, 1, [], resolutions)
    assert text == ("comment=Reviewed import: updated 2 features, deleted 1 feature "
                    "(2 skipped, 2 reshaped, 1 deleted, 1 kept OSM)\n")


def test_changeset_text_reviewed_import_without_countable_resolutions_is_prefix_only():
    text = conflate.changeset_text(3, 0, 0, [], {"w3": {"decision": "replace", "dbsn_classid": "g"}})
    assert text == "comment=Reviewed import: added 3 features\n"


def test_merge_differing_source_is_no_conflict():
    # `source` never reaches the osmChange, so differing values must not block the pair
    merged, conflicts = merge_tags(
        dbsn_tags={"building": "yes", "source": "DBSN;IGM"},
        osm_tags={"building": "yes", "source": "Ministero"},
        specificity=_SPEC,
    )
    assert conflicts == {}
    assert merged["source"] == "Ministero"


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
