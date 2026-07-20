"""Determine province neighbours from OSM administrative boundaries via Overpass."""

import json
import re
import sys
import unicodedata

import httpx
from shapely.geometry import MultiLineString
from tenacity import Retrying, retry_if_exception, stop_after_delay

from dbsn.common import DATA_DIR, SOURCES_JSON, Province, http_client, parse_args, read_sources

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_ADJACENCY_THRESHOLD = 0.009  # degrees (≈1 km)
_BOUNDARIES_CACHE = DATA_DIR / "osm_boundaries_cache.json"

_QUERY = """
[out:json][timeout:300];
(
  relation["ISO3166-2"~"^IT-[A-Z]{2}$"]["boundary"="administrative"];
  relation["ISO3166-2:2"~"^IT-[A-Z]{2}$"]["boundary"="administrative"];
  relation["ISO3166-1"="SM"]["boundary"="administrative"]["admin_level"="2"];
);
out geom;
""".strip()


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _retry_wait(retry_state) -> float:
    exc = retry_state.outcome.exception()
    if isinstance(exc, httpx.HTTPStatusError):
        ra = exc.response.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
    return min(30 * 2 ** (retry_state.attempt_number - 1), 120)


def _before_sleep(retry_state) -> None:
    secs = retry_state.next_action.sleep
    print(f"  [retry  ] retrying in {secs:.0f}s…", file=sys.stderr)


def _fetch_boundaries(overwrite: bool = False, url: str = _OVERPASS_URL) -> list[dict]:
    if not overwrite and _BOUNDARIES_CACHE.exists():
        elements = json.loads(_BOUNDARIES_CACHE.read_text(encoding="utf-8"))
        print(f"  [cache  ] {len(elements)} boundary relations loaded from {_BOUNDARIES_CACHE.name}")
        return elements
    print("  [fetch  ] Italian province boundaries from Overpass...")
    for attempt in Retrying(
        retry=retry_if_exception(_is_transient),
        wait=_retry_wait,
        stop=stop_after_delay(900),
        before_sleep=_before_sleep,
        reraise=True,
    ):
        with attempt, http_client(timeout=360) as client:
            resp = client.post(url, data={"data": _QUERY})
            resp.raise_for_status()
    data = resp.json()
    if "elements" not in data:
        sys.exit(f"error: Overpass returned unexpected response: {data.get('remark', data)!r}")
    elements = data["elements"]
    if len(elements) < 80:
        sys.exit(f"error: Overpass returned only {len(elements)} elements (expected ≥80 for Italian provinces)")
    _BOUNDARIES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _BOUNDARIES_CACHE.write_text(json.dumps(elements, ensure_ascii=False), encoding="utf-8")
    print(f"  [fetch  ] {len(elements)} boundary relations received, cached to {_BOUNDARIES_CACHE.name}")
    return elements


def _relation_to_geometry(rel: dict):
    """Convert Overpass relation to shapely MultiLineString (outer members only)."""
    rings = []
    for member in rel.get("members", []):
        if member.get("type") != "way":
            continue
        geom_pts = member.get("geometry", [])
        if len(geom_pts) < 2:
            continue
        rings.append([(p["lon"], p["lat"]) for p in geom_pts])
    if not rings:
        return None
    return MultiLineString(rings)


def _normalize(s: str) -> str:
    """Strip accents, hyphens, apostrophes; lowercase — for fuzzy name matching."""
    nfd = unicodedata.normalize("NFD", s)
    ascii_s = nfd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"['\-]", " ", ascii_s).lower()).strip()


def _match(p: Province, elements: list[dict], *, tty: bool) -> dict | None:
    """Match Province to an Overpass boundary element. Prompts on TTY if ambiguous."""
    name_lower = p["province"].strip().lower()
    name_norm = _normalize(p["province"])

    # Tier 0: ISO3166-2 code match — authoritative, no name heuristics needed
    iso_code = f"IT-{p['code']}"
    for el in elements:
        tags = el.get("tags", {})
        if tags.get("ISO3166-2") == iso_code or tags.get("ISO3166-2:2") == iso_code:
            return el

    # Tier 1: exact name match
    for el in elements:
        tags = el.get("tags", {})
        for key in ("name", "name:it", "alt_name", "short_name"):
            if tags.get(key, "").strip().lower() == name_lower:
                return el

    # Tier 2: our name is a substring of the OSM name (handles "Città Metropolitana di X")
    candidates = []
    for el in elements:
        tags = el.get("tags", {})
        for key in ("name", "name:it"):
            osm = tags.get(key, "").strip().lower()
            if osm and name_lower in osm:
                candidates.append(el)
                break

    if len(candidates) == 1:
        return candidates[0]

    # Tier 3: accent/punctuation-normalised comparison (e.g. "Forli'-Cesena" ↔ "Forlì-Cesena")
    if not candidates:
        for el in elements:
            tags = el.get("tags", {})
            for key in ("name", "name:it"):
                osm_norm = _normalize(tags.get(key, ""))
                if osm_norm and (name_norm == osm_norm or name_norm in osm_norm):
                    candidates.append(el)
                    break

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) == 0:
        print(f"  [warn   ] {p['code']} {p['province']}: no OSM boundary match", file=sys.stderr)
    else:
        if not tty:
            print(
                f"  [warn   ] {p['code']} {p['province']}: {len(candidates)} ambiguous matches",
                file=sys.stderr,
            )
            return None
        print(f"\n  Multiple OSM boundaries matched '{p['province']}' ({p['code']}):")
        for i, c in enumerate(candidates):
            print(f"    {i + 1}: {c['tags'].get('name', '?')} (osm_id={c['id']})")
        while True:
            try:
                raw = input("  Select [1-N, 0=skip]: ").strip()
                choice = int(raw)
            except (ValueError, EOFError):
                continue
            if choice == 0:
                return None
            if 1 <= choice <= len(candidates):
                return candidates[choice - 1]
    return None


