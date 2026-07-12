"""Step 1: Download province ZIP archives from IGM (fallback: dsantini CDN)."""

import sys

import httpx  # httpx.Client type annotation

from dbsn.common import (
    BUILDINGS_DIR,
    OSM_DIR,
    ZIPS_DIR,
    Province,
    http_client,
    parse_args,
    rel,
)


def _download_province(client: httpx.Client, p: Province, overwrite: bool) -> bool:
    ZIPS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = ZIPS_DIR / p["zip_name"]

    if not overwrite:
        osm_path = OSM_DIR / f"{p['code']}_{p['date']}.osm"
        osm_bz2_path = OSM_DIR / f"{p['code']}_{p['date']}.osm.bz2"
        fgb_path = BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb"
        if osm_path.exists() or osm_bz2_path.exists():
            print(f"  [skip ] {p['code']} {p['province']}: OSM already done (use --overwrite to reprocess)")
            return True
        if fgb_path.exists():
            print(f"  [skip ] {p['code']} {p['province']}: FGB already extracted (use --overwrite to reprocess)")
            return True
        if zip_path.exists():
            size = zip_path.stat().st_size // (1024 * 1024)
            print(f"  [skip ] {p['code']} {p['province']}: {p['zip_name']} ({size}MB) (use --overwrite to reprocess)")
            return True

    urls = [("primary", p["url"])]
    if p["fallback_url"] and p["fallback_url"] != p["url"]:
        urls.append(("fallback", p["fallback_url"]))

    for label, url in urls:
        print(f"  [down ] {p['code']} {p['province']} ({label}): {url}")
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=600) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with zip_path.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            mb_done = downloaded // (1024 * 1024)
                            mb_total = total // (1024 * 1024)
                            print(
                                f"\r    {pct}% ({mb_done}/{mb_total}MB)",
                                end="",
                                flush=True,
                            )
            size = zip_path.stat().st_size // (1024 * 1024)
            print(f"\r  [done ] {p['code']} {p['province']}: {rel(zip_path)} ({size}MB)")
            return True
        except Exception as exc:
            print(f"\r  [fail ] {p['code']} {p['province']} ({label}): {exc}", file=sys.stderr)
            if zip_path.exists():
                zip_path.unlink()

    print(f"  [error] {p['code']} {p['province']}: all URLs failed", file=sys.stderr)
    return False


def run(provinces: list[Province], overwrite: bool) -> None:
    print(f"=== Step 1: Download ({len(provinces)} provinces) ===")
    ok = failed = 0
    with http_client() as client:
        for p in provinces:
            if _download_province(client, p, overwrite):
                ok += 1
            else:
                failed += 1
    print(f"\nDone: {ok} ok, {failed} failed")
    if failed:
        sys.exit(1)


def main() -> None:
    args = parse_args("Step 1: download province ZIP archives")
    run(args.provinces, args.overwrite)


if __name__ == "__main__":
    main()
