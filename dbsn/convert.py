"""Step 3: Convert FlatGeobuf buildings to OSM XML or GeoJSON."""

import sys

import fiona

from dbsn.common import BUILDINGS_DIR, OSM_DIR, Province, add_max_weight_arg, parse_args, read_sources, rel
from dbsn.extract import ExtendTask, ExtractRawTask
from dbsn.translate import TAG_KEYS, make_translator
from utils.dag import Task, run_dag
from utils.writers import write_geojson, write_osm

_GEOJSON_SCHEMA = {"geometry": "Unknown", "properties": dict.fromkeys(TAG_KEYS, "str")}


def _build_override_map(p: Province, sources_by_code: dict[str, Province]) -> dict[str, dict]:
    """Scan BUILDINGS_DIR for ext files applicable to province p; return classid→geom map."""
    override_map: dict[str, dict] = {}
    for ext_path in BUILDINGS_DIR.glob("*.fgb"):
        parts = ext_path.stem.split("_")
        # Ext stem: {C1}_{C2}_{C1date}_{C2date} — exactly 4 parts, first two are 2-letter alpha codes
        if len(parts) != 4:
            continue
        c1, c2, d1, d2 = parts
        if not (len(c1) == 2 and c1.isalpha() and len(c2) == 2 and c2.isalpha()):
            continue
        if not (
            (c1 == p["code"] and d1 == p["date"] and sources_by_code.get(c2, {}).get("date") == d2)
            or (c2 == p["code"] and d2 == p["date"] and sources_by_code.get(c1, {}).get("date") == d1)
        ):
            continue
        with fiona.open(str(ext_path)) as src:
            for feat in src:
                cid = feat["properties"].get("classid")
                if cid:
                    override_map[cid] = dict(feat["geometry"])
    return override_map


def _convert_province(p: Province, overwrite: bool, fmt: str = "osm", compress: bool = False) -> bool | None:
    in_fgb = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"
    if not in_fgb.exists():
        print(f"  [skip   ] {p['code']} {p['province']}: FGB not found, run extract first")
        return None

    OSM_DIR.mkdir(parents=True, exist_ok=True)
    if fmt == "geojson":
        ext = "geojson"
    elif compress:
        ext = "osm.bz2"
    else:
        ext = "osm"
    out_path = OSM_DIR / f"{p['code']}_{p['date']}.{ext}"
    if out_path.exists() and not overwrite:
        size = out_path.stat().st_size // 1024
        print(f"  [skip   ] {p['code']} {p['province']}: {rel(out_path)} ({size}KB) (use --overwrite to reprocess)")
        return True

    if out_path.exists():
        out_path.unlink()

    sources_by_code = {s["code"]: s for s in read_sources()}
    override_map = _build_override_map(p, sources_by_code)
    if override_map:
        print(f"  [override] {p['code']} {p['province']}: {len(override_map)} cross-boundary buildings from ext files")

    print(f"  [convert] {p['code']} {p['province']}: {rel(in_fgb)} → {rel(out_path)}")
    translator = make_translator(p)
    try:
        with fiona.open(str(in_fgb)) as src:
            if fmt == "geojson":
                count_written = write_geojson(
                    src, out_path, translator, _GEOJSON_SCHEMA, overrides=override_map or None
                )
            else:
                try:
                    bounds = src.bounds
                except Exception as exc:
                    print(
                        f"  [warn   ] {p['code']} {p['province']}: bounds unavailable ({exc}), omitting bbox",
                        file=sys.stderr,
                    )
                    bounds = None
                count_written = write_osm(src, out_path, translator, bounds, overrides=override_map or None)
        size = out_path.stat().st_size // 1024
        print(f"  [done   ] {p['code']} {p['province']}: {rel(out_path)} ({count_written} features, {size}KB)")
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}", file=sys.stderr)
        if out_path.exists():
            out_path.unlink()
        return False


class ConvertTask(Task):
    def __init__(
        self,
        prov: Province,
        sources_by_code: dict[str, Province],
        overwrite: bool = False,
        fmt: str = "osm",
        compress: bool = True,
        extend: bool = True,
    ) -> None:
        self._prov = prov
        self._sources = sources_by_code
        self._overwrite = overwrite
        self._fmt = fmt
        self._compress = compress
        self._extend = extend

    @property
    def name(self) -> str:
        return f"convert:{self._prov['code']}"

    def dependencies(self) -> list[Task]:
        deps: list[Task] = [ExtractRawTask(self._prov, self._overwrite)]
        if self._extend:
            for nb_code in self._prov.get("neighbours", []):
                nb = self._sources.get(nb_code)
                if nb:
                    deps.append(ExtendTask(self._prov, nb, self._overwrite))
        return deps

    def skip_if(self) -> bool:
        if self._overwrite:
            return False
        p = self._prov
        if self._fmt == "geojson":
            ext = "geojson"
        elif self._compress:
            ext = "osm.bz2"
        else:
            ext = "osm"
        return (OSM_DIR / f"{p['code']}_{p['date']}.{ext}").exists()

    def run(self) -> None:
        result = _convert_province(self._prov, overwrite=self._overwrite, fmt=self._fmt, compress=self._compress)
        if result is False:
            raise RuntimeError(f"convert failed: {self._prov['code']} {self._prov['province']}")


def run(provinces: list[Province], overwrite: bool, fmt: str = "osm", compress: bool = False) -> None:
    print(f"=== Step 3: Convert ({len(provinces)} provinces, format={fmt}{', compressed' if compress else ''}) ===")
    ok = failed = skipped = 0
    for p in provinces:
        result = _convert_province(p, overwrite, fmt, compress)
        if result is True:
            ok += 1
        elif result is None:
            skipped += 1
        else:
            failed += 1
    print(f"\nDone: {ok} ok, {skipped} skipped, {failed} failed")
    if failed:
        sys.exit(1)


def _extra_args(p) -> None:
    p.add_argument(
        "--format",
        choices=["osm", "geojson"],
        default="osm",
        metavar="osm|geojson",
        help="Output format (default: osm)",
    )
    p.add_argument("--compress", action="store_true", help="Compress OSM output as .osm.bz2")


def main() -> None:
    def _setup(parser) -> None:
        _extra_args(parser)
        add_max_weight_arg(parser)

    args = parse_args("Step 3: convert FlatGeobuf to OSM XML or GeoJSON", setup=_setup)
    if args.compress and args.format == "geojson":
        sys.exit("error: --compress only applies to --format osm")

    sources_by_code = {p["code"]: p for p in read_sources()}
    tasks = [ConvertTask(prov, sources_by_code, args.overwrite, args.format, args.compress) for prov in args.provinces]
    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
