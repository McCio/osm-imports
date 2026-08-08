"""Step 2: Extract buildings layer from GDB to FlatGeobuf using fiona (bundled GDAL)."""

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
    EXCLUDE_META_IST,
    OSM_DIR,
    UNZIPPED_DIR,
    ZIPS_DIR,
    Province,
    add_max_weight_arg,
    http_client,
    parse_args,
    parse_overwrite,
    read_sources,
    rel,
)
from dbsn.download import DownloadTask
from utils.dag import Task, run_dag

_WGS84 = CRS.from_epsg(4326)


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
    """Extension phase: union cross-boundary building fragments and write ext files."""
    neighbour_codes: list[str] = p.get("neighbours", [])  # type: ignore[assignment]
    if not neighbour_codes:
        return

    neighbours = [sources_by_code[c] for c in neighbour_codes if c in sources_by_code]

    # Step 1: Ensure each neighbour's raw FGB exists (raw-only, no recursion)
    for nb in neighbours:
        nb_fgb = BUILDINGS_DIR / f"{nb['code']}_{nb['date']}.fgb"
        if not nb_fgb.exists():
            print(f"  [extend ] {p['code']} {p['province']}: extracting neighbour {nb['code']} {nb['province']} (raw)...")  # noqa: E501
            _extract_province(nb, overwrite=False, extend=False)
        if pre_extracted is not None:
            pre_extracted.add(nb["code"])

    # Step 2: Load province X's full classid → feature dict
    p_fgb = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"
    p_schema: dict | None = None
    p_features: dict[str, dict] = {}
    with fiona.open(str(p_fgb)) as src:
        # Use "Unknown" so shapely union results (Polygon/MultiPolygon) are accepted without coercion
        p_schema = {"geometry": "Unknown", "properties": src.schema["properties"]}
        for feat in src:
            cid = feat["properties"].get("classid")
            if cid:
                p_features[cid] = {"geometry": dict(feat["geometry"]), "properties": dict(feat["properties"])}

    # Step 3: Stream each neighbour's FGB, collect features whose classid ∈ p_features
    # neighbour_shared[classid][nb_code] = feature
    neighbour_shared: dict[str, dict[str, dict]] = {}
    for nb in neighbours:
        nb_fgb = BUILDINGS_DIR / f"{nb['code']}_{nb['date']}.fgb"
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

    # Step 4: For each direct neighbour, write the ext file for that pair
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


