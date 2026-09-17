"""Step 5: Conflate DBSN output against existing OSM features per province."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import osmium
import osmium.geom
import yaml
from scipy.optimize import linear_sum_assignment
from shapely import STRtree, from_wkb
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry import mapping as shapely_mapping
from shapely.ops import unary_union

from dbsn.common import (
    CONFLATE_DIR,
    OSM_DIR,
    Province,
    add_area_arg,
    add_max_weight_arg,
    load_area_clip,
    parse_args,
    parse_overwrite,
    read_sources,
)
from dbsn.convert import ConvertTask
from dbsn.download_pbf import DownloadPbfTask, find_pbf
from dbsn.download_pbf import _normalize as _pbf_normalize
from dbsn.match import match_features
from dbsn.tag_merge import merge_tags
from utils.dag import Task, run_dag
from utils.writers import write_osm_items

_HERE = Path(__file__).parent
_SPECIFICITY_FILE = _HERE / "tag_specificity.yaml"
_CONFIG_FILE = _HERE / "conflate_config.yaml"

def josm_load_urls(layers: list[tuple[str, Path]], host: str) -> list[str]:
    """Remote-control URLs that import each (layer_name, path) as a new JOSM layer.

    Uses the /import endpoint (verified against JOSM 19613: it accepts file://
    URLs; /load_data wants inline data= and rejects file URLs).
    host includes the port, e.g. "localhost:8111".
    """
    from urllib.parse import quote, urlencode
    urls = []
    for name, path in layers:
        params = urlencode({
            "new_layer": "true",
            "layer_name": name,
            "url": Path(path).resolve().as_uri(),
        }, quote_via=quote)
        urls.append(f"http://{host}/import?{params}")
    return urls


def is_zero_delta(
    iou: float,
    merged: dict,
    osm_tags: dict,
    identical_iou: float = 0.95,
) -> bool:
    """True when a 1:1 match adds nothing: high IoU and merged tags equal OSM's.

    These matches are noise for review and are dropped from _conflated.osm.
    """
    return iou >= identical_iou and merged == osm_tags


def _load_specificity() -> dict[str, list[str]]:
    with _SPECIFICITY_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _relevant_kv(specificity: dict[str, list[str]]) -> frozenset[tuple[str, str]]:
    """Build a (key, value) set of all tag combinations DBSN can produce."""
    return frozenset((k, v) for k, vals in specificity.items() for v in vals)


def _load_config(code: str, region: str) -> dict:
    with _CONFIG_FILE.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = dict(raw.get("defaults", {}))
    cfg.update(raw.get("regions", {}).get(region, {}))
    cfg.update(raw.get("provinces", {}).get(code, {}))
    return cfg


# ---------------------------------------------------------------------------
# OSM tag normalisation (applied before merge/output)
# ---------------------------------------------------------------------------

_DROPPABLE_SOURCES = frozenset({"bing", "Bing"})
_DROPPABLE_NOTES = frozenset({"verificare se man_made=works"})


def _normalise_osm_tags(tags: dict) -> dict:
    t = dict(tags)
    if t.get("source") in _DROPPABLE_SOURCES:
        del t["source"]
    if t.get("note") in _DROPPABLE_NOTES:
        del t["note"]
    if "building:use" in t:
        if t.get("building") == "yes":
            t["building"] = t.pop("building:use")
        elif t.get("building") == t["building:use"]:
            del t["building:use"]
    if t.get("building") == "yes" and "shop" in t:
        t["building"] = "retail"
    return t


# ---------------------------------------------------------------------------
# OSM / PBF parsing
# ---------------------------------------------------------------------------

class _AreaHandler(osmium.SimpleHandler):
    """Collect area features with relevant tags from a PBF or OSM file."""

    def __init__(self, clip=None, relevant_kv: frozenset[tuple[str, str]] = frozenset()):
        super().__init__()
        self.wkbfab = osmium.geom.WKBFactory()
        self.features: list[tuple[str, dict, Any, int]] = []  # (feat_id, tags, geom, version)
        self.part_features: list[tuple[str, dict, Any, int]] = []  # building:part only
        self._clip = clip
        self._relevant_kv = relevant_kv
        self.node_way_count: dict[int, int] = {}
        self.tagged_nodes: set[int] = set()
        self.way_nodes: dict[int, list[int]] = {}   # way_id → [node_refs]
        self.way_tags: dict[int, dict] = {}         # way_id → tags (all ways)
        self.node_coords: dict[int, tuple[float, float]] = {}  # node_id → (lon, lat)
        self.node_versions: dict[int, int] = {}     # node_id → version

    def node(self, n):
        self.node_versions[n.id] = n.version
        if n.tags:
            self.tagged_nodes.add(n.id)

    def way(self, w):
        refs = []
        for nd in w.nodes:
            refs.append(nd.ref)
            if nd.location.valid():
                self.node_coords[nd.ref] = (nd.location.lon, nd.location.lat)
        self.way_nodes[w.id] = refs
        self.way_tags[w.id] = dict(w.tags)
        for ref in refs:
            self.node_way_count[ref] = self.node_way_count.get(ref, 0) + 1

    def area(self, a):
        tags = dict(a.tags)
        has_building = "building" in tags
        has_part = not has_building and "building:part" in tags
        if not has_building and not has_part and not any((k, v) in self._relevant_kv for k, v in tags.items()):
            return
        try:
            wkb = self.wkbfab.create_multipolygon(a)
            geom = from_wkb(bytes.fromhex(wkb))
        except Exception:
            return
        if geom is None or geom.is_empty:
            return
        if self._clip is not None and not self._clip.intersects(geom):
            return
        fid = f"w{a.orig_id()}" if a.from_way() else f"r{a.orig_id()}"
        if has_part:
            self.part_features.append((fid, tags, geom, a.version))
        else:
            self.features.append((fid, tags, geom, a.version))


def _parse_dbsn(osm_path: Path, clip=None) -> list[tuple[str, dict, Any]]:
    """Parse DBSN output OSM file via ElementTree.

    osmium location storage doesn't resolve negative node IDs even with explicit lat/lon,
    so we parse the XML directly.
    """
    tree = ET.parse(str(osm_path))
    root = tree.getroot()

    nodes: dict[int, tuple[float, float]] = {}  # id → (lon, lat)
    for elem in root.iter("node"):
        nid = int(elem.get("id"))
        nodes[nid] = (float(elem.get("lon")), float(elem.get("lat")))

    features: list[tuple[str, dict, Any]] = []
    for elem in root.iter("way"):
        way_id = int(elem.get("id"))
        tags = {t.get("k"): t.get("v") for t in elem.iter("tag")}
        if not tags:
            continue
        refs = [int(nd.get("ref")) for nd in elem.iter("nd")]
        if len(refs) < 4 or refs[0] != refs[-1]:
            continue  # not a closed ring
        try:
            coords = [nodes[r] for r in refs]
        except KeyError:
            continue
        geom: Any = Polygon(coords)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            continue
        if clip is not None and not clip.intersects(geom):
            continue
        fid = tags.get("ref:dbsn") or f"w{way_id}"
        features.append((fid, tags, geom))

    return features


def _parse_osm(pbf_path: Path, clip=None, relevant_kv: frozenset[tuple[str, str]] = frozenset()) -> tuple[
    list[tuple[str, dict, Any, int]],  # (feat_id, tags, geom, version) — building=* features
    dict[int, int],                    # node_way_count
    set[int],                          # tagged_node_ids
    dict[int, list[int]],              # way_nodes: way_id → [node_refs]
    dict[int, dict],                   # way_tags: way_id → tags (all ways)
    dict[int, tuple[float, float]],    # node_coords: node_id → (lon, lat)
    dict[int, int],                    # node_versions: node_id → version
    list[tuple[str, dict, Any, int]],  # part_features — building:part=* only (no building=*)
]:
    h = _AreaHandler(clip=clip, relevant_kv=relevant_kv)
    h.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    return (h.features, h.node_way_count, h.tagged_nodes,
            h.way_nodes, h.way_tags, h.node_coords, h.node_versions, h.part_features)


# ---------------------------------------------------------------------------
# Geometry safety check for geometry replacement
# ---------------------------------------------------------------------------

# Shared nodes with these key types block auto-replacement: topology matters for routing/hydro/rail.
_UNSAFE_ADJACENT_KEYS = frozenset({"highway", "waterway", "railway", "natural", "public_transport", "route"})


def _way_nodes_safe(
    osm_feat_id: str,
    way_nodes: dict[int, list[int]],
    node_way_count: dict[int, int],
    tagged_nodes: set[int],
    node_to_ways: dict[int, list[int]],
    way_tags: dict[int, dict],
) -> bool:
    """True if geometry replacement is safe.

    Unsafe if: any node is a tagged POI, OR any shared node belongs to a
    highway/waterway/railway/natural way.  Shared nodes with other buildings,
    barriers, landuse areas, etc. are considered safe — they will be broken by
    the import but do not affect routing or network topology.
    """
    if not osm_feat_id.startswith("w"):
        return False  # relations: never safe to auto-replace
    try:
        way_id = int(osm_feat_id[1:])
    except ValueError:
        return False
    refs = way_nodes.get(way_id)
    if refs is None:
        return False
    for ref in refs:
        if ref in tagged_nodes:
            return False
        if node_way_count.get(ref, 0) <= 1:
            continue
        for adj_wid in node_to_ways.get(ref, []):
            if adj_wid == way_id:
                continue
            if any(k in _UNSAFE_ADJACENT_KEYS for k in way_tags.get(adj_wid, {})):
                return False
    return True


_M_PER_DEG = 111_000.0  # good enough for Italy


def _update_geometry_osc(
    osm_id: str,
    osm_version: int,
    dbsn_geom,
    osm_geom,
    merged_tags: dict,
    way_nodes: dict[int, list[int]],
    node_coords: dict[int, tuple[float, float]],
    node_versions: dict[int, int],
    node_way_count: dict[int, int],
    tagged_nodes: set[int],
    node_to_ways: dict[int, list[int]],
    way_tags: dict[int, dict],
    next_node_id: list[int],  # mutable shared counter; decrements with each new node
) -> tuple[
    list[tuple[int, float, float]],         # node creates: (id, lon, lat)
    list[tuple[int, int, float, float]],    # node modifies: (node_id, version, lon, lat)
    list[tuple[int, int]],                  # node deletes: (node_id, version)
    tuple[str, int, list[int], dict],       # way modify: (osm_id, version, nd_refs, tags)
] | None:
    """Match DBSN vertices to OSM nodes (Hungarian assignment) and produce in-place geometry update.

    Returns None if the way's node refs are missing from node_coords (can't update).
    """
    way_id = int(osm_id[1:])
    osm_refs = way_nodes.get(way_id, [])
    if not osm_refs:
        return None

    if isinstance(dbsn_geom, MultiPolygon):
        dbsn_geom = max(dbsn_geom.geoms, key=lambda g: g.area)
    dbsn_coords: list[tuple[float, float]] = list(dbsn_geom.exterior.coords)[:-1]

    # Closed OSM ring → strip duplicate tail; keep only refs with known coordinates
    raw_ring = osm_refs[:-1] if osm_refs[0] == osm_refs[-1] else list(osm_refs)
    osm_ring = [r for r in raw_ring if r in node_coords]
    if not osm_ring:
        return None

    M, N = len(dbsn_coords), len(osm_ring)

    centroid_dist_m = dbsn_geom.centroid.distance(osm_geom.centroid) * _M_PER_DEG
    b = dbsn_geom.bounds
    bbox_diag_m = ((b[2] - b[0]) ** 2 + (b[3] - b[1]) ** 2) ** 0.5 * _M_PER_DEG
    threshold_m = max(1.0, min(15.0, max(centroid_dist_m * 0.5, bbox_diag_m * 0.1)))

    # MxN cost matrix padded to square for linear_sum_assignment
    size = max(M, N)
    cost = np.full((size, size), 1e9)
    osm_pos = [node_coords[r] for r in osm_ring]
    for di, (dlon, dlat) in enumerate(dbsn_coords):
        for ni, (olon, olat) in enumerate(osm_pos):
            dx, dy = (dlon - olon) * _M_PER_DEG, (dlat - olat) * _M_PER_DEG
            cost[di, ni] = (dx * dx + dy * dy) ** 0.5

    row_ind, col_ind = linear_sum_assignment(cost)

    node_creates: list[tuple[int, float, float]] = []
    node_modifies: list[tuple[int, int, float, float]] = []
    matched_dbsn: set[int] = set()
    matched_osm: set[int] = set()
    nd_refs: dict[int, int] = {}  # di → node_id

    def alloc(lon, lat) -> int:
        nid = next_node_id[0]
        next_node_id[0] -= 1
        node_creates.append((nid, lon, lat))
        return nid

    for di, ni in zip(row_ind, col_ind, strict=False):
        if di >= M or ni >= N or cost[di, ni] > threshold_m:
            continue
        matched_dbsn.add(di)
        matched_osm.add(ni)
        dlon, dlat = dbsn_coords[di]
        ref = osm_ring[ni]
        unsafe = any(
            adj != way_id and any(k in _UNSAFE_ADJACENT_KEYS for k in way_tags.get(adj, {}))
            for adj in node_to_ways.get(ref, [])
        )
        if unsafe:
            nd_refs[di] = alloc(dlon, dlat)
        else:
            node_modifies.append((ref, node_versions.get(ref, 1), dlon, dlat))
            nd_refs[di] = ref

    for di in range(M):
        if di not in matched_dbsn:
            nd_refs[di] = alloc(*dbsn_coords[di])

    node_deletes: list[tuple[int, int]] = []
    for ni in range(N):
        if ni not in matched_osm:
            ref = osm_ring[ni]
            if node_way_count.get(ref, 0) <= 1 and ref not in tagged_nodes:
                node_deletes.append((ref, node_versions.get(ref, 1)))

    return (
        node_creates,
        node_modifies,
        node_deletes,
        (osm_id, osm_version, [nd_refs[di] for di in range(M)], merged_tags),
    )


# ---------------------------------------------------------------------------
# osmChange writing
# ---------------------------------------------------------------------------

_OSC_STRIP = frozenset({"osm:id", "ref:dbsn", "source", "note:conflate", "note:conflate:geometry",
                        "note:conflate:shared_node_ids"})


def _osc_tags(tags: dict) -> dict:
    return {k: v for k, v in tags.items()
            if k not in _OSC_STRIP and not k.startswith("note:conflate:") and not k.startswith("-")}


def _apply_overrides(base: dict, overrides: dict) -> dict:
    """Merge overrides into base; keys starting with '-' delete the corresponding base key."""
    result = dict(base)
    for k, v in overrides.items():
        if k.startswith("-"):
            result.pop(k[1:], None)
        else:
            result[k] = v
    return result


def _way_nd_refs(osm_id: str, way_nodes: dict[int, list[int]]) -> list[int] | None:
    """Return nd refs for a way (without closing node), or None for relations/missing."""
    if not osm_id.startswith("w"):
        return None
    refs = way_nodes.get(int(osm_id[1:]))
    if not refs:
        return None
    # way_nodes includes closing node; _osc_write re-closes via nd_refs[0]
    return refs[:-1] if refs[-1] == refs[0] else refs


def _osc_write(
    path: Path,
    way_creates: list[tuple[dict, Any]],         # (tags, geom) — new building features
    way_tag_modifies: list[tuple[str, int, dict]], # (osm_id, version, tags) — tag-only
    way_deletes: list[tuple[str, int]],           # (osm_id, version)
    node_creates: list[tuple[int, float, float]] = (),   # (id, lon, lat) — from geom updates
    node_modifies: list[tuple[int, int, float, float]] = (),  # (node_id, version, lon, lat)
    node_deletes: list[tuple[int, int]] = (),     # (node_id, version) — isolated removed nodes
    geom_way_modifies: list[tuple[str, int, list[int], dict]] = (),  # (osm_id, ver, nd_refs, tags)
    next_node_id: list[int] | None = None,        # counter for new building-create node IDs
) -> None:
    if next_node_id is None:
        next_node_id = [-1]
    root = ET.Element("osmChange", attrib={"version": "0.6"})

    # --- create ---
    has_creates = way_creates or node_creates
    if has_creates:
        create_el = ET.SubElement(root, "create")
        # Geometry-update new nodes first (IDs already assigned)
        for nid, lon, lat in node_creates:
            ET.SubElement(create_el, "node", attrib={
                "id": str(nid), "lat": f"{lat:.7f}", "lon": f"{lon:.7f}",
                "version": "1", "action": "create",
            })
        # New building features (nodes + ways)
        way_id_counter = [-1]
        for tags, geom in way_creates:
            coords = _geom_to_exterior_coords(geom)
            if not coords:
                continue
            nd_ids: list[str] = []
            for lon, lat in coords:
                nid = next_node_id[0]
                next_node_id[0] -= 1
                ET.SubElement(create_el, "node", attrib={
                    "id": str(nid), "lat": f"{lat:.7f}", "lon": f"{lon:.7f}",
                    "version": "1", "action": "create",
                })
                nd_ids.append(str(nid))
            w = ET.SubElement(create_el, "way", attrib={
                "id": str(way_id_counter[0]), "version": "1", "action": "create",
            })
            way_id_counter[0] -= 1
            for ref in [*nd_ids, nd_ids[0]]:  # close ring
                ET.SubElement(w, "nd", attrib={"ref": ref})
            for k, v in _osc_tags(tags).items():
                ET.SubElement(w, "tag", attrib={"k": k, "v": str(v)})

    # --- modify ---
    has_modifies = way_tag_modifies or node_modifies or geom_way_modifies
    if has_modifies:
        modify_el = ET.SubElement(root, "modify")
        # Node moves first so way nd-refs resolve correctly
        for node_id, version, lon, lat in node_modifies:
            ET.SubElement(modify_el, "node", attrib={
                "id": str(node_id), "version": str(version), "action": "modify",
                "lat": f"{lat:.7f}", "lon": f"{lon:.7f}",
            })
        # Way geometry updates (nd refs + tags)
        for osm_id, version, nd_refs, tags in geom_way_modifies:
            raw_id = osm_id[1:]
            w = ET.SubElement(modify_el, "way", attrib={
                "id": raw_id, "version": str(version), "action": "modify",
            })
            for ref in [*nd_refs, nd_refs[0]]:  # close ring
                ET.SubElement(w, "nd", attrib={"ref": str(ref)})
            for k, v in _osc_tags(tags).items():
                ET.SubElement(w, "tag", attrib={"k": k, "v": str(v)})
        # Tag-only way modifies
        for osm_id, version, tags in way_tag_modifies:
            osm_type = "relation" if osm_id.startswith("r") else "way"
            el = ET.SubElement(modify_el, osm_type, attrib={
                "id": osm_id[1:], "version": str(version), "action": "modify",
            })
            for k, v in _osc_tags(tags).items():
                ET.SubElement(el, "tag", attrib={"k": k, "v": str(v)})

    # --- delete ---
    has_deletes = way_deletes or node_deletes
    if has_deletes:
        delete_el = ET.SubElement(root, "delete", attrib={"if-unused": "true"})
        for osm_id, version in way_deletes:
            osm_type = "relation" if osm_id.startswith("r") else "way"
            ET.SubElement(delete_el, osm_type, attrib={
                "id": osm_id[1:], "version": str(version), "action": "delete",
            })
        for node_id, version in node_deletes:
            ET.SubElement(delete_el, "node", attrib={
                "id": str(node_id), "version": str(version), "action": "delete",
            })

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    with path.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode")


def _geom_to_exterior_coords(geom) -> list[tuple[float, float]]:
    """Return exterior ring coordinates (without closing duplicate) from a Shapely geometry."""
    if isinstance(geom, Polygon):
        return list(geom.exterior.coords)[:-1]
    if isinstance(geom, MultiPolygon):
        largest = max(geom.geoms, key=lambda g: g.area)
        return list(largest.exterior.coords)[:-1]
    return []


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------

def _conflate_dir(code: str, date: str, area_slug: str = "") -> Path:
    return CONFLATE_DIR / f"{code}_{date}{area_slug}"


def _out(code: str, date: str, suffix: str, area_slug: str = "") -> Path:
    stem = f"{code}_{date}{area_slug}"
    return _conflate_dir(code, date, area_slug) / f"{stem}{suffix}"


# ---------------------------------------------------------------------------
# Core conflation run
# ---------------------------------------------------------------------------

def _run(
    p: Province,
    overwrite_conflate: bool,
    area: str | None,
    config: dict,
    specificity: dict[str, list[str]],
    review_identical: bool = False,
) -> None:
    code, date = p["code"], p["date"]
    area_slug = f"_{area.lower().replace(' ', '_')}" if area else ""
    out_dir = _conflate_dir(code, date, area_slug)

    # Find DBSN OSM output (newest of .osm / .osm.bz2)
    candidates = [
        OSM_DIR / f"{code}_{date}{area_slug}.osm",
        OSM_DIR / f"{code}_{date}{area_slug}.osm.bz2",
    ]
    existing = [c for c in candidates if c.exists()]
    if not existing:
        print(f"  [skip   ] {code}: convert output not found, run dbsn-convert first", file=sys.stderr)
        return
    dbsn_osm = max(existing, key=lambda c: c.stat().st_mtime)

    # Check if outputs already exist
    outputs = [_out(code, date, s, area_slug) for s in ("_new.osm", "_osm_only.osm", "_conflated.osm",
                                                          "_ambiguous.osm", "_changes.osc")]
    if not overwrite_conflate and all(o.exists() for o in outputs):
        size = sum(o.stat().st_size for o in outputs) // 1024
        print(f"  [skip   ] {code}: conflation outputs exist ({size} KB, use --overwrite conflate)")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    clip = load_area_clip(p, area) if area else None

    print(f"  [parse  ] {code}: reading DBSN features from {dbsn_osm.name}")
    dbsn_features = _parse_dbsn(dbsn_osm, clip=clip)
    print(f"  [parse  ] {code}: {len(dbsn_features)} DBSN features")

    pbf_slug = _pbf_normalize(area) if area else ""
    pbf = find_pbf(code, pbf_slug)
    if pbf is None:
        print(f"  [skip   ] {code}: PBF not found, run dbsn-conflate --download first",
              file=sys.stderr)
        return

    print(f"  [parse  ] {code}: reading OSM features from {pbf.name}")
    (osm_raw, node_way_count, tagged_nodes, way_nodes,
     way_tags, node_coords, node_versions, part_features_raw) = _parse_osm(
        pbf, clip=clip, relevant_kv=_relevant_kv(specificity)
    )
    print(f"  [parse  ] {code}: {len(osm_raw)} OSM area features, {len(part_features_raw)} building:part features")

    # Reverse index for adjacency checks: node_id → [way_ids]
    node_to_ways: dict[int, list[int]] = {}
    for wid, refs in way_nodes.items():
        for ref in refs:
            node_to_ways.setdefault(ref, []).append(wid)

    # Load existing resolutions
    resolutions_path = _out(code, date, "_resolutions.json", area_slug)
    resolutions: dict = {}
    if resolutions_path.exists():
        resolutions = json.loads(resolutions_path.read_text(encoding="utf-8"))

    # Apply already-resolved decisions: skip those features from matching
    resolved_osm_ids = set(resolutions)
    resolved_dbsn_ids: set[str] = set()
    skipped_dbsn_ids: set[str] = set()
    new_tag_overrides: dict[str, dict] = {}  # dbsn_id → tag overrides for new features
    for key, res in resolutions.items():
        decision = res.get("decision")
        if key.startswith("dbsn:"):
            dbsn_id_key = key.removeprefix("dbsn:")
            if decision == "skip":
                skipped_dbsn_ids.add(dbsn_id_key)
            if res.get("tags"):
                new_tag_overrides[dbsn_id_key] = res["tags"]
        cid = res.get("dbsn_classid") or res.get("dbsn_id")
        if cid:
            resolved_dbsn_ids.add(cid)

    dbsn_input = [(fid, geom) for fid, tags, geom in dbsn_features
                  if fid not in resolved_dbsn_ids and fid not in skipped_dbsn_ids]
    osm_by_id = {fid: (_normalise_osm_tags(tags), geom, version) for fid, tags, geom, version in osm_raw}
    osm_input = [(fid, geom) for fid, (tags, geom, version) in osm_by_id.items()
                 if fid not in resolved_osm_ids]

    dbsn_tags_by_id = {fid: tags for fid, tags, geom in dbsn_features}

    print(f"  [match  ] {code}: matching {len(dbsn_input)} DBSN vs {len(osm_input)} OSM features")
    result = match_features(dbsn_input, osm_input, config)

    if result.detected_offset:
        dx, dy, dm = result.detected_offset
        print(f"  [offset ] {code}: detected {dm:.1f} m offset "
              f"(Δlat={dy*111000:.1f} m, Δlon={dx*111000:.1f} m), re-matched")

    # Fast geometry lookup (avoids O(n) next() per matched feature)
    dbsn_geom_by_id = {fid: geom for fid, _, geom in dbsn_features}

    # Detect N:1 merges: DBSN features in dbsn_only that are adjacent to a matched
    # DBSN feature whose OSM counterpart has a multi-unit building tag.
    # Those DBSN features are merged (unary_union) into the matched geometry instead
    # of being emitted as new creates.
    _MULTI_UNIT = frozenset(["semidetached", "semidetached_house", "terrace", "row"])
    _TOUCH_DEG = 2.0 / 111_000  # ~2 m tolerance for "adjacent" (DBSN may have small gaps)
    # Index only matched DBSN features that pair with a multi-unit OSM tag
    multi_unit_pairs = [
        (did, oid) for did, oid in result.matched
        if osm_by_id[oid][0].get("building", "") in _MULTI_UNIT
    ]
    merge_extras: dict[str, list[str]] = {}  # osm_id → [extra dbsn_ids absorbed into it]
    remaining_dbsn_only: list[str] = []
    if multi_unit_pairs:
        mu_geoms = [dbsn_geom_by_id[did].buffer(_TOUCH_DEG) for did, _ in multi_unit_pairs]
        mu_tree = STRtree(mu_geoms)
        for dbsn_id in result.dbsn_only:
            dg = dbsn_geom_by_id[dbsn_id]
            hits = mu_tree.query(dg)
            if hits.size:
                # Pick the closest multi-unit matched feature
                best = min(hits, key=lambda i: dg.distance(dbsn_geom_by_id[multi_unit_pairs[i][0]]))
                matched_osm_id = multi_unit_pairs[best][1]
                merge_extras.setdefault(matched_osm_id, []).append(dbsn_id)
            else:
                remaining_dbsn_only.append(dbsn_id)
    else:
        remaining_dbsn_only = list(result.dbsn_only)
    result.dbsn_only = remaining_dbsn_only
    if merge_extras:
        print(f"  [merge  ] {code}: {sum(len(v) for v in merge_extras.values())} "
              f"DBSN units merged into {len(merge_extras)} multi-unit OSM features")

    # Buckets for output writing
    new_features: list[tuple[dict, Any]] = []       # (tags, geom) for _new.osm; also OSC <create>
    osm_only: list[tuple[dict, Any]] = []           # (tags, geom) for _osm_only.osm
    conflated: list[tuple[dict, Any]] = []          # (tags, geom) for _conflated.osm
    ambiguous: list[tuple[dict, Any]] = []          # (tags, geom) for _ambiguous.osm
    osc_replace_creates: list[tuple[dict, Any]] = []  # resolved-replace creates (not in _new.osm)
    osc_way_tag_modifies: list[tuple[str, int, dict]] = []
    osc_way_deletes: list[tuple[str, int]] = []
    osc_node_creates: list[tuple[int, float, float]] = []   # (id, lon, lat)
    osc_node_modifies: list[tuple[int, int, float, float]] = []  # (node_id, ver, lon, lat)
    osc_node_deletes: list[tuple[int, int]] = []             # (node_id, ver)
    osc_geom_modifies: list[tuple[str, int, list[int], dict]] = []  # (osm_id, ver, nd_refs, tags)
    next_node_id = [-1]  # shared counter; geometry updates decrement first
    n_identical = 0  # zero-delta matches dropped from review

    def _shared_node_info(osm_id: str) -> tuple[int, str]:
        """Return (count, comma-separated ids) of shared nodes for an OSM way."""
        try:
            wid = int(osm_id[1:])
            refs = [r for r in way_nodes.get(wid, []) if node_way_count.get(r, 0) > 1]
            return len(refs), ",".join(str(r) for r in refs)
        except (ValueError, TypeError):
            return 0, ""

    # 1:1 matches
    for dbsn_id, osm_id in result.matched:
        dbsn_tags = dbsn_tags_by_id[dbsn_id]
        osm_tags, osm_geom, osm_version = osm_by_id[osm_id]

        # If extra DBSN units were absorbed, merge their geometries
        extra_ids = merge_extras.get(osm_id, [])
        if extra_ids:
            dbsn_geom = unary_union([dbsn_geom_by_id[dbsn_id]] + [dbsn_geom_by_id[e] for e in extra_ids])
            merge_note = f",merged={'|'.join(extra_ids)}"
        else:
            dbsn_geom = dbsn_geom_by_id[dbsn_id]
            merge_note = ""

        merged, conflicts = merge_tags(dbsn_tags, osm_tags, specificity)
        merged = _normalise_osm_tags(merged)

        # Zero-delta match: DBSN adds nothing (tags identical, near-identical geometry).
        # Dropped from review; --review-identical keeps them in _conflated.osm.
        if not review_identical and not conflicts and not merge_note \
                and is_zero_delta(result.matched_ious.get((dbsn_id, osm_id), 0.0), merged, osm_tags,
                                  identical_iou=config.get("identical_iou", 0.95)):
            n_identical += 1
            continue

        review_tags = {**merged, "ref:dbsn": dbsn_id, "note:conflate": f"osm={osm_id}{merge_note}"}

        if conflicts:
            for key, (ov, dv) in conflicts.items():
                review_tags[f"note:conflate:{key}"] = f"{ov}→{dv}"
            conflated.append((review_tags, dbsn_geom))
            safe_merged = {k: v for k, v in merged.items() if k not in conflicts}
            if safe_merged != osm_tags:
                osc_way_tag_modifies.append((osm_id, osm_version, safe_merged))
        else:
            safe = _way_nodes_safe(osm_id, way_nodes, node_way_count, tagged_nodes, node_to_ways, way_tags)
            if safe:
                geom_result = _update_geometry_osc(
                    osm_id, osm_version, dbsn_geom, osm_geom, merged,
                    way_nodes, node_coords, node_versions,
                    node_way_count, tagged_nodes, node_to_ways, way_tags,
                    next_node_id,
                )
                if geom_result:
                    nc, nm, nd, wm = geom_result
                    osc_node_creates.extend(nc)
                    osc_node_modifies.extend(nm)
                    osc_node_deletes.extend(nd)
                    osc_geom_modifies.append(wm)
                else:
                    osc_way_tag_modifies.append((osm_id, osm_version, merged))
                conflated.append(({**review_tags, "note:conflate:geometry": "updated"}, dbsn_geom))
            else:
                osc_way_tag_modifies.append((osm_id, osm_version, merged))
                shared_n, shared_ids = _shared_node_info(osm_id)
                geom_note = f"shared_nodes:{shared_n}"
                ambiguous.append(({**review_tags, "note:conflate:geometry": geom_note,
                                   "note:conflate:shared_node_ids": shared_ids}, dbsn_geom))
                ambiguous.append(({**osm_tags, "osm:id": osm_id,
                                   "note:conflate": f"dbsn={dbsn_id},{geom_note}",
                                   "note:conflate:shared_node_ids": shared_ids}, osm_geom))

    # Unmatched DBSN → new (ref:dbsn enables skip resolution via triage)
    for dbsn_id in result.dbsn_only:
        dbsn_tags = _normalise_osm_tags(dbsn_tags_by_id[dbsn_id])
        tagged = {**dbsn_tags, "ref:dbsn": dbsn_id}
        if dbsn_id in new_tag_overrides:
            tagged = _apply_overrides(tagged, new_tag_overrides[dbsn_id])
        new_features.append((tagged, dbsn_geom_by_id[dbsn_id]))

    # Near-miss → ambiguous
    for dbsn_id, osm_id in result.near_miss:
        dbsn_tags = dbsn_tags_by_id[dbsn_id]
        osm_tags, osm_geom, _ = osm_by_id[osm_id]
        ambiguous.append(({**dbsn_tags, "ref:dbsn": dbsn_id,
                           "note:conflate": f"near_miss,osm={osm_id}"}, dbsn_geom_by_id[dbsn_id]))
        ambiguous.append(({**osm_tags, "osm:id": osm_id,
                           "note:conflate": f"near_miss,dbsn={dbsn_id}"}, osm_geom))

    # Shape-signature matches (same shape, no overlap) → ambiguous, suggest replace
    for dbsn_id, osm_id in result.shape_matched:
        dbsn_tags = dbsn_tags_by_id[dbsn_id]
        osm_tags, osm_geom, _ = osm_by_id[osm_id]
        ambiguous.append(({**dbsn_tags, "ref:dbsn": dbsn_id,
                           "note:conflate": f"shape_match,osm={osm_id},suggest=replace"},
                          dbsn_geom_by_id[dbsn_id]))
        ambiguous.append(({**osm_tags, "osm:id": osm_id,
                           "note:conflate": f"shape_match,dbsn={dbsn_id},suggest=replace"}, osm_geom))
    if result.shape_matched:
        print(f"  [shape  ] {code}: {len(result.shape_matched)} shape-signature pairs → ambiguous")

    # Unmatched OSM → osm_only (N:1 containment check: OSM feature inside a matched DBSN)
    containment_threshold = config.get("containment_threshold", 0.8)
    _matched_dbsn_ids = [did for did, _ in result.matched]
    _matched_dbsn_geoms = [dbsn_geom_by_id[did] for did in _matched_dbsn_ids]
    _matched_dbsn_tree = STRtree(_matched_dbsn_geoms) if _matched_dbsn_ids else None
    n_contained = 0
    for osm_id in result.osm_only:
        osm_tags, osm_geom, _ = osm_by_id[osm_id]
        contained_by = None
        if _matched_dbsn_tree is not None and osm_geom.area > 0:
            for hi in _matched_dbsn_tree.query(osm_geom):
                inter = osm_geom.intersection(_matched_dbsn_geoms[hi]).area
                if inter / osm_geom.area >= containment_threshold:
                    contained_by = _matched_dbsn_ids[hi]
                    break
        if contained_by:
            dbsn_tags = dbsn_tags_by_id[contained_by]
            ambiguous.append(({**dbsn_tags, "ref:dbsn": contained_by,
                               "note:conflate": f"has_part,osm={osm_id}"}, dbsn_geom_by_id[contained_by]))
            ambiguous.append(({**osm_tags, "osm:id": osm_id,
                               "note:conflate": f"contained_by,dbsn={contained_by}"}, osm_geom))
            n_contained += 1
        elif osm_tags.get("parking") != "surface":
            osm_only.append(({**osm_tags, "osm:id": osm_id}, osm_geom))
    if n_contained:
        print(f"  [conflate] {code}: {n_contained} OSM features contained in matched DBSN → ambiguous")

    # building:part containment check against ALL DBSN (matched + new + near-miss)
    if part_features_raw:
        _all_dbsn_ids = list(dbsn_geom_by_id.keys())
        _all_dbsn_geoms = [dbsn_geom_by_id[did] for did in _all_dbsn_ids]
        _all_dbsn_tree = STRtree(_all_dbsn_geoms)
        n_parts_contained = 0
        for fid, tags, geom, _ver in part_features_raw:
            contained_by = None
            if geom.area > 0:
                for hi in _all_dbsn_tree.query(geom):
                    inter = geom.intersection(_all_dbsn_geoms[hi]).area
                    if inter / geom.area >= containment_threshold:
                        contained_by = _all_dbsn_ids[hi]
                        break
            if contained_by:
                dbsn_tags = dbsn_tags_by_id[contained_by]
                ambiguous.append(({**dbsn_tags, "ref:dbsn": contained_by,
                                   "note:conflate": f"has_part,osm={fid}"}, dbsn_geom_by_id[contained_by]))
                ambiguous.append(({**tags, "osm:id": fid,
                                   "note:conflate": f"contained_by,dbsn={contained_by}"}, geom))
                n_parts_contained += 1
            else:
                osm_only.append(({**tags, "osm:id": fid}, geom))
        if n_parts_contained:
            print(f"  [conflate] {code}: {n_parts_contained} building:part features inside DBSN → ambiguous")

    # Ambiguous N:M
    for dbsn_ids, osm_ids in result.ambiguous:
        for dbsn_id in dbsn_ids:
            dbsn_tags = dbsn_tags_by_id[dbsn_id]
            ambiguous.append(({**dbsn_tags, "ref:dbsn": dbsn_id,
                               "note:conflate": f"ambiguous,osm={','.join(osm_ids)}"},
                              dbsn_geom_by_id[dbsn_id]))
        for osm_id in osm_ids:
            osm_tags, osm_geom, _ = osm_by_id[osm_id]
            ambiguous.append(({**osm_tags, "osm:id": osm_id,
                               "note:conflate": f"ambiguous,dbsn={','.join(dbsn_ids)}"}, osm_geom))

    # Apply resolved decisions
    for osm_id, res in resolutions.items():
        if osm_id.startswith("dbsn:"):
            continue
        decision = res.get("decision")
        tag_overrides: dict = res.get("tags", {})
        if decision == "replace" and osm_id in osm_by_id:
            _, _, version = osm_by_id[osm_id]
            osc_way_deletes.append((osm_id, version))
            dbsn_cid = res.get("dbsn_classid") or res.get("dbsn_id")
            if dbsn_cid and dbsn_cid in dbsn_geom_by_id:
                final_tags = _normalise_osm_tags(_apply_overrides(dbsn_tags_by_id[dbsn_cid], tag_overrides))
                osc_replace_creates.append((final_tags, dbsn_geom_by_id[dbsn_cid]))
                conflated.append((final_tags, dbsn_geom_by_id[dbsn_cid]))
        elif decision == "keep_osm" and osm_id in osm_by_id:
            osm_tags, osm_geom, version = osm_by_id[osm_id]
            if tag_overrides:
                final_tags = _normalise_osm_tags(_apply_overrides(osm_tags, tag_overrides))
                nd_refs = _way_nd_refs(osm_id, way_nodes)
                if nd_refs is not None:
                    osc_geom_modifies.append((osm_id, version, nd_refs, final_tags))
                else:
                    osc_way_tag_modifies.append((osm_id, version, final_tags))
                conflated.append((final_tags, osm_geom))
            else:
                conflated.append((osm_tags, osm_geom))
        elif decision == "delete" and osm_id in osm_by_id:
            _, _, version = osm_by_id[osm_id]
            osc_way_deletes.append((osm_id, version))
        elif decision == "tag_update" and osm_id in osm_by_id:
            osm_tags, osm_geom, version = osm_by_id[osm_id]
            final_tags = _normalise_osm_tags(_apply_overrides(osm_tags, tag_overrides))
            nd_refs = _way_nd_refs(osm_id, way_nodes)
            if nd_refs is not None:
                osc_geom_modifies.append((osm_id, version, nd_refs, final_tags))
            else:
                osc_way_tag_modifies.append((osm_id, version, final_tags))
            dbsn_cid = res.get("dbsn_classid")
            if dbsn_cid and dbsn_cid in dbsn_geom_by_id:
                # Geometry decision still open — re-expose both in ambiguous.osm for re-triage
                ambiguous.append(({**final_tags, "osm:id": osm_id,
                                   "note:conflate": f"tag_update,dbsn={dbsn_cid}"}, osm_geom))
                ambiguous.append(({**dbsn_tags_by_id[dbsn_cid], "ref:dbsn": dbsn_cid,
                                   "note:conflate": f"osm={osm_id}"}, dbsn_geom_by_id[dbsn_cid]))
            else:
                conflated.append((final_tags, osm_geom))

    # Write outputs
    def _items(bucket):
        for tags, geom in bucket:
            yield shapely_mapping(geom), tags

    for suffix in ("_new.osm", "_osm_only.osm", "_conflated.osm", "_ambiguous.osm", "_changes.osc"):
        _out(code, date, suffix, area_slug).unlink(missing_ok=True)

    n_new = write_osm_items(_items(new_features), _out(code, date, "_new.osm", area_slug), upload="never")
    n_osm_only = write_osm_items(_items(osm_only), _out(code, date, "_osm_only.osm", area_slug), upload="never")
    n_conflated = write_osm_items(_items(conflated), _out(code, date, "_conflated.osm", area_slug), upload="never")
    n_ambiguous = write_osm_items(_items(ambiguous), _out(code, date, "_ambiguous.osm", area_slug), upload="never")
    osc_all_creates = [*new_features, *osc_replace_creates]
    _osc_write(
        _out(code, date, "_changes.osc", area_slug),
        osc_all_creates, osc_way_tag_modifies, osc_way_deletes,
        osc_node_creates, osc_node_modifies, osc_node_deletes,
        osc_geom_modifies, next_node_id,
    )

    n_geom = len(osc_geom_modifies)
    print(
        f"  [done   ] {code}: new={n_new} osm_only={n_osm_only} "
        f"conflated={n_conflated} ambiguous={n_ambiguous} "
        f"osc=({len(osc_all_creates)} create, {len(osc_way_tag_modifies)} tag-modify, "
        f"{n_geom} geom-modify, {len(osc_way_deletes)} delete)"
        + (f" identical_dropped={n_identical}" if n_identical else "")
    )


# ---------------------------------------------------------------------------
# DAG task
# ---------------------------------------------------------------------------

class ConflateTask(Task):
    def __init__(
        self,
        prov: Province,
        sources_by_code: dict[str, Province],
        overwrite_steps: frozenset[str] = frozenset(),
        area: str | None = None,
        fmt: str = "osm",
        compress: bool = False,
        extend: bool = True,
        review_identical: bool = False,
    ) -> None:
        self._prov = prov
        self._sources = sources_by_code
        self._overwrite_steps = overwrite_steps
        self._area = area
        self._fmt = fmt
        self._compress = compress
        self._extend = extend
        self._review_identical = review_identical

    @property
    def name(self) -> str:
        return f"conflate:{self._prov['code']}"

    def skip_if(self) -> bool:
        if "conflate" in self._overwrite_steps:
            return False
        code, date = self._prov["code"], self._prov["date"]
        area_slug = f"_{self._area.lower().replace(' ', '_')}" if self._area else ""
        return all(
            _out(code, date, s, area_slug).exists()
            for s in ("_new.osm", "_osm_only.osm", "_conflated.osm", "_ambiguous.osm", "_changes.osc")
        )

    def dependencies(self) -> list[Task]:
        return [
            ConvertTask(self._prov, self._sources, self._overwrite_steps,
                        self._fmt, self._compress, self._extend, area=self._area),
            DownloadPbfTask(self._prov, overwrite="pbf" in self._overwrite_steps,
                            area=self._area),
        ]

    def run(self) -> None:
        p = self._prov
        config = _load_config(p["code"], p.get("region", ""))
        specificity = _load_specificity()
        _run(p, "conflate" in self._overwrite_steps, self._area, config, specificity,
             review_identical=self._review_identical)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _extra_args(parser) -> None:
    add_area_arg(parser)
    add_max_weight_arg(parser)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download osmit-estratti PBF before conflating (equivalent to --overwrite pbf)",
    )
    parser.add_argument(
        "--overpass-url", dest="overpass_url", metavar="URL",
        help="Custom Overpass API endpoint (used when --area is specified)",
    )
    parser.add_argument(
        "--review-identical", dest="review_identical", action="store_true",
        help="Keep zero-delta matches (IoU≥identical_iou, no tag changes) in _conflated.osm",
    )
    parser.add_argument(
        "--josm", nargs="?", const="localhost:8111", metavar="HOST:PORT",
        help="After conflation, open review files + PBF in JOSM via remote control; "
             "optional HOST:PORT (default: localhost:8111). PBF layer is reference "
             "only — no lock available via remote control",
    )


def main() -> None:
    args = parse_args(
        "Step 5: conflate DBSN output against OSM",
        resolve_provinces=True,
        setup=_extra_args,
    )
    overwrite_steps = parse_overwrite(args.overwrite)
    if args.download:
        overwrite_steps = overwrite_steps | frozenset({"pbf"})

    sources = read_sources()
    sources_by_code = {p["code"]: p for p in sources}

    tasks = [
        ConflateTask(prov, sources_by_code, overwrite_steps, area=args.area,
                     review_identical=args.review_identical)
        for prov in args.provinces
    ]
    run_dag(tasks, args.max_weight)

    if args.josm:
        _josm_open(args.provinces, args.area, args.josm)


def _josm_open(provinces: list, area: str | None, host: str) -> None:
    """Open conflate review files + PBF as JOSM layers via remote control."""
    import urllib.request
    from urllib.error import URLError

    area_slug = f"_{area.lower().replace(' ', '_')}" if area else ""
    pbf_slug = _pbf_normalize(area) if area else ""
    for p in provinces:
        code, date = p["code"], p["date"]
        pbf = find_pbf(code, pbf_slug)
        layers = []
        if pbf is not None:
            # /import cannot read PBF binary — convert to .osm XML (cached next to it)
            xml = pbf.with_suffix(".osm")
            if not xml.exists() or xml.stat().st_mtime < pbf.stat().st_mtime:
                import subprocess
                result = subprocess.run(
                    ["osmium", "cat", str(pbf), "-o", str(xml)],
                    check=False, capture_output=True, text=True,
                )
                if result.returncode != 0:
                    print(f"  [josm   ] {code}: osmium cat failed "
                          f"(exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
                    xml = pbf  # attempt PBF import anyway
            if xml.exists():
                layers.append((f"osm-estratti {code}", xml))
        for suffix, label in (("_ambiguous.osm", "ambiguous"), ("_new.osm", "new"),
                              ("_osm_only.osm", "osm-only"), ("_conflated.osm", "conflated")):
            out = _out(code, date, suffix, area_slug)
            if out.exists():
                layers.append((f"{label} {code}", out))
        if not layers:
            continue
        try:
            for url in josm_load_urls(layers, host):
                urllib.request.urlopen(url, timeout=5).close()
            print(f"  [josm   ] {code}: opened {len(layers)} layers in JOSM "
                  f"(new layers each run — close stale ones)")
        except (URLError, OSError):
            print(f"  [josm   ] {code}: JOSM remote control not reachable at {host} "
                  f"(enable it in JOSM preferences), open manually:", file=sys.stderr)
            for name, path in layers:
                print(f"           {name}: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
