"""Shared paths, types, and helpers for the DBSN pipeline."""

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict

import fiona
import httpx
import shapely.ops
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape

# ---------------------------------------------------------------------------
# Italian title-case for DBSN names (ALL CAPS in source)
# ---------------------------------------------------------------------------

_IT_LOWER = frozenset({
    # articles
    "il", "lo", "la", "i", "gli", "le",
    # simple prepositions / conjunctions
    "di", "a", "da", "in", "con", "su", "per", "tra", "fra",
    "e", "ed", "o", "od", "né", "ma", "se", "che", "come",
    # contracted preposition+article
    "del", "dello", "della", "dei", "degli", "delle",
    "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "nel", "nello", "nella", "nei", "negli", "nelle",
    "sul", "sullo", "sulla", "sui", "sugli", "sulle",
    "al", "allo", "alla", "ai", "agli", "alle",
    "col", "coi",
})

# Words containing a dot are treated as abbreviations/acronyms → kept as-is.
# e.g. S., S.P.A., S.R.L., SS., U.S.L.
_HAS_DOT = re.compile(r"\.")

# Pre-apostrophe elision forms (dell’, nell’, sull’, all’, dall’, l’, un’)
_IT_ELISION = frozenset({
    "dell", "dall", "nell", "sull", "all", "coll", "l", "un",
})

# Match any apostrophe variant (straight U+0027, curly U+2018/2019, modifier U+02BC)
_APOS_RE = re.compile("[\x27‘’ʼ]")


def _titlecase_word(w: str, first: bool) -> str:
    """Apply Italian title-case rules to a single space-separated token."""
    if _HAS_DOT.search(w):
        return w  # abbreviation / acronym: keep exactly
    m = _APOS_RE.search(w)
    if m:
        pre, post = w[:m.start()], w[m.end():]
        pre_low = pre.lower()
        pre_out = pre_low if (not first and pre_low in _IT_ELISION) else pre.capitalize()
        post_out = post.capitalize() if post else ""
        return f"{pre_out}'{post_out}"
    lower = w.lower()
    if not first and lower in _IT_LOWER:
        return lower
    return w.capitalize()


def it_titlecase(name: str) -> str:
    """Convert an ALL-CAPS Italian building name to title case."""
    if not name or name.upper() == "UNK":
        return ""
    tokens = name.split()
    return " ".join(_titlecase_word(t, i == 0) for i, t in enumerate(tokens))


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "dbsn"
TSV_CACHE = DATA_DIR / "dbsn.tsv"
SOURCES_JSON = DATA_DIR / "sources.json"
ZIPS_DIR = DATA_DIR / "zips"
UNZIPPED_DIR = DATA_DIR / "unzipped"
BUILDINGS_DIR = DATA_DIR / "buildings"
OSM_DIR = DATA_DIR / "osm"

CONFLATE_DIR = DATA_DIR / "conflate"

EXCLUDE_META_IST: frozenset[str] = frozenset(("03", "21", "23"))
OVERWRITE_STEPS: tuple[str, ...] = (
    "discover", "neighbours", "download", "extract", "extend", "convert", "validate",
    "pbf", "conflate",
)

IGM_DOWNLOAD_URL = (
    "https://igmi.esercito.difesa.it/servizi/database-di-sintesi-nazionale/database-di-sintesi-nazionale-download/"
)
DANYSAN1_TSV_URL = "https://raw.githubusercontent.com/Danysan1/dbsn-import/main/dbsn.tsv"

USER_AGENT = (
    "dbsn-pipeline/0.1 (OSM DBSN buildings import; https://wiki.openstreetmap.org/wiki/Import/Catalogue/DBSN/Buildings)"
)


def rel(path) -> str:
    try:
        return str(Path(path).relative_to(DATA_DIR))
    except ValueError:
        return Path(path).name


def fmt_size(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    return f"{n // 1024} KB"


def http_client(**kwargs) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, **kwargs)