def _extract_province(
    p: Province,
    overwrite: bool,
    extend: bool = True,
    sources_by_code: dict[str, Province] | None = None,
    pre_extracted: set[str] | None = None,
) -> bool | None:
    out_fgb = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"

    def _run_extend() -> None:
        if extend and p.get("neighbours") and sources_by_code:
            try:
                _extend_province(p, overwrite, sources_by_code, pre_extracted)
            except Exception as exc:
                print(f"  [error  ] {p['code']} {p['province']}: extension failed ({exc}), raw FGB kept",
                      file=sys.stderr)

    if not overwrite:
        osm_path = OSM_DIR / f"{p['code']}_{p['date']}.osm"
        osm_bz2_path = OSM_DIR / f"{p['code']}_{p['date']}.osm.bz2"
        if osm_path.exists() or osm_bz2_path.exists():
            print(f"  [skip   ] {p['code']} {p['province']}: OSM already done (use --overwrite to reprocess)")
            return True
        if out_fgb.exists():
            size = out_fgb.stat().st_size // 1024
            print(f"  [skip   ] {p['code']} {p['province']}: {rel(out_fgb)} ({size}KB) (use --overwrite to reprocess)")
            _run_extend()
            return True
    elif pre_extracted and p["code"] in pre_extracted:
        # overwrite=True but already extracted as a neighbour this run — skip re-extraction
        _run_extend()
        return True

    zip_path = ZIPS_DIR / p["zip_name"]
    if not zip_path.exists():
        print(f"  [download] {p['code']} {p['province']}: ZIP missing, fetching...")
        with http_client() as client:
            if not _dl._download_province(client, p, overwrite=False):
                print(f"  [skip   ] {p['code']} {p['province']}: download failed, skipping")
                return None

    # Unzip — always clear to avoid reusing a partial dir from a previous crash
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

    print(f"  [extract] {p['code']} {p['province']}: {rel(gdb)} → {rel(out_fgb)}")
    try:
        with fiona.open(str(gdb), layer="edifc") as src:
            t = Transformer.from_crs(src.crs, _WGS84, always_xy=True)
            schema = {"geometry": src.schema["geometry"], "properties": src.schema["properties"]}
            BUILDINGS_DIR.mkdir(parents=True, exist_ok=True)
            if out_fgb.exists():
                out_fgb.unlink()
            written = 0
            with fiona.open(str(out_fgb), "w", driver="FlatGeobuf", schema=schema, crs=_WGS84) as dst:
                for feat in src:
                    if feat["properties"].get("meta_ist") in EXCLUDE_META_IST:
                        continue
                    dst.write(
                        {
                            "type": "Feature",
                            "geometry": _reproject(dict(feat["geometry"]), t),
                            "properties": dict(feat["properties"]),
                        }
                    )
                    written += 1

        size = out_fgb.stat().st_size // 1024
        print(f"  [done   ] {p['code']} {p['province']}: {rel(out_fgb)} ({written} features, {size}KB)")
        shutil.rmtree(unzip_dir)

        if sources_by_code is None:
            sources_by_code = {s["code"]: s for s in read_sources()}
        _run_extend()
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}", file=sys.stderr)
        if out_fgb.exists():
            out_fgb.unlink()
        shutil.rmtree(unzip_dir, ignore_errors=True)
        return False


def extend_pair(p1: Province, p2: Province, overwrite: bool = False) -> None:
    """Write the cross-boundary ext FGB for the province pair (p1, p2).

    Canonical file name uses alphabetically-sorted codes. Unions the building
    fragments from both provinces for each shared classid.
    """
    c1, c2 = (p1["code"], p2["code"]) if p1["code"] < p2["code"] else (p2["code"], p1["code"])
    if p1["code"] != c1:
        p1, p2 = p2, p1
    d1, d2 = p1["date"], p2["date"]
    ext_path = BUILDINGS_DIR / f"{c1}_{c2}_{d1}_{d2}.fgb"

    if ext_path.exists() and not overwrite:
        size = ext_path.stat().st_size // 1024
        print(f"  [skip   ] ext {c1}_{c2}: {rel(ext_path)} ({size}KB)")
        return

    p1_fgb = BUILDINGS_DIR / f"{c1}_{d1}.fgb"
    p2_fgb = BUILDINGS_DIR / f"{c2}_{d2}.fgb"
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

    def __init__(self, prov: Province, overwrite_steps: frozenset[str] = frozenset()) -> None:
        self._prov = prov
        self._overwrite_steps = overwrite_steps

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
        return (BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb").exists()

    def run(self) -> None:
        result = _extract_province(self._prov, overwrite="extract" in self._overwrite_steps, extend=False)
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
    def _setup(parser) -> None:
        parser.add_argument("--no-extend", action="store_true", help="Skip extension phase (no ext files)")
        add_max_weight_arg(parser)

    args = parse_args("Step 2: extract buildings layer from GDB to FlatGeobuf", setup=_setup)
    overwrite_steps = parse_overwrite(args.overwrite)
    sources_by_code = {p["code"]: p for p in read_sources()}

    tasks: list[Task] = []
    for prov in args.provinces:
        tasks.append(ExtractRawTask(prov, overwrite_steps))
        if not args.no_extend:
            for nb_code in prov.get("neighbours", []):
                nb = sources_by_code.get(nb_code)
                if nb:
                    tasks.append(ExtendTask(prov, nb, overwrite_steps))

    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
