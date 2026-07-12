"""Run osm-conflate for an ICCU region, producing OSM and GeoJSON change files."""

import subprocess
import sys

import polars as pl

from iccu.common import (
    ALL_REGION,
    CLEAN_CSV,
    OVERPASS_DIR,
    PROFILE_PY,
    changes_geojson,
    changes_osc,
    changes_osm,
    osm_output_dir,
    overpass_cache,
    parse_args,
    parse_regions,
)
from iccu.region import resolve


def run(region: str = ALL_REGION, overwrite: bool = False, osc: bool = False) -> None:
    if not CLEAN_CSV.exists():
        print(f"clean.csv not found at {CLEAN_CSV} — run iccu-clean first.", file=sys.stderr)
        sys.exit(1)

    df = pl.read_csv(CLEAN_CSV).filter(~(pl.col("latitudine").is_null() | pl.col("longitudine").is_null()))
    rf = resolve(region, df)
    print(f"=== Step 4: Conflate ({rf.label}) ===")

    out_dir = osm_output_dir(rf.label)
    out_osm = changes_osc(rf.label) if osc else changes_osm(rf.label)
    out_geojson = changes_geojson(rf.label)

    if not overwrite and out_osm.exists() and out_geojson.exists():
        print(f"Conflate outputs exist for '{rf.label}', skipping (use --overwrite to reprocess).")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    OVERPASS_DIR.mkdir(parents=True, exist_ok=True)
    if overwrite:
        overpass_cache(rf.label).unlink(missing_ok=True)

    cmd = [
        "uv",
        "run",
        "conflate",
        "--source",
        str(CLEAN_CSV),
        "--output",
        str(out_osm),
        "--changes",
        str(out_geojson),
        "--regions",
        rf.conflate_regions,
        "--osm",
        str(overpass_cache(rf.label)),
        str(PROFILE_PY),
    ]
    if osc:
        cmd.insert(3, "--osc")

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _extra_args(p) -> None:
    p.add_argument("--osc", action="store_true", help="Produce osmChange (.osc) instead of JOSM XML (.osm)")


def main() -> None:
    args = parse_args("Step 4: conflate ICCU data with OSM", region=True, setup=_extra_args)
    regions = parse_regions(args.region)
    if not regions:
        sys.exit("error: --region cannot be empty")
    for region in regions:
        run(region, args.overwrite, args.osc)


if __name__ == "__main__":
    main()
