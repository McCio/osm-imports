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

# review-file stem suffixes (= diff kinds) that are absorbed; `_ambiguous` follows in a later slice
_KINDS = ("new", "conflated", "osm_only", "ambiguous")


def _kind(path: Path) -> str | None:
    """Diff kind of a review file (`X_osm_only.osm` -> `osm_only`), None for the kinds not absorbed yet."""
    return next((k for k in _KINDS if path.stem.endswith(f"_{k}")), None)


_DECISION_KEY = "note:conflate:decision"
_KEEP = frozenset({"keep", "both"})
_UNDECIDED = frozenset({"", "undecided"})
_MAKE_PART = frozenset({"make_part", "part"})  # contained OSM feature: building=X becomes building:part=X
_DROP = frozenset({"delete", "drop"})
_MATCH = frozenset({"match"})  # interpret the pair as a match: the OSM way is updated in place
_PAIR_KINDS = ("near_miss", "shape_match")  # 1:1 pairs where removing the OSM member means match


@dataclass
class _Feature:
    tags: dict[str, str]
    geom: object
    note: str = ""  # `note:conflate`: names the OSM mate (`osm=wXXX[,merged=a|b]`) of a `_conflated` pair
    decision: str | None = None  # the user's `note:conflate:decision` value, an instruction rather than an OSM tag
    deleted: bool = False  # a JOSM `action="delete"` primitive
    vertices: tuple[str, ...] = ()  # `osm:id` (nXXX) of the way's identity nodes: its unsafe OSM nodes


@dataclass
class _Node:
    lon: float
    lat: float
    tags: dict[str, str]


def _identity_nodes(path: Path) -> dict[str, _Node]:
    """`nXXX` → node, for every node of an OSM file carrying `osm:id=nXXX` (JOSM renumbering is harmless)."""
    out: dict[str, _Node] = {}
    for n in ET.parse(path).getroot().iter("node"):
        tags = {t.get("k"): t.get("v") for t in n.iter("tag")}
        if tags.get("osm:id", "").startswith("n"):
            out[tags["osm:id"]] = _Node(float(n.get("lon")), float(n.get("lat")), tags)
    return out


def _node_decisions(feat: _Feature, before: dict[str, _Node], after: dict[str, _Node]) -> dict[str, object]:
    """`carry_nodes` / `delete_nodes` entries for the unsafe nodes of `feat` the user marked `note:conflate:unsafe`.

    `detach` is the default and records nothing; a node missing from the edited file is detached too, never deleted.
    `carry` counts only for a node the user moved (an unmoved vertex is reused as is).
    """
    carry: dict[str, dict] = {}
    delete: list[str] = []
    for nid in feat.vertices:
        b, a = before[nid], after.get(nid)
        action = a.tags.get("note:conflate:unsafe") if a else None
        if action == "delete":
            delete.append(nid)
        elif action == "carry" and (abs(a.lon - b.lon) > _GEOM_TOLERANCE or abs(a.lat - b.lat) > _GEOM_TOLERANCE):
            tags = {k: v for k, v in a.tags.items() if k != "osm:id" and not _is_conflate_meta(k)}
            carry[nid] = {"lon": a.lon, "lat": a.lat, "tags": tags}
    return {**({"carry_nodes": carry} if carry else {}), **({"delete_nodes": delete} if delete else {})}


def _read_features(path: Path, key: str = "ref:dbsn") -> dict[str, _Feature]:
    """`key` tag value → feature, for every way or multipolygon relation carrying `key` in an OSM file."""
    root = ET.parse(path).getroot()
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.iter("node")}
    rings = {
        w.get("id"): Polygon([nodes[nd.get("ref")] for nd in w.iter("nd")])
        for w in root.iter("way")
    }
    identity = {  # xml node id → nXXX
        n.get("id"): v for n in root.iter("node")
        for t in n.iter("tag") if t.get("k") == "osm:id" and (v := t.get("v")).startswith("n")
    }
    out: dict[str, _Feature] = {}
    for elem in root:
        tags = {t.get("k"): t.get("v") for t in elem.iter("tag")}
        if key not in tags:
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
        out[tags[key]] = _Feature(
            {k: v for k, v in tags.items() if k != "type" and not _is_conflate_meta(k)},
            geom,
            tags.get("note:conflate", ""),
            tags.get(_DECISION_KEY),
            elem.get("action") == "delete",
            tuple(identity[ref] for nd in elem.iter("nd") if (ref := nd.get("ref")) in identity)
            if elem.tag == "way" else (),
        )
    return out


