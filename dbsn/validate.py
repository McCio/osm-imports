"""Step 4: Validate generated OSM files using osmium check-refs."""

import sys
from pathlib import Path

from dbsn.common import OSM_DIR, Province, add_max_weight_arg, parse_args, read_sources, rel
from dbsn.convert import ConvertTask
from utils.dag import Task, run_dag
from utils.osm_validate import validate_file


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


class ValidateTask(Task):
    weight = 2

    def __init__(
        self,
        prov: Province,
        sources_by_code: dict[str, Province],
        delete_invalid: bool = False,
        overwrite_steps: frozenset[str] = frozenset(),
        fmt: str = "osm",
        compress: bool = True,
        extend: bool = True,
    ) -> None:
        self._prov = prov
        self._sources = sources_by_code
        self._delete_invalid = delete_invalid
        self._overwrite_steps = overwrite_steps
        self._fmt = fmt
        self._compress = compress
        self._extend = extend

    @property
    def name(self) -> str:
        return f"validate:{self._prov['code']}"

    def log_cached(self) -> None:
        print(f"  [{self.label}] {self._prov['code']} {self._prov['province']}: cached")

    def dependencies(self) -> list[Task]:
        return [ConvertTask(self._prov, self._sources, self._overwrite_steps, self._fmt, self._compress, self._extend)]

    def skip_if(self) -> bool:
        return not bool(_osm_files(self._prov))

    def run(self) -> None:
        result = _validate_province(self._prov, self._delete_invalid)
        if result is False:
            raise RuntimeError(f"validate failed: {self._prov['code']} {self._prov['province']}")


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
    def _setup(parser) -> None:
        _extra_args(parser)
        add_max_weight_arg(parser)

    args = parse_args("Step 4: validate OSM files with osmium check-refs", overwrite=False, setup=_setup)
    sources_by_code = {p["code"]: p for p in read_sources()}
    tasks = [ValidateTask(prov, sources_by_code, delete_invalid=args.delete_invalid) for prov in args.provinces]
    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
