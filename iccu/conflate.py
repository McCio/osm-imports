"""Run osm-conflate for an ICCU region, producing OSM and GeoJSON change files."""

import os
import re
import shutil
import sys
import tempfile

import polars as pl
from conflate.conflate import run as _conflate_run

from iccu.export import CSV_SCHEMA_OVERRIDES
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
    safe_label,
)
from iccu.region import resolve


def run(region: str = ALL_REGION, overwrite: bool = False, osc: bool = False, overpass_url: str | None = None, contact: str | None = None) -> None:
    print(f"=== Step 4: Conflate ({safe_label(region)}) ===")

    if not CLEAN_CSV.exists():
        print(f"clean.csv not found at {CLEAN_CSV} — run iccu-clean first.", file=sys.stderr)
        sys.exit(1)

    df = pl.read_csv(CLEAN_CSV, schema_overrides=CSV_SCHEMA_OVERRIDES).filter(~(pl.col("latitudine").is_null() | pl.col("longitudine").is_null()))
    rf = resolve(region, df)

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
        shutil.rmtree(str(overpass_cache(rf.label)) + ".d", ignore_errors=True)

    # Write a filtered CSV so conflate's bbox covers only this region/province,
    # not all 13k Italy rows (which causes Overpass timeouts).
    if rf.expr is not None:
        filtered_df = df.filter(rf.expr)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="wb") as tmp:
            source_csv = tmp.name
        filtered_df.write_csv(source_csv)
    else:
        source_csv = str(CLEAN_CSV)

    if rf.admin_level is not None:
        area_name = rf.osm_name if rf.osm_name is not None else re.escape(rf.conflate_regions)
        # The ",i" flag relies on ICU for non-ASCII case folding (e.g. Ü↔ü, É↔é).
        # overpass-api.de uses ICU; self-hosted Overpass instances compiled without it
        # may silently return nothing for names like SÜDTIROL or VALLÉE.
        area_filter = f'area["name"~"^{area_name}$",i]["admin_level"="{rf.admin_level}"]->.a'
    else:
        area_filter = None

    try:
        _conflate_run(
            profile=PROFILE_PY,
            source=source_csv,
            output=out_osm,
            changes=out_geojson,
            regions=rf.conflate_regions,
            osm=overpass_cache(rf.label),
            overpass_url=overpass_url,
            overpass_area=area_filter,
            contact=contact,
            osc=osc,
        )
    finally:
        if rf.expr is not None:
            os.unlink(source_csv)


def _extra_args(p) -> None:
    p.add_argument(
        "--osc", action="store_true", help="Produce osmChange (.osc) instead of JOSM XML (.osm) as conflation result"
    )
    p.add_argument("--overpass-url", dest="overpass_url", metavar="URL", help="Custom Overpass API endpoint")
    p.add_argument("--contact", metavar="REF", help="Contact reference for Overpass User-Agent (URL or email)")


def main() -> None:
    args = parse_args("Step 4: conflate ICCU data with OSM", region=True, setup=_extra_args)
    regions = parse_regions(args.region)
    if not regions:
        sys.exit("error: --region cannot be empty")
    for region in regions:
        run(region, args.overwrite, args.osc, args.overpass_url, args.contact)


if __name__ == "__main__":
    main()
