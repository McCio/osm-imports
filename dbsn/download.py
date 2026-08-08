"""Step 1: Download province ZIP archives (newer date first; wmit before IGM within same date)."""

import sys

import httpx  # httpx.Client type annotation

from dbsn.common import (
    BUILDINGS_DIR,
    DATE_RE,
    OSM_DIR,
    ZIPS_DIR,
    Province,
    add_max_weight_arg,
    http_client,
    parse_args,
    parse_overwrite,
    read_sources,
    rel,
)
from utils.dag import Task, run_dag


def _date_key(url: str) -> int:
    m = DATE_RE.search(url)
    return int(m.group().replace("-", "")) if m else 0


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

    candidates = [("IGM", p["igm_url"])]
    if p["wmit_url"] and p["wmit_url"] != p["igm_url"]:
        candidates.append(("wmit", p["wmit_url"]))
    urls = sorted(candidates, key=lambda item: (-_date_key(item[1]), 0 if item[0] == "wmit" else 1))

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


class DownloadTask(Task):
    def __init__(self, prov: Province, overwrite_steps: frozenset[str] = frozenset()) -> None:
        self._prov = prov
        self._overwrite_steps = overwrite_steps

    @property
    def name(self) -> str:
        return f"download:{self._prov['code']}"

    def skip_if(self) -> bool:
        if "download" in self._overwrite_steps:
            return False
        p = self._prov
        return (
            (ZIPS_DIR / p["zip_name"]).exists()
            or (BUILDINGS_DIR / f"{p['code']}_{p['date']}.fgb").exists()
            or (OSM_DIR / f"{p['code']}_{p['date']}.osm").exists()
            or (OSM_DIR / f"{p['code']}_{p['date']}.osm.bz2").exists()
        )

    def run(self) -> None:
        with http_client() as client:
            ok = _download_province(client, self._prov, "download" in self._overwrite_steps)
        if not ok:
            raise RuntimeError(f"download failed: {self._prov['code']} {self._prov['province']}")


def run(provinces: list[Province], overwrite: bool, download_neighbours: bool = True) -> None:
    print(f"=== Step 1: Download ({len(provinces)} provinces) ===")
    ok = failed = 0
    with http_client() as client:
        for p in provinces:
            if _download_province(client, p, overwrite):
                ok += 1
            else:
                failed += 1

        if download_neighbours:
            sources_by_code = {s["code"]: s for s in read_sources()}
            batch_codes = {p["code"] for p in provinces}
            seen: set[str] = set()
            nb_provinces: list[Province] = []
            for p in provinces:
                for nb_code in p.get("neighbours", []):
                    if nb_code not in batch_codes and nb_code not in seen and nb_code in sources_by_code:
                        seen.add(nb_code)
                        nb_provinces.append(sources_by_code[nb_code])
            if nb_provinces:
                print(f"  [neighbours] pre-fetching {len(nb_provinces)} neighbour ZIP(s)...")
                for nb in nb_provinces:
                    if _download_province(client, nb, overwrite=False):
                        ok += 1
                    else:
                        failed += 1

    print(f"\nDone: {ok} ok, {failed} failed")
    if failed:
        sys.exit(1)


def main() -> None:
    def _setup(parser) -> None:
        parser.add_argument("--no-neighbours", action="store_true", help="Skip pre-fetching neighbour ZIPs")
        add_max_weight_arg(parser)

    args = parse_args("Step 1: download province ZIP archives", setup=_setup)
    overwrite_steps = parse_overwrite(args.overwrite)
    sources_by_code = {p["code"]: p for p in read_sources()}

    tasks: list[DownloadTask] = []
    for prov in args.provinces:
        tasks.append(DownloadTask(prov, overwrite_steps))
        if not args.no_neighbours:
            for nb_code in prov.get("neighbours", []):
                nb = sources_by_code.get(nb_code)
                if nb:
                    tasks.append(DownloadTask(nb))

    run_dag(tasks, args.max_weight)


if __name__ == "__main__":
    main()
