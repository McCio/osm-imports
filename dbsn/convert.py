"""Step 3: Convert FlatGeobuf buildings to OSM XML or GeoJSON."""

import sys

import fiona

from dbsn.common import BUILDINGS_DIR, OSM_DIR, Province, parse_args, rel
from dbsn.translate import TAG_KEYS, translate
from utils.writers import write_geojson, write_osm

_GEOJSON_SCHEMA = {"geometry": "Unknown", "properties": dict.fromkeys(TAG_KEYS, "str")}


def _convert_province(p: Province, overwrite: bool, fmt: str = "osm", compress: bool = False) -> bool | None:
    in_fgb = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"
    if not in_fgb.exists():
        print(f"  [skip   ] {p['code']} {p['province']}: FGB not found, run extract first")
        return None

    OSM_DIR.mkdir(parents=True, exist_ok=True)
    if fmt == "geojson":
        ext = "geojson"
    elif compress:
        ext = "osm.bz2"
    else:
        ext = "osm"
    out_path = OSM_DIR / f"{p['code']}_{p['date']}.{ext}"
    if out_path.exists() and not overwrite:
        size = out_path.stat().st_size // 1024
        print(f"  [skip   ] {p['code']} {p['province']}: {rel(out_path)} ({size}KB) (use --overwrite to reprocess)")
        return True

    if out_path.exists():
        out_path.unlink()

    print(f"  [convert] {p['code']} {p['province']}: {rel(in_fgb)} → {rel(out_path)}")
    try:
        with fiona.open(str(in_fgb)) as src:
            if fmt == "geojson":
                count_written = write_geojson(src, out_path, translate, _GEOJSON_SCHEMA)
            else:
                try:
                    bounds = src.bounds
                except Exception as exc:
                    print(
                        f"  [warn   ] {p['code']} {p['province']}: bounds unavailable ({exc}), omitting bbox",
                        file=sys.stderr,
                    )
                    bounds = None
                count_written = write_osm(src, out_path, translate, bounds)
        size = out_path.stat().st_size // 1024
        print(f"  [done   ] {p['code']} {p['province']}: {rel(out_path)} ({count_written} features, {size}KB)")
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}", file=sys.stderr)
        if out_path.exists():
            out_path.unlink()
        return False


def run(provinces: list[Province], overwrite: bool, fmt: str = "osm", compress: bool = False) -> None:
    print(f"=== Step 3: Convert ({len(provinces)} provinces, format={fmt}{', compressed' if compress else ''}) ===")
    ok = failed = skipped = 0
    for p in provinces:
        result = _convert_province(p, overwrite, fmt, compress)
        if result is True:
            ok += 1
        elif result is None:
            skipped += 1
        else:
            failed += 1
    print(f"\nDone: {ok} ok, {skipped} skipped, {failed} failed")
    if failed:
        sys.exit(1)


def _extra_args(p) -> None:
    p.add_argument(
        "--format",
        choices=["osm", "geojson"],
        default="osm",
        metavar="osm|geojson",
        help="Output format (default: osm)",
    )
    p.add_argument("--compress", action="store_true", help="Compress OSM output as .osm.bz2")


def main() -> None:
    args = parse_args("Step 3: convert FlatGeobuf to OSM XML or GeoJSON", setup=_extra_args)
    if args.compress and args.format == "geojson":
        sys.exit("error: --compress only applies to --format osm")
    run(args.provinces, args.overwrite, args.format, args.compress)


if __name__ == "__main__":
    main()
