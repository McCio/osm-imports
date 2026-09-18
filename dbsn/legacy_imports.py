"""Legacy-import detection: OSM features created by old regional imports.

Detection is changeset-level, not element-level: the local PBF carries no
changeset metadata, so per-element changeset ids are fetched from the OSM API
(`/api/0.6/{type}s?{type}s=…`, 100 ids per call) and joined against changeset
tags fetched from `/api/0.6/changesets?changesets=…`. Results are cached per
conflate run dir as `_changesets.json`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from dbsn.common import http_client


def compile_patterns(patterns: list[dict]) -> list[tuple[re.Pattern, re.Pattern | None]]:
    """Compile [{changeset_source: regex, user: regex?}] → [(source_re, user_re|None)]."""
    out = []
    for p in patterns:
        src = re.compile(p["changeset_source"])
        user = re.compile(p["user"]) if p.get("user") else None
        out.append((src, user))
    return out


def is_legacy_import(changeset: dict | None,
                     compiled: list[tuple[re.Pattern, re.Pattern | None]]) -> bool:
    """True when a changeset's {source, user} matches any pattern (source AND user)."""
    if changeset is None or not compiled:
        return False
    for src_re, user_re in compiled:
        if src_re.search(changeset.get("source", "")):
            if user_re is None or user_re.search(changeset.get("user", "")):
                return True
    return False


def layered_patterns(config: dict, code: str, region: str, area_slug: str) -> list[dict]:
    """Collect legacy_import patterns from all config layers (list-append, not override).

    Layers: legacy_imports.patterns → regions[region] → provinces[code] →
    areas[area_slug] (comune level, keyed by the slug without leading '_').
    """
    li = config.get("legacy_imports", {}) or {}
    patterns: list[dict] = list(li.get("patterns", []))
    for layer_key, layer in ((region, li.get("regions", {})), (code, li.get("provinces", {})),
                             (area_slug.lstrip("_"), li.get("areas", {}))):
        patterns.extend(layer.get(layer_key, {}).get("patterns", []))
    return patterns


def fetch_changeset_meta(osm_ids: list[str], cache_path: Path) -> dict[str, dict]:
    """Fetch {user, source, comment, closed} per element id (w123/r456) via OSM API.

    Two-stage: elements (for their changeset id) → changesets (for their tags).
    Cached in cache_path as {element_id: {…changeset fields…, changeset: id}};
    cached entries are returned as-is, only missing ids are fetched.
    """
    cache: dict[str, dict] = {}
    if cache_path.exists():
        import json
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    missing = [i for i in osm_ids if i not in cache]
    if not missing:
        return cache

    by_type: dict[str, list[str]] = {}
    for oid in missing:
        by_type.setdefault("way" if oid[0] == "w" else "relation", []).append(oid[1:])

    elem_changeset: dict[str, str] = {}
    with http_client(timeout=60) as client:
        for typ, ids in by_type.items():
            for i in range(0, len(ids), 100):
                resp = client.get(
                    f"https://api.openstreetmap.org/api/0.6/{typ}s",
                    params={f"{typ}s": ",".join(ids[i:i + 100])},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
                for el in root.iter(typ):
                    elem_changeset[f"{'w' if typ == 'way' else 'r'}{el.get('id')}"] = el.get("changeset")

        cs_ids = sorted({cs for cs in elem_changeset.values() if cs and cs != "0"})
        cs_meta: dict[str, dict] = {}
        for i in range(0, len(cs_ids), 100):
            resp = client.get(
                "https://api.openstreetmap.org/api/0.6/changesets",
                params={"changesets": ",".join(cs_ids[i:i + 100])},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for cs in root.iter("changeset"):
                tags = {t.get("k"): t.get("v") for t in cs.iter("tag")}
                cs_meta[cs.get("id")] = {
                    "user": cs.get("user") or "",
                    "source": tags.get("source", ""),
                    "comment": tags.get("comment", ""),
                    "closed": cs.get("closed_at") or "",
                }

    for oid, cs in elem_changeset.items():
        meta = cs_meta.get(cs) or {}
        meta["changeset"] = cs
        cache[oid] = meta

    import json
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return cache
