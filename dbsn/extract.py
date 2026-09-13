"""Step 2: Extract GDB layers to FlatGeobuf using fiona (bundled GDAL)."""

import shutil
import sys
import zipfile
from pathlib import Path

import fiona
from fiona.crs import CRS
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union

from dbsn import download as _dl
from dbsn.common import (
    BUILDINGS_DIR,
    OSM_DIR,
    UNZIPPED_DIR,
    ZIPS_DIR,
    Province,
    add_layers_arg,
    add_max_weight_arg,
    http_client,
    parse_args,
    parse_layers,
    parse_overwrite,
    read_sources,
    rel,
)
from dbsn.download import DownloadTask
from dbsn.layers import LayerSpec, get_layer_specs
from utils.dag import Task, run_dag

_WGS84 = CRS.from_epsg(4326)


def fgb_path(code: str, date: str, layer: str) -> Path:
    return BUILDINGS_DIR / f"{code}_{date}_{layer}.fgb"


def _reproject(geom: dict, t: Transformer) -> dict:
    def xf_ring(ring):
        xs, ys = t.transform([c[0] for c in ring], [c[1] for c in ring])
        if len(ring[0]) > 2:
            return [[x, y, c[2]] for (x, y), c in zip(zip(xs, ys, strict=True), ring, strict=True)]
        return [[x, y] for x, y in zip(xs, ys, strict=True)]

    gtype = geom["type"]
    if gtype == "Polygon":
        return {"type": "Polygon", "coordinates": [xf_ring(r) for r in geom["coordinates"]]}
    if gtype == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[xf_ring(r) for r in poly] for poly in geom["coordinates"]]}
    return geom


def _write_ext_fgb(
    ext_path: Path,
    schema: dict,
    p_features: dict[str, dict],
    shared_nb: dict[str, list[dict]],
) -> None:
    """Write cross-boundary ext FGB. shared_nb: classid → neighbour feature(s) to union with p's."""
    c1, c2 = ext_path.stem.split("_")[:2]
    for stale in BUILDINGS_DIR.glob(f"{c1}_{c2}_*.fgb"):
        stale.unlink()
    print(f"  [extend ] {c1} ↔ {c2}: {len(shared_nb)} shared buildings → {rel(ext_path)}")
    try:
        with fiona.open(str(ext_path), "w", driver="FlatGeobuf", schema=schema, crs=_WGS84) as dst:
            for cid, nb_feats in shared_nb.items():
                all_feats = [p_features[cid], *nb_feats]
                best = max(all_feats, key=lambda f: f["properties"].get("shape_Area") or 0.0)
                geoms = [shapely_shape(f["geometry"]) for f in all_feats if f["geometry"]]
                merged = unary_union(geoms)
                dst.write({"type": "Feature", "geometry": merged.__geo_interface__, "properties": best["properties"]})
    except Exception:
        if ext_path.exists():
            ext_path.unlink()
        raise


