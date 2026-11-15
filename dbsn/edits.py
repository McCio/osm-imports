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
_KINDS = {"new": "new", "conflated": "conflated"}


_KEEP_TRUE = frozenset({"yes", "true", "1"})
_KEEP_FALSE = frozenset({"no", "false", "0"})


@dataclass
class _Feature:
    tags: dict[str, str]
    geom: object
    note: str = ""  # `note:conflate`: names the OSM mate (`osm=wXXX[,merged=a|b]`) of a `_conflated` pair
    keep_osm: str | None = None  # the user's `keep_osm` tag, an instruction rather than an OSM tag
    deleted: bool = False  # a JOSM `action="delete"` primitive


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
        if "ref:dbsn" not in tags:
            continue
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
            {k: v for k, v in tags.items() if k not in ("type", "keep_osm") and not _is_conflate_meta(k)},
            geom,
            tags.get("note:conflate", ""),
            tags.get("keep_osm"),
            elem.get("action") == "delete",
        )
    return out


def _keeps_osm(ref: str, after: _Feature | None) -> bool:
    """Did the user tag the pair `keep_osm`? Unknown values warn and count as keep (the safe direction)."""
    value = after.keep_osm if after else None
    if value is None or value.lower() in _KEEP_FALSE:
        return False
    if value.lower() not in _KEEP_TRUE:
        print(f"  [warn   ] keep_osm={value!r} on {ref} is not yes/true/1: keeping the OSM feature")
    return True


def _pair_decision(ref: str, feat: _Feature, keep: bool) -> dict[str, dict]:
    """Entries for a `_conflated` pair the user removed (delete its OSM mate) or tagged `keep_osm` (leave it).

    Either way the DBSN feature (and every one merged into the pair) stays out of `_new`.
    """
    fields = dict(part.split("=", 1) for part in feat.note.split(",") if "=" in part)
    decision = "keep_osm" if keep else "delete"
    entries = {f"dbsn:{extra}": {"decision": "skip"} for extra in fields.get("merged", "").split("|") if extra}
    if "osm" in fields:
        entries[fields["osm"]] = {"decision": decision, "dbsn_classid": ref}
    else:
        entries[f"dbsn:{ref}"] = {"decision": "skip"}
    return entries


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


def drop_edited_copies(review_files: list[Path]) -> list[Path]:
    """Delete the `_edited` sibling of each review file; returns the ones that existed."""
    dropped = [e for e in map(edited_sibling, review_files) if e.exists()]
    for e in dropped:
        e.unlink()
    return dropped


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
        gone = ref not in after or after[ref].deleted
        keep = kind == "conflated" and _keeps_osm(ref, after.get(ref))
        if kind == "conflated" and (gone or keep):
            entries.update(_pair_decision(ref, feat, keep))
            continue
        if gone:
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
