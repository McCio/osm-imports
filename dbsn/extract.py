"""Step 2: Extract buildings layer from GDB to FlatGeobuf using fiona (bundled GDAL)."""

import shutil
import sys
import zipfile

import fiona
from fiona.crs import CRS
from pyproj import Transformer

from dbsn.common import (
    BUILDINGS_DIR,
    EXCLUDE_META_IST,
    OSM_DIR,
    UNZIPPED_DIR,
    ZIPS_DIR,
    Province,
    parse_args,
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


def _extract_province(p: Province, overwrite: bool) -> bool | None:
    out_fgb = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"
    if not overwrite:
        osm_path = OSM_DIR / f"{p['code']}_{p['date']}.osm"
        osm_bz2_path = OSM_DIR / f"{p['code']}_{p['date']}.osm.bz2"
        if osm_path.exists() or osm_bz2_path.exists():
            print(f"  [skip   ] {p['code']} {p['province']}: OSM already done")
            return True
        if out_fgb.exists():
            size = out_fgb.stat().st_size // 1024
            print(f"  [skip   ] {p['code']} {p['province']}: {rel(out_fgb)} ({size}KB)")
            return True

    zip_path = ZIPS_DIR / p["zip_name"]
    if not zip_path.exists():
        print(f"  [skip   ] {p['code']} {p['province']}: ZIP not found, run download first")
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
        print(f"  [error  ] {p['code']} {p['province']}: no .gdb found in {rel(unzip_dir)}")
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
        return True

    except Exception as exc:
        print(f"  [error  ] {p['code']} {p['province']}: {exc}")
        if out_fgb.exists():
            out_fgb.unlink()
        shutil.rmtree(unzip_dir, ignore_errors=True)
        return False


def run(provinces: list[Province], overwrite: bool) -> None:
    print(f"=== Step 2: Extract ({len(provinces)} provinces) ===")
    ok = failed = skipped = 0
    for p in provinces:
        result = _extract_province(p, overwrite)
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
    args = parse_args("Step 2: extract buildings layer from GDB to FlatGeobuf")
    run(args.provinces, args.overwrite)


if __name__ == "__main__":
    main()