def _extend_province(
    p: Province,
    overwrite: bool,
    sources_by_code: dict[str, Province],
    pre_extracted: set[str] | None = None,
) -> None:
    """Extension phase: union cross-boundary building fragments and write ext files (edifc only)."""
    neighbour_codes: list[str] = p.get("neighbours", [])  # type: ignore[assignment]
    if not neighbour_codes:
        return

    neighbours = [sources_by_code[c] for c in neighbour_codes if c in sources_by_code]

    for nb in neighbours:
        nb_fgb = fgb_path(nb["code"], nb["date"], "edifc")
        if not nb_fgb.exists():
            print(f"  [extend ] {p['code']} {p['province']}: extracting neighbour {nb['code']} {nb['province']} (raw)...")
            _extract_province(nb, overwrite=False, extend=False)
        if pre_extracted is not None:
            pre_extracted.add(nb["code"])

    p_fgb = fgb_path(p["code"], p["date"], "edifc")
    p_schema: dict | None = None
    p_features: dict[str, dict] = {}
    with fiona.open(str(p_fgb)) as src:
        p_schema = {"geometry": "Unknown", "properties": src.schema["properties"]}
        for feat in src:
            cid = feat["properties"].get("classid")
            if cid:
                p_features[cid] = {"geometry": dict(feat["geometry"]), "properties": dict(feat["properties"])}

    neighbour_shared: dict[str, dict[str, dict]] = {}
    for nb in neighbours:
        nb_fgb = fgb_path(nb["code"], nb["date"], "edifc")
        if not nb_fgb.exists():
            print(f"  [warn   ] {p['code']}: neighbour {nb['code']} FGB missing, skipped", file=sys.stderr)
            continue
        with fiona.open(str(nb_fgb)) as src:
            for feat in src:
                cid = feat["properties"].get("classid")
                if cid and cid in p_features:
                    neighbour_shared.setdefault(cid, {})[nb["code"]] = {
                        "geometry": dict(feat["geometry"]),
                        "properties": dict(feat["properties"]),
                    }

    for nb in neighbours:
        c1, c2 = sorted([p["code"], nb["code"]])
        d1 = sources_by_code[c1]["date"]
        d2 = sources_by_code[c2]["date"]
        ext_path = BUILDINGS_DIR / f"{c1}_{c2}_{d1}_{d2}.fgb"

        if ext_path.exists() and not overwrite:
            size = ext_path.stat().st_size // 1024
            print(f"  [skip   ] ext {c1}_{c2}: {rel(ext_path)} ({size}KB)")
            continue

        shared_with_nb: dict[str, list[dict]] = {
            cid: list(nb_map.values())
            for cid, nb_map in neighbour_shared.items()
            if nb["code"] in nb_map
        }
        _write_ext_fgb(ext_path, p_schema, p_features, shared_with_nb)


def _extract_layer(gdb: Path, spec: LayerSpec, out_fgb: Path, t: Transformer) -> int:
    """Extract one GDB layer to FGB, returning count of written features."""
    with fiona.open(str(gdb), layer=spec.name) as src:
        schema = {"geometry": src.schema["geometry"], "properties": src.schema["properties"]}
        BUILDINGS_DIR.mkdir(parents=True, exist_ok=True)
        if out_fgb.exists():
            out_fgb.unlink()
        written = 0
        with fiona.open(str(out_fgb), "w", driver="FlatGeobuf", schema=schema, crs=_WGS84) as dst:
            for feat in src:
                props = feat["properties"]
                if not spec.passes_filter(props):
                    continue
                dst.write({
                    "type": "Feature",
                    "geometry": _reproject(dict(feat["geometry"]), t),
                    "properties": dict(props),
                })
                written += 1
    return written


