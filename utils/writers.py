"""OSM and GeoJSON writer utilities."""

import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from itertools import count
from pathlib import Path

import fiona
import osmium

# XML 1.0 forbids control chars except TAB/LF/CR
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]")


_OSM_MAX_TAG = 255


def _safe_tags(raw: dict | None) -> dict:
    if not raw:
        return {}
    return {k: _INVALID_XML.sub("", str(v))[:_OSM_MAX_TAG] for k, v in raw.items() if v is not None}


def _translated(src, translate_fn: Callable, overrides: dict | None = None):
    for feat in src:
        geom = feat["geometry"]
        if geom is None:
            continue
        if overrides:
            cid = feat["properties"].get("classid")
            if cid and cid in overrides:
                geom = overrides[cid]
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


def _add_ring(coords: list, coord_map: defaultdict, writer: osmium.SimpleWriter) -> list[int]:
    ring = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
    ids: list[int] = []
    for coord in ring:
        lon, lat = coord[0], coord[1]  # ignore Z if present
        key = (lon, lat)
        is_new = key not in coord_map
        nid = coord_map[key]
        if is_new:
            writer.add_node(osmium.osm.mutable.Node(id=nid, location=osmium.osm.Location(lon, lat)))
        ids.append(nid)
    return ids


def _add_way(ring_ids: list[int], tags: dict, way_ids: Iterator[int]) -> osmium.osm.mutable.Way:
    return osmium.osm.mutable.Way(id=next(way_ids), nodes=[*ring_ids, ring_ids[0]], tags=tags)


def _add_area(
    coords: list,
    coord_map: defaultdict,
    way_ids: Iterator[int],
    writer: osmium.SimpleWriter,
    tags: dict | None = None,
) -> osmium.osm.mutable.Way | None:
    ring_ids = _add_ring(coords, coord_map, writer)
    if not ring_ids:
        return None
    return _add_way(ring_ids, tags or {}, way_ids)


def _add_area_relation(
    geom: dict,
    tags: dict,
    coord_map: defaultdict,
    way_ids: Iterator[int],
    rel_ids: Iterator[int],
    writer: osmium.SimpleWriter,
) -> tuple[list[osmium.osm.mutable.Way], osmium.osm.mutable.Relation | None] | None:
    all_ways: list[osmium.osm.mutable.Way] = []
    members: list[tuple[str, int, str]] = []
    for poly_coords in geom["coordinates"]:
        w = _add_area(poly_coords[0], coord_map, way_ids, writer)
        if w is None:
            continue
        all_ways.append(w)
        members.append(("w", w.id, "outer"))
        for inner_coords in poly_coords[1:]:
            w = _add_area(inner_coords, coord_map, way_ids, writer)
            if w is None:
                continue
            all_ways.append(w)
            members.append(("w", w.id, "inner"))
    if not members:
        return None
    if len(members) == 1:
        # single outer, no holes — promote to simple way, skip the relation
        way = osmium.osm.mutable.Way(id=all_ways[0].id, nodes=list(all_ways[0].nodes), tags=tags)
        return [way], None
    rel = osmium.osm.mutable.Relation(id=next(rel_ids), members=members, tags={"type": "multipolygon", **tags})
    return all_ways, rel


def write_osm(src, output_path: Path, translate_fn: Callable, bounds=None, overrides: dict | None = None) -> int:
    node_ids = count(-1, -1)
    way_ids = count(-1, -1)
    rel_ids = count(-1, -1)
    coord_map: defaultdict[tuple[float, float], int] = defaultdict(lambda: next(node_ids))

    h = osmium.io.Header()
    if bounds:
        min_lon, min_lat, max_lon, max_lat = bounds
        h.add_box(osmium.osm.Box(osmium.osm.Location(min_lon, min_lat), osmium.osm.Location(max_lon, max_lat)))

    ways: list[osmium.osm.mutable.Way] = []
    rels: list[osmium.osm.mutable.Relation] = []
    written = 0

    with osmium.SimpleWriter(str(output_path), header=h) as writer:
        for geom, tags in _translated(src, translate_fn, overrides):
            if geom["type"] == "Point":
                lon, lat = geom["coordinates"][0], geom["coordinates"][1]
                nid = next(node_ids)
                writer.add_node(osmium.osm.mutable.Node(id=nid, location=osmium.osm.Location(lon, lat), tags=tags))
            elif geom["type"] == "Polygon":
                w = _add_area(geom["coordinates"][0], coord_map, way_ids, writer, tags=tags)
                if w is None:
                    continue
                ways.append(w)
            elif geom["type"] == "MultiPolygon":
                result = _add_area_relation(geom, tags, coord_map, way_ids, rel_ids, writer)
                if result is None:
                    continue
                ws, r = result
                ways.extend(ws)
                if r is not None:
                    rels.append(r)
            else:
                continue
            written += 1
        for w in ways:
            writer.add_way(w)
        for r in rels:
            writer.add_relation(r)

    return written


def write_geojson(
    src, output_path: Path, translate_fn: Callable, schema: dict, crs=None, overrides: dict | None = None
) -> int:
    if crs is None:
        crs = getattr(src, "crs", "EPSG:4326")
    schema_props = set(schema.get("properties", {}).keys())
    written = 0
    with fiona.open(str(output_path), "w", driver="GeoJSON", schema=schema, crs=crs) as dst:
        for geom, tags in _translated(src, translate_fn, overrides):
            props = {**{k: None for k in schema_props}, **tags}
            dst.write({"type": "Feature", "geometry": geom, "properties": props})
            written += 1
    return written