class Province(TypedDict):
    region: str
    province: str
    code: str
    zip_name: str
    igm_url: str
    wmit_url: str
    date: str
    tsv_date: str
    status: str
    zip_size: int | None
    igm_zip_size: NotRequired[int | None]
    wmit_zip_size: NotRequired[int | None]
    neighbours: NotRequired[list[str]]


def max_date() -> str:
    return max(p["date"] for p in read_sources())


def read_sources() -> list[Province]:
    if SOURCES_JSON.exists():
        data = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
        result = []
        for code, v in data.items():
            p: Province = Province(
                code=code,
                zip_name=f"{code}_{v['date']}.zip",
                province=v.get("province", ""),
                region=v.get("region", ""),
                date=v["date"],
                igm_url=v["igm_url"],
                wmit_url=v.get("wmit_url", v["igm_url"]),
                tsv_date=v.get("tsv_date", v["date"]),
                status=v.get("status", "ok"),
                zip_size=v.get("zip_size"),
            )
            if "igm_zip_size" in v:
                p["igm_zip_size"] = v["igm_zip_size"]
            if "wmit_zip_size" in v:
                p["wmit_zip_size"] = v["wmit_zip_size"]
            if "neighbours" in v:
                p["neighbours"] = v["neighbours"]
            result.append(p)
        return result

    if TSV_CACHE.exists():
        return _parse_tsv(TSV_CACHE)
    with http_client() as client:
        resp = client.get(DANYSAN1_TSV_URL)
        resp.raise_for_status()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TSV_CACHE.write_text(resp.text, encoding="utf-8")
    return _parse_tsv(TSV_CACHE)


def _parse_tsv(path: Path) -> list[Province]:
    provinces = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(
            f,
            delimiter="\t",
            fieldnames=[
                "region",
                "province",
                "file",
                "url_wmit",
                "url_igm",
                "date",
                "latest",
            ],
        )
        next(reader)
        for row in reader:
            if row["latest"].strip() != "yes":
                continue
            code = row["file"][:2].upper()
            date = row["date"].strip()
            url_igm = row["url_igm"].strip()
            raw_wmit = row["url_wmit"].strip()
            url_wmit = raw_wmit if raw_wmit not in ("TODO", "") else url_igm
            provinces.append(
                Province(
                    region=row["region"].strip(),
                    province=row["province"].strip(),
                    code=code,
                    zip_name=f"{code}_{date}.zip",
                    igm_url=url_igm,
                    wmit_url=url_wmit,
                    date=date,
                    tsv_date=date,
                    status="ok",
                )
            )
    return provinces


def filter_provinces(provinces: list[Province], selector: str) -> list[Province]:
    """Selector: 'all', or comma-separated codes/names/regions."""
    if selector.lower() == "all":
        return provinces
    terms = [s.strip() for s in selector.split(",") if s.strip()]
    seen: set[str] = set()
    result: list[Province] = []
    for term in terms:
        upper, lower = term.upper(), term.lower()
        for p in provinces:
            if p["code"] in seen:
                continue
            if p["code"] == upper or p["province"].lower() == lower or p["region"].lower() == lower:
                seen.add(p["code"])
                result.append(p)
    return result


def parse_overwrite(raw: list[str] | None) -> frozenset[str]:
    """Normalise --overwrite values (CSV, repeated, bare/all) → frozenset of step names."""
    if raw is None:
        return frozenset()
    steps: set[str] = set()
    for item in raw:
        for step in item.split(","):
            step = step.strip()
            if step == "all":
                return frozenset(OVERWRITE_STEPS)
            if step:
                steps.add(step)
    unknown = steps - frozenset(OVERWRITE_STEPS)
    if unknown:
        sys.exit(f"error: unknown --overwrite step(s): {', '.join(sorted(unknown))}. "
                 f"Valid: {', '.join(OVERWRITE_STEPS)} (or 'all')")
    return frozenset(steps)