def _extract_province(
    p: Province,
    overwrite: bool,
    extend: bool = True,
    sources_by_code: dict[str, Province] | None = None,
    pre_extracted: set[str] | None = None,
    layers: list[LayerSpec] | None = None,
) -> bool | None:
    active = layers or get_layer_specs()
    out_fgbs = [fgb_path(p["code"], p["date"], s.name) for s in active]

    def _run_extend() -> None:
        if extend and p.get("neighbours") and sources_by_code:
            try:
                _extend_province(p, overwrite, sources_by_code, pre_extracted)
            except Exception as exc:
                print(f"  [error  ] {p['code']} {p['province']}: extension failed ({exc}), raw FGB kept",
                      file=sys.stderr)

    if not overwrite:
        osm_path = OSM_DIR / f"{p['code']}_{p['date']}.osm"
        osm_bz2 = OSM_DIR / f"{p['code']}_{p['date']}.osm.bz2"
        if osm_path.exists() or osm_bz2.exists():
            print(f"  [skip   ] {p['code']} {p['province']}: OSM already done (use --overwrite to reprocess)")
            return True
        if all(f.exists() for f in out_fgbs):
            size = out_fgbs[0].stat().st_size // 1024
            print(f"  [skip   ] {p['code']} {p['province']}: FGBs present ({size}KB) (use --overwrite to reprocess)")
            _run_extend()
            return True
    elif pre_extracted and p["code"] in pre_extracted:
        _run_extend()
        return True

    zip_path = ZIPS_DIR / p["zip_name"]
    if not zip_path.exists():
        print(f"  [download] {p['code']} {p['province']}: ZIP missing, fetching...")
        with http_client() as client:
            if not _dl._download_province(client, p, overwrite=False):
                print(f"  [skip   ] {p['code']} {p['province']}: download failed, skipping")
                return None

    UNZIPPED_DIR.mkdir(parents=True, exist_ok=True)
    unzip_dir = UNZIPPED_DIR / p["zip_name"].removesuffix(".zip")
    if unzip_dir.exists():
        shutil.rmtree(unzip_dir)
    print(f"  [unzip  ] {p['code']} {p['province']}: {rel(zip_path)}...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(unzip_dir)

    gdbs = sorted(unzip_dir.rglob("*.gdb"))
    if not gdbs:
        print(f"  [error  ] {p['code']} {p['province']}: no .gdb found in {rel(unzip_dir)}", file=sys.stderr)
        shutil.rmtree(unzip_dir)
        return False
    gdb = gdbs[0]

    available_gdb_layers = set(fiona.listlayers(str(gdb)))
    present = [s for s in active if s.name in available_gdb_layers]
    absent = [s.name for s in active if s.name not in available_gdb_layers]
    if absent:
        print(f"  [warn   ] {p['code']} {p['province']}: layers not in GDB, skipped: {', '.join(absent)}")

    layers_str = ", ".join(s.name for s in present)
    print(f"  [extract] {p['code']} {p['province']}: {rel(gdb)} → [{layers_str}]")
    try:
        with fiona.open(str(gdb), layer="edifc") as _first:
            t = Transformer.from_crs(_first.crs, _WGS84, always_xy=True)

        for spec in present:
            out_fgb = fgb_path(p["code"], p["date"], spec.name)
            written = _extract_layer(gdb, spec, out_fgb, t)
            size = out_fgb.stat().st_size // 1024
            print(f"  [done   ] {p['code']} {p['province']}: {rel(out_fgb)} ({written} features, {size}KB)")

        shutil.rmtree(unzip_dir)

        if sources_by_code is None:
            sources_by_code = {s["code"]: s for s in read_sources()}
        _run_extend()
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}", file=sys.stderr)
        for s in present:
            f = fgb_path(p["code"], p["date"], s.name)
            if f.exists():
                f.unlink()
        shutil.rmtree(unzip_dir, ignore_errors=True)
        return False


def extend_pair(p1: Province, p2: Province, overwrite: bool = False) -> None:
    """Write cross-boundary ext FGB for the province pair (edifc only)."""
    c1, c2 = (p1["code"], p2["code"]) if p1["code"] < p2["code"] else (p2["code"], p1["code"])
    if p1["code"] != c1:
        p1, p2 = p2, p1
    d1, d2 = p1["date"], p2["date"]
    ext_path = BUILDINGS_DIR / f"{c1}_{c2}_{d1}_{d2}.fgb"

    if ext_path.exists() and not overwrite:
        size = ext_path.stat().st_size // 1024
        print(f"  [skip   ] ext {c1}_{c2}: {rel(ext_path)} ({size}KB)")
        return

    p1_fgb = fgb_path(c1, d1, "edifc")
    p2_fgb = fgb_path(c2, d2, "edifc")
    if not p1_fgb.exists() or not p2_fgb.exists():
        missing = [str(f) for f in (p1_fgb, p2_fgb) if not f.exists()]
        print(f"  [warn   ] ext {c1}_{c2}: missing FGB(s) {missing}, skipping", file=sys.stderr)
        return

    p1_features: dict[str, dict] = {}
    p1_schema: dict | None = None
    with fiona.open(str(p1_fgb)) as src:
        p1_schema = {"geometry": "Unknown", "properties": src.schema["properties"]}
        for feat in src:
            cid = feat["properties"].get("classid")
            if cid:
                p1_features[cid] = {"geometry": dict(feat["geometry"]), "properties": dict(feat["properties"])}

    shared_nb: dict[str, list[dict]] = {}
    with fiona.open(str(p2_fgb)) as src:
        for feat in src:
            cid = feat["properties"].get("classid")
            if cid and cid in p1_features:
                shared_nb[cid] = [{"geometry": dict(feat["geometry"]), "properties": dict(feat["properties"])}]

    if not shared_nb:
        print(f"  [extend ] {c1} ↔ {c2}: 0 shared buildings, skipping")
        return

    _write_ext_fgb(ext_path, p1_schema, p1_features, shared_nb)


