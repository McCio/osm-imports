"""Geometric matching of DBSN features against OSM features."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from shapely import STRtree, affinity


@dataclass
class MatchResult:
    matched: list[tuple[str, str]] = field(default_factory=list)   # (dbsn_id, osm_id)
    dbsn_only: list[str] = field(default_factory=list)
    osm_only: list[str] = field(default_factory=list)
    ambiguous: list[tuple[list[str], list[str]]] = field(default_factory=list)  # ([dbsn_ids], [osm_ids])
    near_miss: list[tuple[str, str]] = field(default_factory=list)  # (dbsn_id, osm_id) — below threshold
    detected_offset: tuple[float, float, float] | None = None  # (dx_deg, dy_deg, dist_m)


def match_features(
    dbsn: list[tuple[str, object]],
    osm: list[tuple[str, object]],
    config: dict,
) -> MatchResult:
    """Match DBSN features to OSM features by centroid distance + IoU."""
    if not dbsn or not osm:
        return MatchResult(
            dbsn_only=[d for d, _ in dbsn],
            osm_only=[o for o, _ in osm],
        )

    max_m = config.get("centroid_distance_m", 100)
    iou_min = config.get("iou_threshold", 0.3)
    near_miss_iou = config.get("near_miss_iou", 0.05)

    # Degree-to-metre approximation: 1° ≈ 111_000 m (good enough for centroid gate)
    base_max_deg = max_m / 111_000

    osm_ids = [oid for oid, _ in osm]
    osm_geoms = [g for _, g in osm]
    tree = STRtree(osm_geoms)

    # Compute IoU for a candidate pair
    def _iou(g1, g2):
        inter = g1.intersection(g2).area
        if inter == 0:
            return 0.0
        return inter / g1.union(g2).area

    # Size-aware centroid gate: large features allow a wider centroid radius.
    # Factor: sqrt(area_m2) * 1.5, so a 10_000 m² building gets +150 m extra.
    _DEG_PER_M = 1 / 111_000

    def _centroid_gate_deg(g):
        area_deg2 = g.area
        area_m2 = area_deg2 * (111_000 ** 2)
        return base_max_deg + (area_m2 ** 0.5 * 1.5) * _DEG_PER_M

    # Collect scores at two levels: confirmed (≥ iou_min) and near-miss (≥ near_miss_iou)
    scores: dict[tuple[int, int], float] = {}      # (di, oi) → IoU, confirmed matches
    nm_scores: dict[tuple[int, int], float] = {}   # (di, oi) → IoU, near-miss candidates

    for di, (_, dg) in enumerate(dbsn):
        dc = dg.centroid
        gate = _centroid_gate_deg(dg)
        candidates = tree.query(dc.buffer(gate))
        for oi in candidates:
            iou = _iou(dg, osm_geoms[oi])
            if iou >= iou_min:
                scores[(di, oi)] = iou
            elif iou >= near_miss_iou:
                nm_scores[(di, oi)] = iou

    # Greedy best-IoU assignment for confirmed matches, no sharing
    result = MatchResult()
    used_dbsn: set[int] = set()
    used_osm: set[int] = set()

    for (di, oi), _ in sorted(scores.items(), key=lambda x: -x[1]):
        if di in used_dbsn or oi in used_osm:
            continue
        result.matched.append((dbsn[di][0], osm_ids[oi]))
        used_dbsn.add(di)
        used_osm.add(oi)

    # Near-miss: best candidate for each unmatched DBSN feature (one pair only)
    nm_best: dict[int, tuple[int, float]] = {}  # di → (oi, iou)
    for (di, oi), iou in nm_scores.items():
        if di in used_dbsn or oi in used_osm:
            continue
        if di not in nm_best or iou > nm_best[di][1]:
            nm_best[di] = (oi, iou)
    nm_used_osm: set[int] = set()
    for di, (oi, _) in sorted(nm_best.items(), key=lambda x: -x[1][1]):
        if oi in nm_used_osm:
            continue
        result.near_miss.append((dbsn[di][0], osm_ids[oi]))
        nm_used_osm.add(oi)
        used_dbsn.add(di)
        used_osm.add(oi)

    result.dbsn_only = [dbsn[i][0] for i in range(len(dbsn)) if i not in used_dbsn]
    result.osm_only = [osm_ids[i] for i in range(len(osm)) if i not in used_osm]

    # Offset detection: if enough matches, compute median centroid delta
    min_matches = config.get("offset_min_matches", 20)
    min_dist_m = config.get("offset_min_m", 5.0)
    if len(result.matched) >= min_matches:
        dbsn_by_id = {did: dg for did, dg in dbsn}
        osm_by_id = {oid: osm_geoms[osm_ids.index(oid)] for oid in osm_ids}
        deltas = [
            (osm_by_id[oid].centroid.x - dbsn_by_id[did].centroid.x,
             osm_by_id[oid].centroid.y - dbsn_by_id[did].centroid.y)
            for did, oid in result.matched
        ]
        dx = statistics.median(d[0] for d in deltas)
        dy = statistics.median(d[1] for d in deltas)
        dist_m = ((dx * 111_000) ** 2 + (dy * 111_000) ** 2) ** 0.5
        if dist_m > min_dist_m:
            shifted = [(did, affinity.translate(dg, xoff=dx, yoff=dy)) for did, dg in dbsn]
            result2 = match_features(shifted, osm, {**config, "offset_min_matches": 10**9})
            result2.detected_offset = (dx, dy, dist_m)
            return result2

    return result
