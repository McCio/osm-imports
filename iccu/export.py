"""Export the ICCU dataset (clean.csv) as GeoJSON and/or OSM XML, optionally filtered by region."""

import sys
from collections.abc import Iterator

import polars as pl

from iccu.common import (
    ALL_REGION,
    CLEAN_CSV,
    dataset_geojson,
    dataset_osm,
    osm_output_dir,
    parse_args,
    parse_regions,
)
from iccu.region import RegionFilter, resolve
from utils.writers import write_geojson, write_osm

COL_MAPPER = {
    "codice-isil": "ref:isil",
    "acnp": "ref:acnp",
    "cei": "ref:cei",
    "cmbs": "ref:cmbs",
    "rism": "ref:rism",
    "sbn": "ref:sbn",
    "denominazione": "official_name",
    "denominazioni-precedenti": "old_name",
    "denominazioni-alternative": "alt_name",
    "address_street": "addr:street",
    "address_housenumber": "addr:housenumber",
    "valore_contact_phone": "contact:phone",
    "valore_contact_fax": "contact:fax",
    "valore_contact_email": "contact:email",
    "valore_contact_website": "contact:website",
    "valore_contact_instagram": "contact:instagram",
    "valore_contact_facebook": "contact:facebook",
    "valore_contact_twitter": "contact:twitter",
    "valore_contact_whatsapp": "contact:whatsapp",
    "access": "access",
    "wheelchair": "wheelchair",
    "cap": "addr:postcode",
    "ente": "operator",
}

_OSM_KEYS = frozenset(COL_MAPPER.values()) | {"amenity"}

_GEOJSON_SCHEMA = {
    "geometry": "Point",
    "properties": dict.fromkeys(_OSM_KEYS, "str"),
}


def _df_to_features(df: pl.DataFrame) -> Iterator[dict]:
    df = df.rename({k: v for k, v in COL_MAPPER.items() if k in df.columns})
    for row in df.to_dicts():
        lat = float(row["latitudine"])
        lon = float(row["longitudine"])
        yield {
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: v for k, v in row.items() if k in _OSM_KEYS},
        }


def _translate(props: dict) -> dict:
    return {"amenity": "library", **{k: str(v) for k, v in props.items() if v is not None and str(v).strip()}}


def _load(region: str) -> tuple[pl.DataFrame, RegionFilter]:
    if not CLEAN_CSV.exists():
        print(f"clean.csv not found at {CLEAN_CSV} — run iccu-clean first.", file=sys.stderr)
        sys.exit(1)
    df = pl.read_csv(CLEAN_CSV, schema_overrides={"cap": pl.Utf8}).filter(~(pl.col("latitudine").is_null() | pl.col("longitudine").is_null()))
    rf = resolve(region, df)
    if rf.expr is not None:
        df = df.filter(rf.expr)
    return df, rf


def run(region: str = ALL_REGION, overwrite: bool = False, fmt: str = "both", compress: bool = False) -> None:
    df, rf = _load(region)
    label = rf.label
    print(f"=== Step 3: Export ({label}) ===")

    if not overwrite:
        targets = []
        if fmt in ("geojson", "both"):
            targets.append(dataset_geojson(label))
        if fmt in ("osm", "both"):
            targets.append(dataset_osm(label, compress))
        if all(t.exists() for t in targets):
            print(f"Export outputs exist for '{label}', skipping (use --overwrite to reprocess).")
            return

    osm_output_dir(label).mkdir(parents=True, exist_ok=True)
    features = list(_df_to_features(df))

    if fmt in ("geojson", "both"):
        n = write_geojson(features, dataset_geojson(label), _translate, _GEOJSON_SCHEMA)
        print(f"Written: {dataset_geojson(label)} ({n} features)")
    if fmt in ("osm", "both"):
        p = dataset_osm(label, compress)
        n = write_osm(features, p, _translate)
        print(f"Written: {p} ({n} nodes)")


def _extra_args(p) -> None:
    p.add_argument(
        "--format",
        choices=["osm", "geojson", "both"],
        default="both",
        metavar="osm|geojson|both",
        help="Output format (default: both)",
    )
    p.add_argument("--compress", action="store_true", help="Compress OSM output as .osm.bz2")


def main() -> None:
    args = parse_args("Step 3: export ICCU dataset as GeoJSON and/or OSM XML", region=True, setup=_extra_args)
    regions = parse_regions(args.region)
    if not regions:
        sys.exit("error: --region cannot be empty")
    for region in regions:
        run(region, args.overwrite, args.format, args.compress)


if __name__ == "__main__":
    main()
