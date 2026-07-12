"""Run all ICCU pipeline steps in sequence: download → clean → export → conflate."""

import sys

from iccu import clean, conflate, download, export
from iccu.common import parse_args, parse_regions


def _extra_args(p) -> None:
    export._extra_args(p)
    conflate._extra_args(p)


def main() -> None:
    args = parse_args(
        "Run all ICCU pipeline steps: download → clean → export → conflate",
        region=True,
        setup=_extra_args,
    )
    regions = parse_regions(args.region)
    if not regions:
        sys.exit("error: --region cannot be empty")

    download.run(args.overwrite)
    clean.run(args.overwrite)
    for region in regions:
        export.run(region, args.overwrite, args.format, args.compress)
        conflate.run(region, args.overwrite, args.osc, args.overpass_url, args.contact)


if __name__ == "__main__":
    main()
