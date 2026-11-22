"""Run all pipeline steps: discover → neighbours → [download/extract/extend/convert/validate] in parallel."""

import sys

from dbsn import convert, discover, validate
from dbsn import neighbours as neighbours_mod
from dbsn.common import (
    SOURCES_JSON,
    add_area_arg,
    add_layers_arg,
    add_max_weight_arg,
    filter_provinces,
    parse_args,
    parse_layers,
    parse_overwrite,
    read_sources,
)
from dbsn.layers import get_layers
from dbsn.validate import ValidateTask
from utils.dag import run_dag


def _extra_args(p) -> None:
    convert._extra_args(p)
    validate._extra_args(p)
    p.add_argument("--neighbours", action="store_true", help="Force re-run of neighbours step")
    p.add_argument("--no-extend", action="store_true", help="Skip cross-boundary extension phase")
    p.add_argument(
        "--overpass-url", dest="overpass_url", metavar="URL",
        help=f"Overpass API endpoint for the neighbours step "
             f"(default: {neighbours_mod._OVERPASS_URL})",
    )
    add_area_arg(p)
    add_layers_arg(p, [ld.name for ld in get_layers()])
    add_max_weight_arg(p)


def main() -> None:
    args = parse_args(
        "Run all DBSN pipeline steps: discover → neighbours → download → extract → convert → validate",
        resolve_provinces=False,
        setup=_extra_args,
    )

    overwrite_steps = parse_overwrite(args.overwrite)
    try:
        selected_names = parse_layers(args.layers, [ld.name for ld in get_layers()])
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    # Step 0: Discover
    if "discover" in overwrite_steps or not SOURCES_JSON.exists():
        discover.run("discover" in overwrite_steps)

    sources = read_sources()
    provinces = filter_provinces(sources, args.province)
    if not provinces:
        sys.exit(f"No province matched '{args.province}'")

    # Step 1: Neighbours — run before DAG build so dependency edges are known
    all_have_neighbours = all("neighbours" in p for p in provinces)
    if "neighbours" in overwrite_steps or args.neighbours or not all_have_neighbours:
        print("=== Step 1: Neighbours ===")
        neighbours_mod.run(read_sources(), provinces,
                           overwrite="neighbours" in overwrite_steps or args.neighbours,
                           overpass_url=args.overpass_url)

    all_sources = read_sources()
    provinces = filter_provinces(all_sources, args.province)
    sources_by_code = {p["code"]: p for p in all_sources}

    # Steps 2-5: Build DAG — ValidateTask pulls the full dep tree via dependencies()
    tasks = [
        ValidateTask(
            prov,
            sources_by_code,
            delete_invalid=args.delete_invalid,
            overwrite_steps=overwrite_steps,
            fmt=args.format,
            compress=args.compress,
            extend=not args.no_extend,
            area=args.area,
            layer_names=selected_names,
        )
        for prov in provinces
    ]
    run_dag(tasks, args.max_weight, overwrite_steps)


if __name__ == "__main__":
    main()