def _keeps_osm(ref: str, after: _Feature | None) -> bool:
    """Did the user decide `keep` on the pair? Unknown values warn and count as keep (the safe direction)."""
    value = (after.decision if after else None) or ""
    if value.lower() in _UNDECIDED | _MAKE_PART | _DROP | _MATCH:
        return False
    if value.lower() not in _KEEP:
        print(f"  [warn   ] {_DECISION_KEY}={value!r} on {ref} is not keep: keeping the OSM feature")
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
        if _kind(p)
        and edited_sibling(p).exists()
        and edited_sibling(p).stat().st_mtime_ns > p.stat().st_mtime_ns
    ]


def unedited_copies(review_files: list[Path]) -> list[Path]:
    """Review files whose `_edited` sibling is older than the file: a copy of a previous run nobody saved since.

    Such a copy holds no edits and is safe to replace (a fresh copy has the file's mtime, so it is not listed).
    """
    return [
        p for p in review_files
        if edited_sibling(p).exists() and edited_sibling(p).stat().st_mtime_ns < p.stat().st_mtime_ns
    ]


def absorb_edits(review_files: list[Path], resolutions_path: Path) -> int:
    """Merge the edits of each review file's `_edited` sibling into the resolutions file.

    Returns the number of entries written. Idempotent; the `_edited` copies are left untouched.
    """
    found: dict[str, dict] = {}
    for path in pending_edits(review_files):
        found.update(diff_edited(path, edited_sibling(path), _kind(path)))
    if not found:
        return 0
    resolutions = json.loads(resolutions_path.read_text(encoding="utf-8")) if resolutions_path.exists() else {}
    for key, entry in found.items():
        resolutions[key] = _merge_entry(resolutions.get(key, {}), entry)
    tmp = resolutions_path.with_name(resolutions_path.name + ".tmp")
    tmp.write_text(json.dumps(resolutions, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(resolutions_path)
    return len(found)


def _mates(note: str, key: str) -> list[str]:
    """Ids listed under `key=` in a `note:conflate` value (`ambiguous,osm=w1,w2` → [w1, w2])."""
    out: list[str] = []
    listing = False
    for part in note.split(","):
        if "=" in part:
            name, _, value = part.partition("=")
            listing = name == key
            if listing:
                out.append(value)
        elif listing:
            out.append(part)
    return out


def _diff_ambiguous(original: Path, edited: Path) -> dict[str, dict]:
    """Entries for `_ambiguous`: each member is judged on its own.

    A removed DBSN member is skipped, a removed OSM member deleted, except the OSM member of a near-miss or
    shape-match pair whose DBSN mate stays: that means `match` (also an explicit `note:conflate:decision=match`
    on either member, with exactly one mate left). A decided member (`keep`/`both`, `make_part`,
    every mate removed, or no mate at all) always keeps its tag, shape and node edits, whatever the conflict kind;
    `keep` on either member keeps the OSM way and lets the DBSN feature be imported beside it. An edit on an
    undecided member is not absorbed, except both members of a pair drawn anew: keep both.
    """
    osm_before, osm_after = _read_features(original, "osm:id"), _read_features(edited, "osm:id")
    dbsn_before, dbsn_after = _read_features(original), _read_features(edited)

    def _dropped(i: str) -> bool:
        return i in osm_after and (osm_after[i].decision or "").lower() in _DROP

    def _decided(feat: _Feature | None, values: frozenset[str]) -> bool:
        return feat is not None and (feat.decision or "").lower() in values

    def _osm_mates(ref: str) -> list[str]:
        return [m for m in _mates(dbsn_before[ref].note, "osm") if m in osm_before]

    removed = {i for i in osm_before if i not in osm_after or osm_after[i].deleted or _dropped(i)}
    dbsn_left = {d for d, f in dbsn_after.items() if d in dbsn_before and not f.deleted}

    # `match`: the DBSN feature is forced onto the OSM way, which is updated in place
    matches: dict[str, str] = {}  # osm id → dbsn id
    for i in removed:  # removing the OSM member of a near-miss / shape-match pair whose DBSN mate stays
        mates = _mates(osm_before[i].note, "dbsn")
        if osm_before[i].note.split(",")[0] in _PAIR_KINDS and len(mates) == 1 and not _dropped(i) \
                and mates[0] in dbsn_left:
            matches[i] = mates[0]
    for i, f in osm_after.items():  # the explicit value, on either member: its one remaining mate
        if i in osm_before and i not in removed and _decided(f, _MATCH):
            left = [d for d in _mates(osm_before[i].note, "dbsn") if d in dbsn_left]
            if len(left) == 1:
                matches[i] = left[0]
            else:
                print(f"  [warn   ] {i}: {_DECISION_KEY}=match needs exactly one DBSN mate left; delete the others")
    for d in dbsn_left:
        if _decided(dbsn_after[d], _MATCH):
            left = [o for o in _osm_mates(d) if o not in removed]
            if len(left) == 1:
                matches[left[0]] = d
            else:
                print(f"  [warn   ] {d}: {_DECISION_KEY}=match needs exactly one OSM mate left; delete the others")

    deleted = {i: {"decision": "delete"} for i in removed if i not in matches}
    dbsn = diff_edited(original, edited, "new")
    skips = {k: v for k, v in dbsn.items() if v.get("decision") == "skip"}
    dbsn_edits = {k: v for k, v in dbsn.items() if "decision" not in v}
    osm_edits = {i: e for i, e in diff_edited(original, edited, "osm_only").items() if e["decision"] == "tag_update"}

    taken = deleted.keys() | matches.keys()
    kept = {i for i in osm_before if i not in taken and _keeps_osm(i, osm_after.get(i))}
    for ref in dbsn_before:
        if _keeps_osm(ref, dbsn_after.get(ref)):
            kept.update(m for m in _osm_mates(ref) if m not in taken)
    kept.update(  # the shapes of both members of a pair drawn anew: keep both
        i for i, e in osm_edits.items()
        if "geometry" in e and (mates := _mates(osm_before[i].note, "dbsn"))
        and all("geometry" in dbsn_edits.get(f"dbsn:{m}", {}) for m in mates)
    )
    make_part = {
        i for i, f in osm_after.items()
        if i in osm_before and i not in removed and "building" in f.tags and _decided(f, _MAKE_PART)
    }
    decided_osm = (kept | make_part | {
        i for i, f in osm_before.items()
        if not (mates := _mates(f.note, "dbsn")) or all(f"dbsn:{m}" in skips for m in mates)
    }) - taken
    decided_dbsn = {
        k for k in dbsn_edits
        if k.removeprefix("dbsn:") in matches.values()
        or ((mates := _osm_mates(k.removeprefix("dbsn:"))) and (kept.intersection(mates) or set(mates) <= removed))
    }

    entries: dict[str, dict] = {
        **skips, **deleted, **{i: {"decision": "match", "dbsn_classid": d} for i, d in matches.items()},
    }
    for i in decided_osm:
        edit = dict(osm_edits.get(i, {}))
        if i in make_part:  # building=X becomes building:part=X on top of the user's own tags
            edit = {**edit, "decision": "tag_update",
                    "tags": {**edit.get("tags", {}), "building:part": osm_after[i].tags["building"], "-building": ""}}
        if edit:
            entries[i] = edit
        elif i in kept:
            entries[i] = {"decision": "keep_osm"}
    entries.update({k: v for k, v in dbsn_edits.items() if k in decided_dbsn})
    for i in osm_edits.keys() & matches.keys():
        print(f"  [warn   ] {i}: edit of an OSM member being matched is not absorbed; edit the DBSN member instead")
    settled = decided_osm | taken
    for ref in [*(k for k in dbsn_edits if k not in decided_dbsn), *(i for i in osm_edits if i not in settled)]:
        print(f"  [warn   ] {ref}: edit of an undecided member is not absorbed; set {_DECISION_KEY} or delete a mate")
    return entries


def diff_edited(original: Path, edited: Path, kind: str) -> dict[str, dict]:
    """Resolution entries (`dbsn:<classid>` → entry) for what the user changed in `edited`."""
    if kind == "ambiguous":
        return _diff_ambiguous(original, edited)
    key = "osm:id" if kind == "osm_only" else "ref:dbsn"
    before, after = _read_features(original, key), _read_features(edited, key)
    nodes_before = _identity_nodes(original) if kind == "osm_only" else {}
    nodes_after = _identity_nodes(edited) if kind == "osm_only" else {}
    entries: dict[str, dict] = {}
    for ref, feat in before.items():
        gone = ref not in after or after[ref].deleted
        keep = kind == "conflated" and _keeps_osm(ref, after.get(ref))
        if kind == "conflated" and (gone or keep):
            entries.update(_pair_decision(ref, feat, keep))
            continue
        if gone:
            if kind == "osm_only":
                entries[ref] = {"decision": "delete"}
            else:
                entries[f"dbsn:{ref}"] = {"decision": "skip"}
            continue
        entry: dict = {}
        if overrides := _tag_overrides(feat.tags, after[ref].tags):
            entry["tags"] = overrides
        if not feat.geom.normalize().equals_exact(after[ref].geom.normalize(), _GEOM_TOLERANCE):
            entry["geometry"] = to_wkt(after[ref].geom, rounding_precision=7)
        entry.update(_node_decisions(feat, nodes_before, nodes_after))
        if entry and kind == "osm_only":
            entries[ref] = {"decision": "tag_update", **entry}
        elif entry:
            entries[f"dbsn:{ref}"] = entry
    return entries
