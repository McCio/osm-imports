"""Step 3: Convert FlatGeobuf layers to a single OSM XML (or GeoJSON) per province."""

import itertools
import sys
from pathlib import Path

import fiona

from dbsn.common import (
    BUILDINGS_DIR,
    OSM_DIR,
    Province,
    add_layers_arg,
    add_max_weight_arg,
    parse_args,
    parse_layers,
    parse_overwrite,
    read_sources,
    rel,
)
from dbsn.extract import ExtendTask, ExtractRawTask, fgb_path
from dbsn.layers import LayerDef, get_layers
from utils.dag import Task, run_dag
from utils.writers import translate_features, write_geojson, write_osm_items


def _build_override_maps(p: Province, sources_by_code: dict[str, Province]) -> dict[str, dict[str, dict]]:
    """Scan BUILDINGS_DIR for ext files applicable to province p; return {layer: {classid: geom}}."""
    maps: dict[str, dict[str, dict]] = {}
    for ext_path in BUILDINGS_DIR.glob("*.fgb"):
        parts = ext_path.stem.split("_", 4)
        # Ext stem: {C1}_{C2}_{C1date}_{C2date}_{layer} — split max 4 times; layer may contain underscores
        if len(parts) != 5:
            continue
        c1, c2, d1, d2, lname = parts
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
                    maps.setdefault(lname, {})[cid] = dict(feat["geometry"])
    return maps


def _layer_items(p: Province, layer: LayerDef, region: str, override_map: dict | None = None):
    """Yield translated (geom, tags) items from a single layer FGB."""
    fgb = fgb_path(p["code"], p["date"], layer.name)
    if not fgb.exists():
        return
    translate_fn = lambda attrs: layer.translate_fn(attrs, region)  # noqa: E731
    overrides = override_map if layer.supports_extension else None
    with fiona.open(str(fgb)) as src:
        yield from translate_features(src, translate_fn, overrides)


def _convert_province(
    p: Province,
    overwrite: bool,
    sources_by_code: dict[str, Province],
    fmt: str = "osm",
    compress: bool = False,
    layers: list[LayerDef] | None = None,
) -> bool | None:
    active = layers or get_layers()
    edifc_fgb = fgb_path(p["code"], p["date"], "edifc")
    if not edifc_fgb.exists():
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

    override_maps = _build_override_maps(p, sources_by_code)
    total_overrides = sum(len(v) for v in override_maps.values())
    if total_overrides:
        print(f"  [override] {p['code']} {p['province']}: {total_overrides} cross-boundary features from ext files")

    region = p.get("region") or ""
    layers_str = ", ".join(ld.name for ld in active)
    print(f"  [convert] {p['code']} {p['province']}: [{layers_str}] → {rel(out_path)}")

    try:
        if fmt == "geojson":
            all_tag_keys = sorted({k for ld in active for k in ld.tag_keys})
            schema = {"geometry": "Unknown", "properties": dict.fromkeys(all_tag_keys, "str")}
            total = 0
            # geojson: write each layer separately (fiona doesn't support chained append easily)
            with fiona.open(str(edifc_fgb)) as first_src:
                crs = first_src.crs
            with fiona.open(str(out_path), "w", driver="GeoJSON", schema=schema, crs=crs) as dst:
                schema_props = set(all_tag_keys)
                for layer in active:
                    for geom, tags in _layer_items(p, layer, region, override_maps.get(layer.name)):
                        dst.write({"type": "Feature", "geometry": geom,
                                   "properties": {**{k: None for k in schema_props}, **tags}})
                        total += 1
            count_written = total
        else:
            # Determine bounds from edifc FGB (covers the primary layer; good enough for bbox)
            bounds = None
            try:
                with fiona.open(str(edifc_fgb)) as src:
                    bounds = src.bounds
            except Exception as exc:
                print(f"  [warn   ] {p['code']} {p['province']}: bounds unavailable ({exc}), omitting bbox",
                      file=sys.stderr)

            items = itertools.chain.from_iterable(
                _layer_items(p, layer, region, override_maps.get(layer.name)) for layer in active
            )
            count_written = write_osm_items(items, out_path, bounds)

        size = out_path.stat().st_size // 1024
        print(f"  [done   ] {p['code']} {p['province']}: {rel(out_path)} ({count_written} features, {size}KB)")
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}", file=sys.stderr)
        if out_path.exists():
            out_path.unlink()
        return False