def parse_args(
    description: str,
    *,
    overwrite: bool = True,
    resolve_provinces: bool = True,
    default_province: str | None = None,
    setup: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--province",
        "-p",
        required=default_province is None,
        default=default_province,
        metavar="CODE|NAME|REGION[,...]|all",
        help="Comma-separated province codes, province names, region names, or 'all'"
        + (f" (default: {default_province})" if default_province else ""),
    )
    if overwrite:
        parser.add_argument(
            "--overwrite",
            action="append",
            nargs="?",
            const="all",
            metavar="STEP[,STEP...]",
            help=f"Steps to re-process ({', '.join(OVERWRITE_STEPS)}) or 'all'; bare --overwrite = all",
        )
    if setup:
        setup(parser)
    args = parser.parse_args()
    if resolve_provinces:
        args.provinces = filter_provinces(read_sources(), args.province)
        if not args.provinces:
            parser.error(f"No province matched '{args.province}'")
    return args


def default_max_weight() -> int:
    return max(1, int((os.cpu_count() or 4) * 0.75))


def add_max_weight_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-weight",
        type=int,
        default=default_max_weight(),
        metavar="N",
        help=f"Max concurrent weight (validate=2, others=1; default: {default_max_weight()})",
    )


def add_layers_arg(parser: argparse.ArgumentParser, all_names: list[str]) -> None:
    parser.add_argument(
        "--layers",
        metavar="LAYER[,LAYER...]",
        help=f"Comma-separated layer names to process (default: all). Available: {', '.join(all_names)}",
    )


def parse_layers(raw: str | None, all_names: list[str]) -> list[str] | None:
    """Return selected layer names, or None meaning 'all'."""
    if raw is None:
        return None
    names = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [n for n in names if n not in all_names]
    if unknown:
        raise ValueError(f"Unknown layer(s): {', '.join(unknown)}. Available: {', '.join(all_names)}")
    return names


def _max_date_main() -> None:
    print(max_date())


def add_area_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--area",
        metavar="LOCALITY",
        help="Filter output to features intersecting this loc_sg locality (e.g. 'Marghera')",
    )


def add_osm_area_arg(parser: argparse.ArgumentParser) -> None:
    """--osm-area: OSM-side clip, conflate step only."""
    parser.add_argument(
        "--osm-area",
        dest="osm_area",
        metavar="NAME|r/ID|w/ID",
        help="Clip also to an OSM area: admin relation or named area (relation or "
             "closed way) by name or id, read from the province PBF; intersected "
             "with --area when both given. Outputs get an extra name slug",
    )


def load_combined_clip(p: Province, area: str | None, osm_area: str | None):
    """Load --area (DBSN loc_sg) and --osm-area (OSM geometry) clips; return their
    intersection, the only geometry when just one is given, or None when neither."""
    if not area and not osm_area:
        return None
    clip = None
    if area:
        clip = load_area_clip(p, area)
    if osm_area:
        osm_clip = load_osm_area_clip(p, osm_area)
        clip = osm_clip if clip is None else (clip.intersection(osm_clip) if osm_clip is not None else clip)
        if clip is not None and clip.is_empty:
            print("  [warn   ] --area and --osm-area do not overlap: nothing will be processed", file=sys.stderr)
    return clip


def load_area_clip(p: Province, area_name: str):
    """Return a WGS84 shapely geometry covering the named loc_sg locality, or None if unavailable."""
    zip_path = ZIPS_DIR / p["zip_name"]
    if not zip_path.exists():
        print(f"  [warn   ] --area: zip not found ({zip_path.name}), skipping area filter", file=sys.stderr)
        return None

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    gdb_inner = next(
        (re.match(r"^(.*\.gdb)/", n).group(1) for n in names if re.match(r"^.*\.gdb/", n)),
        None,
    )
    if gdb_inner is None:
        print(f"  [warn   ] --area: no GDB found in {zip_path.name}, skipping area filter", file=sys.stderr)
        return None

    vsizip_path = f"/vsizip/{zip_path}/{gdb_inner}"
    try:
        with fiona.open(vsizip_path, layer="loc_sg") as src:
            t = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            geoms = [
                shapely.ops.transform(t.transform, shapely_shape(feat["geometry"]))
                for feat in src
                if feat["geometry"] and feat["properties"].get("loc_sg_top", "").lower() == area_name.lower()
            ]
    except Exception as exc:
        print(f"  [warn   ] --area: could not read loc_sg ({exc}), skipping area filter", file=sys.stderr)
        return None

    if not geoms:
        print(f"  [warn   ] --area: no loc_sg match for '{area_name}', skipping area filter", file=sys.stderr)
        return None

    return shapely.ops.unary_union(geoms)


