"""Absorb JOSM edits of conflate review files into resolution entries."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from shapely import to_wkt
from shapely.geometry import Polygon
from shapely.ops import unary_union

from dbsn.triage import _is_conflate_meta

# JOSM and the osmium writer both keep 1e-7 degree precision
_GEOM_TOLERANCE = 1e-8

# review-file stem suffix → diff kind; the other review files follow in later slices
_KINDS = {"new": "new"}


@dataclass
class _Feature:
    tags: dict[str, str]
    geom: object


def _read_features(path: Path) -> dict[str, _Feature]:
    """`ref:dbsn` → feature, for every tagged way or multipolygon relation in an OSM file."""
    root = ET.parse(path).getroot()
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.iter("node")}
    rings = {
        w.get("id"): Polygon([nodes[nd.get("ref")] for nd in w.iter("nd")])
        for w in root.iter("way")
    }
    out: dict[str, _Feature] = {}
    for elem in root:
        tags = {t.get("k"): t.get("v") for t in elem.iter("tag")}
        if "ref:dbsn" not in tags or elem.get("action") == "delete":
            continue  # a JOSM-deleted primitive reads as removed
        if elem.tag == "way":
            geom = rings[elem.get("id")]
        elif elem.tag == "relation":
            members = [(m.get("ref"), m.get("role")) for m in elem.iter("member") if m.get("type") == "way"]
            outer = unary_union([rings[ref] for ref, role in members if role == "outer"])
            inner = unary_union([rings[ref] for ref, role in members if role == "inner"])
            geom = outer.difference(inner)
        else:
            continue
        out[tags["ref:dbsn"]] = _Feature(
            {k: v for k, v in tags.items() if k != "type" and not _is_conflate_meta(k)}, geom
        )
    return out


def _tag_overrides(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    """Overrides turning `before` into `after`; a `-key` entry deletes a base tag."""
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    return {**changed, **{f"-{k}": "" for k in before.keys() - after.keys()}}


def _merge_entry(existing: dict, new: dict) -> dict:
    """`new` over `existing`; tag overrides merge, and a key's `-key` twin is dropped."""
    merged = {**existing, **new}
    if "tags" in existing and "tags" in new:
        tags = dict(existing["tags"])
        for k, v in new["tags"].items():
            tags.pop(k[1:] if k.startswith("-") else f"-{k}", None)
            tags[k] = v
        merged["tags"] = tags
    return merged


def edited_sibling(path: Path) -> Path:
    """`X_conflated.osm` -> `X_conflated_edited.osm`, the copy the user edits in JOSM."""
    return path.with_name(f"{path.stem}_edited{path.suffix}")


def pending_edits(review_files: list[Path]) -> list[Path]:
    """Review files of an absorbable kind whose `_edited` sibling was saved after the file was written.

    A fresh copy keeps the original's mtime, so only a JOSM save makes the sibling newer.
    """
    return [
        p for p in review_files
        if p.stem.rsplit("_", 1)[-1] in _KINDS
        and edited_sibling(p).exists()
        and edited_sibling(p).stat().st_mtime_ns > p.stat().st_mtime_ns
    ]


def absorb_edits(review_files: list[Path], resolutions_path: Path) -> int:
    """Merge the edits of each review file's `_edited` sibling into the resolutions file.

    Returns the number of entries written. Idempotent; the `_edited` copies are left untouched.
    """
    found: dict[str, dict] = {}
    for path in pending_edits(review_files):
        found.update(diff_edited(path, edited_sibling(path), _KINDS[path.stem.rsplit("_", 1)[-1]]))
    if not found:
        return 0
    resolutions = json.loads(resolutions_path.read_text(encoding="utf-8")) if resolutions_path.exists() else {}
    for key, entry in found.items():
        resolutions[key] = _merge_entry(resolutions.get(key, {}), entry)
    tmp = resolutions_path.with_name(resolutions_path.name + ".tmp")
    tmp.write_text(json.dumps(resolutions, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(resolutions_path)
    return len(found)


def diff_edited(original: Path, edited: Path, kind: str) -> dict[str, dict]:
    """Resolution entries (`dbsn:<classid>` → entry) for what the user changed in `edited`."""
    before, after = _read_features(original), _read_features(edited)
    entries: dict[str, dict] = {}
    for ref, feat in before.items():
        if ref not in after:
            entries[f"dbsn:{ref}"] = {"decision": "skip"}
            continue
        entry: dict = {}
        if overrides := _tag_overrides(feat.tags, after[ref].tags):
            entry["tags"] = overrides
        if not feat.geom.normalize().equals_exact(after[ref].geom.normalize(), _GEOM_TOLERANCE):
            entry["geometry"] = to_wkt(after[ref].geom, rounding_precision=7)
        if entry:
            entries[f"dbsn:{ref}"] = entry
    return entries
