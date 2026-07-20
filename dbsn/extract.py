"""Step 2: Extract buildings layer from GDB to FlatGeobuf using fiona (bundled GDAL)."""

import shutil
import sys
import zipfile

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
    http_client,
    parse_args,
    read_sources,
    rel,
)

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
            print(f"  [extend ] {p['code']} {p['province']}: extracting neighbour {nb['code']} {nb['province']} (raw)...")
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
        shared_with_nb = {cid: nb_map for cid, nb_map in neighbour_shared.items() if nb["code"] in nb_map}

        c1, c2 = sorted([p["code"], nb["code"]])
        d1 = sources_by_code[c1]["date"]
        d2 = sources_by_code[c2]["date"]
        ext_path = BUILDINGS_DIR / f"{c1}_{c2}_{d1}_{d2}.fgb"

        if ext_path.exists() and not overwrite:
            size = ext_path.stat().st_size // 1024
            print(f"  [skip   ] ext {c1}_{c2}: {rel(ext_path)} ({size}KB)")
            continue

        # Delete stale ext files for this pair (any date combination)
        for stale in BUILDINGS_DIR.glob(f"{c1}_{c2}_*.fgb"):
            stale.unlink()

        print(f"  [extend ] {p['code']} ↔ {nb['code']}: {len(shared_with_nb)} shared buildings → {rel(ext_path)}")
        try:
            with fiona.open(str(ext_path), "w", driver="FlatGeobuf", schema=p_schema, crs=_WGS84) as dst:
                for cid, nb_map in shared_with_nb.items():
                    # nb_map = neighbour_shared[cid]: all neighbours that carry this classid
                    all_feats = [p_features[cid], *nb_map.values()]

                    best = max(all_feats, key=lambda f: f["properties"].get("shape_Area") or 0.0)
                    geoms = [shapely_shape(f["geometry"]) for f in all_feats if f["geometry"]]
                    merged = unary_union(geoms)

                    dst.write(
                        {
                            "type": "Feature",
                            "geometry": merged.__geo_interface__,
                            "properties": best["properties"],
                        }
                    )
        except Exception:
            if ext_path.exists():
                ext_path.unlink()
            raise


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

    args = parse_args("Step 2: extract buildings layer from GDB to FlatGeobuf", setup=_setup)
    run(args.provinces, args.overwrite, extend=not args.no_extend)


if __name__ == "__main__":
    main()