class ExtractRawTask(Task):
    run_in_process = True

    def __init__(
        self,
        prov: Province,
        overwrite_steps: frozenset[str] = frozenset(),
        layer_names: list[str] | None = None,
    ) -> None:
        self._prov = prov
        self._overwrite_steps = overwrite_steps
        self._layer_names = layer_names  # None = all; stored as names (picklable)

    def _active(self) -> list[LayerSpec]:
        specs = get_layer_specs()
        if self._layer_names is None:
            return specs
        return [s for s in specs if s.name in self._layer_names]

    @property
    def name(self) -> str:
        return f"extract-raw:{self._prov['code']}"

    @property
    def label(self) -> str:
        return "extract"

    def log_cached(self) -> None:
        print(f"  [{self.label}] {self._prov['code']} {self._prov['province']}: cached")

    def dependencies(self) -> list[Task]:
        return [DownloadTask(self._prov, self._overwrite_steps)]

    def skip_if(self) -> bool:
        if "extract" in self._overwrite_steps:
            return False
        p = self._prov
        # Use edifc FGB as the sentinel — always present if extraction completed
        return fgb_path(p["code"], p["date"], "edifc").exists()

    def run(self) -> None:
        result = _extract_province(
            self._prov,
            overwrite="extract" in self._overwrite_steps,
            extend=False,
            layers=self._active(),
        )
        if result is False:
            raise RuntimeError(f"extract failed: {self._prov['code']} {self._prov['province']}")


class ExtendTask(Task):
    run_in_process = True

    def __init__(self, p1: Province, p2: Province, overwrite_steps: frozenset[str] = frozenset()) -> None:
        if p1["code"] > p2["code"]:
            p1, p2 = p2, p1
        self._p1 = p1
        self._p2 = p2
        self._overwrite_steps = overwrite_steps

    @property
    def name(self) -> str:
        return f"extend:{self._p1['code']}:{self._p2['code']}"

    def log_cached(self) -> None:
        print(f"  [{self.label}] {self._p1['code']}↔{self._p2['code']}: cached")

    def dependencies(self) -> list[Task]:
        return [
            ExtractRawTask(self._p1, self._overwrite_steps),
            ExtractRawTask(self._p2, self._overwrite_steps),
        ]

    def skip_if(self) -> bool:
        if "extend" in self._overwrite_steps:
            return False
        c1, c2 = self._p1["code"], self._p2["code"]
        d1, d2 = self._p1["date"], self._p2["date"]
        return (BUILDINGS_DIR / f"{c1}_{c2}_{d1}_{d2}.fgb").exists()

    def run(self) -> None:
        extend_pair(self._p1, self._p2, overwrite="extend" in self._overwrite_steps)


def run(provinces: list[Province], overwrite: bool, extend: bool = True) -> None:
    print(f"=== Step 2: Extract ({len(provinces)} provinces) ===")
    sources_by_code = {s["code"]: s for s in read_sources()} if extend else {}
    pre_extracted: set[str] = set()
    ok = failed = skipped = 0
    for p in provinces:
        result = _extract_province(
            p, overwrite, extend=extend, sources_by_code=sources_by_code, pre_extracted=pre_extracted
        )
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
    all_layer_names = [s.name for s in get_layer_specs()]

    def _setup(parser) -> None:
        parser.add_argument("--no-extend", action="store_true", help="Skip extension phase (no ext files)")
        add_layers_arg(parser, all_layer_names)
        add_max_weight_arg(parser)

    args = parse_args("Step 2: extract GDB layers to FlatGeobuf", setup=_setup)
    overwrite_steps = parse_overwrite(args.overwrite)
    sources_by_code = {p["code"]: p for p in read_sources()}

    try:
        selected_names = parse_layers(args.layers, all_layer_names)
    except ValueError as exc:
        import sys as _sys; _sys.exit(f"error: {exc}")
    active_specs = [s for s in get_layer_specs() if selected_names is None or s.name in selected_names]

    active_names = [s.name for s in active_specs]

    tasks: list[Task] = []
    for prov in args.provinces:
        tasks.append(ExtractRawTask(prov, overwrite_steps, layer_names=active_names))
        if not args.no_extend:
            for nb_code in prov.get("neighbours", []):
                nb = sources_by_code.get(nb_code)
                if nb:
                    tasks.append(ExtendTask(prov, nb, overwrite_steps))

    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