def parse_osm_area_selector(osm_area: str) -> tuple[str, int | None, str | None]:
    """Split an --osm-area selector into (kind, id, lowercased name): id form or name form."""
    m = re.fullmatch(r"([rRwW])/(\d+)", osm_area.strip())
    if m:
        return m.group(1).lower(), int(m.group(2)), None
    return "", None, osm_area.strip().lower()


def load_osm_area_clip(p: Province, osm_area: str):
    """Return a WGS84 shapely geometry of an OSM area in this province.

    Selector: 'r/ID' or 'w/ID' matches one element; otherwise a case-insensitive
    exact-name match over named relations (boundaries, multipolygon areas) and
    named closed ways in the cached province PBF (requires the conflate PBF,
    downloaded by dbsn-conflate). Multiple matches are unioned.
    """
    from dbsn.download_pbf import find_pbf  # noqa: PLC0415 — avoids import cycle at module load

    pbf = find_pbf(p["code"])
    if pbf is None:
        print(f"  [warn   ] --osm-area: no PBF for {p['code']}, run dbsn-conflate first", file=sys.stderr)
        return None

    import osmium  # noqa: PLC0415 — heavy parser, conflate-only
    from shapely.geometry import LineString, Polygon  # noqa: PLC0415
    from shapely.ops import polygonize, unary_union  # noqa: PLC0415

    kind, wanted_id, name_sel = parse_osm_area_selector(osm_area)

    class _RelHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.matches: dict[int, list[int]] = {}  # rel id → outer member way ids
            self.names: dict[int, str] = {}

        def relation(self, r):
            name = (r.tags.get("name") or "").strip()
            hit = (kind == "r" and wanted_id == r.id) or (
                name_sel is not None and name and name.lower() == name_sel
                and r.tags.get("type") in ("boundary", "multipolygon")
            )
            if hit:
                outers = [m.ref for m in r.members if m.type == "w" and m.role in ("", "outer")]
                if outers:
                    self.matches[r.id] = outers
                    self.names[r.id] = name

    rels = _RelHandler()
    rels.apply_file(str(pbf), locations=False, idx="flex_mem")
    member_way_ids = {wid for ids in rels.matches.values() for wid in ids}

    class _WayHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.pts: dict[int, list] = {}

        def way(self, w):
            named = (w.tags.get("name") or "").strip()
            pick = w.id in member_way_ids or (
                name_sel is not None and w.is_closed() and named
                and named.lower() == name_sel and len(w.nodes) >= 4
            ) or (kind == "w" and wanted_id == w.id and w.is_closed())
            if pick:
                self.pts[w.id] = [(n.location.lon, n.location.lat) for n in w.nodes]

    ways = _WayHandler()
    ways.apply_file(str(pbf), locations=True, idx="flex_mem")

    geoms = []
    labels = []
    for rid, ids in rels.matches.items():
        rings = [LineString(pts) for pts in (ways.pts.get(w) for w in ids) if pts]
        if not rings:
            continue
        poly = unary_union(list(polygonize(rings)))
        if not poly.is_empty:
            geoms.append(poly)
            labels.append(f"r/{rid} {rels.names[rid] or '(unnamed)'}")
    for wid, pts in ways.pts.items():
        if len(pts) >= 4 and pts[0] == pts[-1]:
            geoms.append(Polygon(pts))
            labels.append(f"w/{wid}")

    if not geoms:
        print(f"  [warn   ] --osm-area: no match for '{osm_area}' in {pbf.name}", file=sys.stderr)
        return None
    print(f"  [osm-are] {p['code']}: {osm_area} → {', '.join(labels)}")
    return unary_union(geoms)
