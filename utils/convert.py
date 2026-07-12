"""OSM XML and GeoJSON writer utilities."""

import bz2
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from itertools import count
from pathlib import Path


def _safe_tags(raw: dict | None) -> dict:
    if not raw:
        return {}
    return {k: str(v) for k, v in raw.items() if v is not None}


def _translated(src, translate_fn: Callable):
    for feat in src:
        geom = feat["geometry"]
        if geom is None:
            continue
        tags = _safe_tags(translate_fn(dict(feat["properties"])))
        if not tags:
            continue
        # GDAL reads GU_CPSurfaceB3D as MultiPolygon even for simple buildings
        if geom["type"] == "MultiPolygon" and len(geom["coordinates"]) == 1:
            geom = {"type": "Polygon", "coordinates": geom["coordinates"][0]}
        # Polygon with holes → MultiPolygon so Polygon always means simple
        if geom["type"] == "Polygon" and len(geom["coordinates"]) > 1:
            geom = {"type": "MultiPolygon", "coordinates": [geom["coordinates"]]}
        yield geom, tags


def _add_ring(coords: list, node_ids: Iterator[int]) -> tuple[list[ET.Element], list[str]]:
    ring = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
    elems, ids = [], []
    for coord in ring:
        lon, lat = coord[0], coord[1]  # ignore Z if present
        nid = str(next(node_ids))
        elems.append(ET.Element("node", id=nid, lat=f"{lat:.7f}", lon=f"{lon:.7f}", visible="true"))
        ids.append(nid)
    return elems, ids


def _add_way(ring_ids: list[str], tags: dict, way_ids: Iterator[int]) -> ET.Element:
    way = ET.Element("way", id=str(next(way_ids)), visible="true")
    for nid in ring_ids:
        ET.SubElement(way, "nd", ref=nid)
    if ring_ids:
        ET.SubElement(way, "nd", ref=ring_ids[0])  # close the ring
    for k, v in tags.items():
        ET.SubElement(way, "tag", k=k, v=v)
    return way


def _add_area(
    coords: list,
    node_ids: Iterator[int],
    way_ids: Iterator[int],
    tags: dict | None = None,
    rel: ET.Element | None = None,
    role: str | None = None,
) -> tuple[list[ET.Element], ET.Element]:
    node_elems, ring_ids = _add_ring(coords, node_ids)
    way = _add_way(ring_ids, tags or {}, way_ids)
    if rel is not None:
        ET.SubElement(rel, "member", type="way", ref=way.get("id"), role=role)
    return node_elems, way


def _add_area_relation(
    geom: dict,
    tags: dict,
    node_ids: Iterator[int],
    way_ids: Iterator[int],
    rel_ids: Iterator[int],
) -> tuple[list[ET.Element], list[ET.Element], ET.Element]:
    polygons = geom["coordinates"]
    all_nodes: list[ET.Element] = []
    all_ways: list[ET.Element] = []
    rel = ET.Element("relation", id=str(next(rel_ids)), visible="true")
    for poly_coords in polygons:
        ns, w = _add_area(poly_coords[0], node_ids, way_ids, rel=rel, role="outer")
        all_nodes.extend(ns)
        all_ways.append(w)
        for inner_coords in poly_coords[1:]:
            ns, w = _add_area(inner_coords, node_ids, way_ids, rel=rel, role="inner")
            all_nodes.extend(ns)
            all_ways.append(w)
    ET.SubElement(rel, "tag", k="type", v="multipolygon")
    for k, v in tags.items():
        ET.SubElement(rel, "tag", k=k, v=v)
    return all_nodes, all_ways, rel


def write_osm(src, output_path: Path, translate_fn: Callable, compress: bool = False, bounds=None) -> int:
    node_ids = count(-1, -1)
    way_ids = count(-1, -1)
    rel_ids = count(-1, -1)
    nodes: list[ET.Element] = []
    ways: list[ET.Element] = []
    rels: list[ET.Element] = []

    written = 0
    for geom, tags in _translated(src, translate_fn):
        if geom["type"] == "Polygon":
            ns, w = _add_area(geom["coordinates"][0], node_ids, way_ids, tags=tags)
            nodes.extend(ns)
            ways.append(w)
        elif geom["type"] == "MultiPolygon":
            ns, ws, r = _add_area_relation(geom, tags, node_ids, way_ids, rel_ids)
            nodes.extend(ns)
            ways.extend(ws)
            rels.append(r)
        else:
            continue
        written += 1

    osm = ET.Element("osm", version="0.6", generator="dbsn-pipeline")
    if bounds:
        min_lon, min_lat, max_lon, max_lat = bounds
        ET.SubElement(
            osm,
            "bounds",
            minlat=f"{min_lat:.7f}",
            minlon=f"{min_lon:.7f}",
            maxlat=f"{max_lat:.7f}",
            maxlon=f"{max_lon:.7f}",
        )
    for elem in nodes + ways + rels:
        osm.append(elem)

    ET.indent(osm, space="  ")
    data = b"<?xml version='1.0' encoding='UTF-8'?>\n" + ET.tostring(osm, encoding="utf-8")
    if compress:
        with bz2.open(output_path, "wb") as f:
            f.write(data)
    else:
        output_path.write_bytes(data)
    return written


def write_geojson(src, output_path: Path, translate_fn: Callable) -> int:
    fc: dict = {"type": "FeatureCollection", "features": []}
    for geom, tags in _translated(src, translate_fn):
        fc["features"].append({"type": "Feature", "geometry": geom, "properties": tags})
    output_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    return len(fc["features"])
