"""OSM writer: identity nodes (unsafe OSM nodes shown with their identity and tags in review files)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from shapely.geometry import box, mapping

from utils.writers import write_osm_items

SQUARE = box(12.0, 45.0, 12.0001, 45.0001)
CORNER = (12.0001, 45.0)  # a vertex of SQUARE
ENTRANCE = {"entrance": "yes", "note:conflate:unsafe": "detach"}


def _write(path, identity_nodes):
    write_osm_items([(mapping(SQUARE), {"building": "yes"})], path, upload="never", identity_nodes=identity_nodes)
    return ET.parse(path).getroot()


def _tags(elem):
    return {t.get("k"): t.get("v") for t in elem.iter("tag")}


def _at(root, lon, lat):
    return [n for n in root.iter("node") if (float(n.get("lon")), float(n.get("lat"))) == (lon, lat)]


def test_identity_node_away_from_every_vertex_is_written_standalone(tmp_path):
    root = _write(tmp_path / "out.osm", [("n2", 12.00005, 45.00005, ENTRANCE)])

    (node,) = _at(root, 12.00005, 45.00005)
    assert _tags(node)["osm:id"] == "n2"
    assert node.get("id") not in [nd.get("ref") for nd in root.find("way").iter("nd")]


def test_overlapping_identity_nodes_stay_standalone_and_warn(tmp_path, capsys):
    reason = {**ENTRANCE, "note:conflate:unsafe_reason": "tagged"}
    root = _write(tmp_path / "out.osm", [("n2", *CORNER, reason), ("n3", *CORNER, reason)])

    by_identity = {_tags(n).get("osm:id"): n for n in _at(root, *CORNER)}
    assert set(by_identity) == {"n2", "n3", None}  # both identity nodes, plus a plain vertex node
    assert _tags(by_identity["n2"])["note:conflate:unsafe_reason"] == "tagged;overlap:n3"
    assert _tags(by_identity["n3"])["note:conflate:unsafe_reason"] == "tagged;overlap:n2"
    assert by_identity[None].get("id") in [nd.get("ref") for nd in root.find("way").iter("nd")]  # no identity wins
    assert "overlap" in capsys.readouterr().out


def test_identity_node_at_a_vertex_is_the_vertex(tmp_path):
    root = _write(tmp_path / "out.osm", [("n2", *CORNER, ENTRANCE)])

    (node,) = _at(root, *CORNER)  # no plain duplicate at the same place
    assert _tags(node) == {"osm:id": "n2", **ENTRANCE}
    assert node.get("id") in [nd.get("ref") for nd in root.find("way").iter("nd")]