class ConvertTask(Task):
    run_in_process = True
    weight = 1.5

    def __init__(
        self,
        prov: Province,
        sources_by_code: dict[str, Province],
        overwrite_steps: frozenset[str] = frozenset(),
        fmt: str = "osm",
        compress: bool = True,
        extend: bool = True,
        layer_names: list[str] | None = None,
    ) -> None:
        self._prov = prov
        self._sources = sources_by_code
        self._overwrite_steps = overwrite_steps
        self._fmt = fmt
        self._compress = compress
        self._extend = extend
        self._layer_names = layer_names  # None = all; stored as names (picklable)

    @property
    def name(self) -> str:
        return f"convert:{self._prov['code']}"

    def log_cached(self) -> None:
        print(f"  [{self.label}] {self._prov['code']} {self._prov['province']}: cached")

    def _active_layers(self) -> list[LayerDef]:
        all_layers = get_layers()
        if self._layer_names is None:
            return all_layers
        return [ld for ld in all_layers if ld.name in self._layer_names]

    def dependencies(self) -> list[Task]:
        deps: list[Task] = [ExtractRawTask(self._prov, self._overwrite_steps, self._layer_names)]
        if self._extend:
            for nb_code in self._prov.get("neighbours", []):
                nb = self._sources.get(nb_code)
                if nb:
                    deps.append(ExtendTask(self._prov, nb, self._overwrite_steps))
        return deps

    def skip_if(self) -> bool:
        if "convert" in self._overwrite_steps:
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
        result = _convert_province(
            self._prov,
            overwrite="convert" in self._overwrite_steps,
            sources_by_code=self._sources,
            fmt=self._fmt,
            compress=self._compress,
            layers=self._active_layers(),
        )
        if result is False:
            raise RuntimeError(f"convert failed: {self._prov['code']} {self._prov['province']}")


def run(provinces: list[Province], overwrite: bool, fmt: str = "osm", compress: bool = False) -> None:
    print(f"=== Step 3: Convert ({len(provinces)} provinces, format={fmt}{', compressed' if compress else ''}) ===")
    sources_by_code = {s["code"]: s for s in read_sources()}
    ok = failed = skipped = 0
    for p in provinces:
        result = _convert_province(p, overwrite, sources_by_code, fmt, compress)
        if result is True:
            ok += 1
        elif result is None:
            skipped += 1
        else:
            failed += 1
    print(f"\nDone: {ok} ok, {skipped} skipped, {failed} failed")
    if failed:
        sys.exit(1)


def main() -> None:
    all_layers = get_layers()
    all_layer_names = [ld.name for ld in all_layers]

    def _setup(parser) -> None:
        parser.add_argument(
            "--format",
            choices=["osm", "geojson"],
            default="osm",
            metavar="osm|geojson",
            help="Output format (default: osm)",
        )
        parser.add_argument("--compress", action="store_true", help="Compress OSM output as .osm.bz2")
        add_layers_arg(parser, all_layer_names)
        add_max_weight_arg(parser)

    args = parse_args("Step 3: convert FlatGeobuf layers to OSM XML or GeoJSON", setup=_setup)
    if args.compress and args.format == "geojson":
        sys.exit("error: --compress only applies to --format osm")

    try:
        selected_names = parse_layers(args.layers, all_layer_names)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    active_layers = [ld for ld in all_layers if selected_names is None or ld.name in selected_names]

    overwrite_steps = parse_overwrite(args.overwrite)
    sources_by_code = {p["code"]: p for p in read_sources()}
    active_names = selected_names  # None = all layers
    tasks = [
        ConvertTask(prov, sources_by_code, overwrite_steps, args.format, args.compress, layer_names=active_names)
        for prov in args.provinces
    ]
    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
