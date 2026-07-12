"""Step 4: Validate generated OSM files using osmium check-refs."""

import sys
from pathlib import Path

from dbsn.common import OSM_DIR, Province, parse_args, rel
from utils.validate import validate_file


def _osm_files(p: Province) -> list[str]:
    return [
        str(OSM_DIR / f"{p['code']}_{p['date']}.{ext}")
        for ext in ("osm", "osm.bz2")
        if (OSM_DIR / f"{p['code']}_{p['date']}.{ext}").exists()
    ]


def _validate_province(p: Province, delete_invalid: bool) -> bool | None:
    paths = _osm_files(p)
    if not paths:
        print(f"  [skip    ] {p['code']} {p['province']}: OSM not found, run convert first")
        return None
    ok = True
    for path in paths:
        print(f"  [validate] {p['code']} {p['province']}: {rel(path)}")
        if not validate_file(path):
            if delete_invalid:
                Path(path).unlink(missing_ok=True)
                print(f"  [deleted ] {rel(path)}")
            ok = False
    return ok


def run(provinces: list[Province], delete_invalid: bool = False) -> None:
    print(f"=== Step 4: Validate ({len(provinces)} provinces) ===")
    ok = failed = missing = 0
    for p in provinces:
        result = _validate_province(p, delete_invalid)
        if result is True:
            ok += 1
        elif result is None:
            missing += 1
        else:
            failed += 1
    print(f"\nDone: {ok} ok, {missing} missing, {failed} failed")
    if failed:
        sys.exit(1)


def _extra_args(p) -> None:
    p.add_argument("--delete-invalid", action="store_true", help="Delete OSM files that fail validation")


def main() -> None:
    args = parse_args("Step 4: validate OSM files with osmium check-refs", overwrite=False, setup=_extra_args)
    run(args.provinces, args.delete_invalid)


if __name__ == "__main__":
    main()
