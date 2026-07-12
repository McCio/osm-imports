"""Validate clean.csv for data quality issues before export/conflation."""

import sys

import polars as pl

from iccu.common import CLEAN_CSV, parse_args
from iccu.export import CSV_SCHEMA_OVERRIDES

# Italy + islands bounding box (generous margins)
_LAT_MIN, _LAT_MAX = 35.4, 47.2
_LON_MIN, _LON_MAX = 6.6, 18.6


def run() -> int:
    """Validate clean.csv. Returns number of pipeline-breaking errors found.

    Errors   → exit 1; block the pipeline (e.g. duplicate ISIL, bad coordinates that
               would produce wrong conflation output).
    Warnings → logged to stderr but exit 0; source-data issues we cannot auto-fix
               (e.g. a library geocoded in the wrong country).
    """
    if not CLEAN_CSV.exists():
        print(f"clean.csv not found at {CLEAN_CSV} — run iccu-clean first.", file=sys.stderr)
        sys.exit(1)

    df = pl.read_csv(CLEAN_CSV, schema_overrides=CSV_SCHEMA_OVERRIDES)
    errors = 0
    warnings = 0

    def _err(msg: str) -> None:
        nonlocal errors
        errors += 1
        print(f"  [error] {msg}", file=sys.stderr)

    def _warn(msg: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"  [warn]  {msg}", file=sys.stderr)

    # 1. ISIL uniqueness — duplicates cause conflation to match two records to the same OSM object
    for code in (
        df.filter(pl.col("codice-isil").is_duplicated()).select("codice-isil").unique()["codice-isil"].to_list()
    ):
        _err(f"duplicate ISIL: {code!r}")

    # 2. ISIL format
    for row in df.filter(~pl.col("codice-isil").str.contains(r"^IT-[A-Z]{2}\d+$")).select("codice-isil").to_dicts():
        _err(f"malformed ISIL: {row['codice-isil']!r}")

    # 3. Missing name — required OSM tag
    for row in (
        df.filter(pl.col("denominazione").is_null() | pl.col("denominazione").eq("")).select("codice-isil").to_dicts()
    ):
        _err(f"missing name: {row['codice-isil']}")

    # 4. Missing region / province / comune — required for region filtering
    for col in ("regione", "provincia", "comune"):
        if col not in df.columns:
            continue
        for row in df.filter(pl.col(col).is_null() | pl.col(col).eq("")).select("codice-isil").to_dicts():
            _err(f"missing {col}: {row['codice-isil']}")

    # 5. Likely swapped lat/lon — Italian lat ∈ [35.4, 47.2] never overlaps lon ∈ [6.6, 18.6];
    #    a lat value in the lon range means the pair is definitely transposed → wrong conflation.
    has_coords = df.filter(pl.col("latitudine").is_not_null() & pl.col("longitudine").is_not_null())
    for row in (
        has_coords.filter(
            pl.col("latitudine").is_between(_LON_MIN, _LON_MAX) & pl.col("longitudine").is_between(_LAT_MIN, _LAT_MAX)
        )
        .select("codice-isil", "latitudine", "longitudine")
        .to_dicts()
    ):
        _err(f"swapped lat/lon: {row['codice-isil']} lat={row['latitudine']} lon={row['longitudine']}")

    # 6. Coordinates outside Italy bounding box — warn only; source-data error we cannot fix,
    #    conflation will simply not match the library to any Italian OSM object.
    for row in (
        has_coords.filter(
            (pl.col("latitudine") < _LAT_MIN)
            | (pl.col("latitudine") > _LAT_MAX)
            | (pl.col("longitudine") < _LON_MIN)
            | (pl.col("longitudine") > _LON_MAX)
        )
        .select("codice-isil", "latitudine", "longitudine")
        .to_dicts()
    ):
        _warn(f"coordinates outside Italy: {row['codice-isil']} lat={row['latitudine']} lon={row['longitudine']}")

    total = len(df)
    summary = f"{total} libraries: {errors} error(s), {warnings} warning(s)."
    if errors:
        print(f"Validation FAILED — {summary}", file=sys.stderr)
    else:
        print(f"Validation OK — {summary}")
    return errors


def main() -> None:
    parse_args("Validate clean.csv for data quality issues", overwrite=False)
    n = run()
    sys.exit(min(n, 1))


if __name__ == "__main__":
    main()
