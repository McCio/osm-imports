"""Inspect DBSN GDB layers intersecting a lat/lon area.

Usage:
    dbsn-inspect --lat 45.452966 --lon 12.300224
    dbsn-inspect --lat 45.45 --lon 12.30 --radius 1000 --layers man_tr,sv_str
    dbsn-inspect --province VE --lat 45.45 --lon 12.30   # skip auto-detect
"""

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import fiona
from pyproj import Transformer
from shapely.geometry import MultiLineString, Point, shape
from shapely.ops import polygonize, unary_union

from dbsn.common import ZIPS_DIR, Province, http_client, read_sources
from dbsn.download import _download_province
from dbsn.neighbours import _BOUNDARIES_CACHE, _fetch_boundaries


def _province_codes_for_point(lat: float, lon: float) -> list[str]:
    """Return province codes whose boundary contains (lat, lon)."""
    elements = _fetch_boundaries()
    pt = Point(lon, lat)
    codes: list[str] = []
    for el in elements:
        bounds = el.get("bounds", {})
        if not (bounds.get("minlat", 91) <= lat <= bounds.get("maxlat", -91)
                and bounds.get("minlon", 181) <= lon <= bounds.get("maxlon", -181)):
            continue
        rings = [
            [(p["lon"], p["lat"]) for p in m.get("geometry", [])]
            for m in el.get("members", [])
            if m.get("type") == "way" and m.get("role") == "outer" and len(m.get("geometry", [])) >= 3
        ]
        if not rings:
            continue
        poly = unary_union(list(polygonize(MultiLineString(rings))))
        # ponytail: fall back to bbox when polygonize fails on complex/island boundaries
        if poly.is_empty:
            pass  # bbox pre-filter already matched; accept
        elif not poly.contains(pt):
            continue
        tags = el.get("tags", {})
        iso = tags.get("ISO3166-2") or tags.get("ISO3166-2:2", "")
        if iso.startswith("IT-"):
            codes.append(iso[3:])
    return codes


def _ensure_zip(p: Province) -> Path:
    zip_path = ZIPS_DIR / p["zip_name"]
    if not zip_path.exists():
        print(f"  [download] {p['code']} {p['province']}: ZIP missing, fetching...")
        with http_client() as client:
            if not _download_province(client, p, overwrite=False):
                raise SystemExit(f"Download failed for {p['code']}")
    return zip_path


def _geom_type(props: dict) -> str:
    """Classify feature geometry type from property keys (case-insensitive)."""
    lower = {k.lower() for k in props}
    if "shape_area" in lower:
        return "area"
    if "shape_length" in lower:
        return "line"
    return "point"


def _search_zip(
    zip_path: Path,
    lat: float,
    lon: float,
    radius: float,
    want_layers: set[str] | None,
    geom_filter: set[str],  # subset of {"area","line","point"}
) -> int:
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        gdbs = sorted(Path(tmp).rglob("*.gdb"))
        if not gdbs:
            print(f"[warn] no .gdb in {zip_path.name}")
            return 0
        gdb = gdbs[0]

        all_layers = fiona.listlayers(str(gdb))
        layers = [l for l in all_layers if want_layers is None or l in want_layers]
        if want_layers:
            missing = want_layers - set(all_layers)
            if missing:
                print(f"[warn] layers not in GDB: {', '.join(sorted(missing))}")

        with fiona.open(str(gdb), layer=layers[0]) as f:
            native_crs = f.crs

        t = Transformer.from_crs("EPSG:4326", native_crs, always_xy=True)
        tx, ty = t.transform(lon, lat)
        buf = Point(tx, ty).buffer(radius)
        print(f"Target in {zip_path.stem}: ({lat}, {lon}) → ({tx:.1f}, {ty:.1f}) [{native_crs}], radius={radius}m\n")

        for layer in layers:
            hits = []
            with fiona.open(str(gdb), layer=layer) as f:
                for feat in f:
                    if feat["geometry"] and shape(feat["geometry"]).intersects(buf):
                        props = dict(feat["properties"])
                        if _geom_type(props) in geom_filter:
                            hits.append(props)
            if hits:
                print(f"=== {layer} ({len(hits)} features) ===")
                for p in hits:
                    print(" ", {k: v for k, v in p.items() if v is not None and v != "UNK"})
                total += len(hits)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Find DBSN features intersecting a lat/lon area")
    ap.add_argument("--province", "-p", help="Province code (e.g. VE); auto-detected from coords if omitted")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius", type=float, default=500, help="Search radius in metres (default: 500)")
    ap.add_argument("--layers", help="Comma-separated layer names (default: all)")
    all_types = {"area", "line", "point"}
    for gtype in all_types:
        grp = ap.add_mutually_exclusive_group()
        grp.add_argument(f"--{gtype}", dest=gtype, action="store_true", default=None, help=f"Include {gtype} features")
        grp.add_argument(f"--no-{gtype}", dest=gtype, action="store_false", help=f"Exclude {gtype} features")
    args = ap.parse_args()
    # explicit --X sets True; explicit --no-X sets False; None = unset
    explicitly_included = {t for t in all_types if getattr(args, t) is True}
    explicitly_excluded = {t for t in all_types if getattr(args, t) is False}
    if explicitly_included:
        geom_filter = explicitly_included  # only what was asked for
    else:
        geom_filter = all_types - explicitly_excluded  # default all, minus exclusions

    want_layers = {l.strip() for l in args.layers.split(",")} if args.layers else None
    sources_by_code = {p["code"]: p for p in read_sources()}

    if args.province:
        codes = [args.province.upper()]
    else:
        print(f"Auto-detecting province for ({args.lat}, {args.lon})...")
        codes = _province_codes_for_point(args.lat, args.lon)
        if not codes:
            raise SystemExit("No province boundary contains this point")
        print(f"Detected: {', '.join(codes)}\n")

    total = 0
    for code in codes:
        p = sources_by_code.get(code)
        if not p:
            print(f"[warn] province {code} not in sources, skipping")
            continue
        zip_path = _ensure_zip(p)
        total += _search_zip(zip_path, args.lat, args.lon, args.radius, want_layers, geom_filter)

    if total == 0:
        print("No features found.")
    else:
        print(f"\nTotal: {total} features")


if __name__ == "__main__":
    main()
