"""Print resolved region labels for a --region input (used by scripts and the GH Actions workflow)."""

import sys

import polars as pl

from iccu.common import ALL_REGION, CLEAN_CSV, parse_args, parse_regions
from iccu.region import resolve


def main() -> None:
    args = parse_args("Print resolved region labels, one per line", region=True, overwrite=False)
    regions = parse_regions(args.region)
    if not regions:
        sys.exit("error: --region cannot be empty")

    df: pl.DataFrame | None = None
    for region in regions:
        if region == ALL_REGION:
            print(ALL_REGION)
            continue
        if df is None:
            if not CLEAN_CSV.exists():
                print(f"clean.csv not found at {CLEAN_CSV} — run iccu-clean first.", file=sys.stderr)
                sys.exit(1)
            df = pl.read_csv(CLEAN_CSV)
        print(resolve(region, df).label)


if __name__ == "__main__":
    main()