def run(
    all_sources: list[Province],
    selected: list[Province],
    overwrite: bool = False,
    overpass_url: str = _OVERPASS_URL,
) -> None:
    """Compute neighbours for all provinces; write to sources.json.

    `selected` is used to determine whether to exit non-zero on unmatched.
    `overwrite` forces a fresh Overpass fetch (ignores the local cache).
    """
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    elements = _fetch_boundaries(overwrite=overwrite, url=overpass_url)

    # Match each province to an OSM boundary, build shapely geometry
    geoms: dict[str, object] = {}  # code -> shapely geometry
    matched_osm_ids: set[int] = set()
    unmatched: list[str] = []

    for p in all_sources:
        el = _match(p, elements, tty=tty)
        if el is None:
            unmatched.append(p["code"])
            continue
        g = _relation_to_geometry(el)
        if g is None:
            print(f"  [warn   ] {p['code']} {p['province']}: empty boundary geometry", file=sys.stderr)
            unmatched.append(p["code"])
            continue
        geoms[p["code"]] = g
        matched_osm_ids.add(el["id"])

    unmatched_osm = [el for el in elements if el["id"] not in matched_osm_ids]
    print(
        f"  [match  ] {len(geoms)}/{len(all_sources)} provinces matched, "
        f"{len(unmatched)} unmatched; "
        f"{len(unmatched_osm)}/{len(elements)} OSM boundaries unmatched"
    )
    for el in unmatched_osm:
        name = el.get("tags", {}).get("name", f"osm:{el['id']}")
        print(f"  [warn   ] osm:{el['id']} {name}: no province match", file=sys.stderr)

    # Compute pairwise adjacency
    neighbours: dict[str, list[str]] = {code: [] for code in geoms}
    codes = sorted(geoms)
    for i, c1 in enumerate(codes):
        for c2 in codes[i + 1 :]:
            if geoms[c1].distance(geoms[c2]) < _ADJACENCY_THRESHOLD:
                neighbours[c1].append(c2)
                neighbours[c2].append(c1)

    total_pairs = sum(len(v) for v in neighbours.values()) // 2
    print(f"  [adj    ] {total_pairs} adjacent pairs found (threshold={_ADJACENCY_THRESHOLD}°)")

    # Update sources.json: write matched, clear stale for unmatched
    sources_data = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    matched_codes = set(geoms.keys())
    changed = 0
    for p in all_sources:
        code = p["code"]
        if code not in matched_codes:
            if "neighbours" in sources_data.get(code, {}):
                del sources_data[code]["neighbours"]
                changed += 1
        else:
            nbs_sorted = sorted(neighbours[code])
            if sources_data.get(code, {}).get("neighbours") != nbs_sorted:
                sources_data[code]["neighbours"] = nbs_sorted
                changed += 1
    SOURCES_JSON.write_text(json.dumps(sources_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  [write  ] {changed} provinces updated in sources.json")

    # Check if any selected provinces are unmatched
    selected_codes = {p["code"] for p in selected}
    unmatched_selected = [c for c in unmatched if c in selected_codes]
    if unmatched_selected:
        msg = f"unmatched provinces: {', '.join(unmatched_selected)}"
        if not tty:
            sys.exit(f"error: {msg}")
        print(f"  [warn   ] {msg} — neighbours key absent")


def main() -> None:
    def _setup(parser) -> None:
        parser.add_argument(
            "--overpass-url",
            default=_OVERPASS_URL,
            metavar="URL",
            help=f"Overpass API endpoint (default: {_OVERPASS_URL})",
        )

    args = parse_args(
        "Determine province neighbours from OSM administrative boundaries",
        overwrite=True,
        setup=_setup,
    )
    print(f"=== Neighbours ({len(args.provinces)} provinces) ===")
    all_sources = read_sources()
    run(all_sources, args.provinces, overwrite=args.overwrite, overpass_url=args.overpass_url)


if __name__ == "__main__":
    main()
