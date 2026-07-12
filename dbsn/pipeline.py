"""Run all pipeline steps in sequence: discover → download → extract → convert → validate."""

import sys

from dbsn import convert, discover, download, extract, validate
from dbsn.common import SOURCES_JSON, filter_provinces, parse_args
from dbsn.common import read_sources as _read_sources


def _extra_args(p) -> None:
    convert._extra_args(p)
    validate._extra_args(p)


def main() -> None:
    args = parse_args(
        "Run all DBSN pipeline steps: discover → download → extract → convert → validate",
        resolve_provinces=False,
        setup=_extra_args,
    )
    if args.overwrite or not SOURCES_JSON.exists() or args.province.lower() == "all":
        sources = discover.run(args.overwrite)
    else:
        print("=== Step 0: Discover (skipped — sources.json exists, province filtered) ===")
        sources = _read_sources()
    provinces = filter_provinces(sources, args.province)
    if not provinces:
        sys.exit(f"No province matched '{args.province}'")
    download.run(provinces, args.overwrite)
    extract.run(provinces, args.overwrite)
    if args.compress and args.format == "geojson":
        sys.exit("error: --compress only applies to --format osm")
    convert.run(provinces, args.overwrite, args.format, args.compress)
    validate.run(provinces, args.delete_invalid)


if __name__ == "__main__":
    main()
