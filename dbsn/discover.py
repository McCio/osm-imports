"""Step 0: Discover latest DBSN download links from IGM, dsantini CDN, and Danysan1 TSV."""

import argparse
import csv
import json
import re
import sys

import httpx
from bs4 import BeautifulSoup

from dbsn.common import (
    DANYSAN1_TSV_URL,
    DATA_DIR,
    IGM_DOWNLOAD_URL,
    SOURCES_JSON,
    TSV_CACHE,
    fmt_size,
    http_client,
    rel,
)

# Province-level files: exactly 2 letters at start of filename segment, e.g. MI_dbsn_2025-07-28.zip
# Region-level files (e.g. Marche_dbsn_...) and 3-letter codes (SMR) are intentionally excluded.
_ZIP_RE = re.compile(r"(?:^|[/\\])([A-Z]{2})_dbsn_(\d{4}-\d{2}-\d{2})\.zip", re.IGNORECASE)
_IGM_BASE = "https://igmi.esercito.difesa.it"


def _fetch_tsv(overwrite: bool) -> dict[str, dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TSV_CACHE.exists() and not overwrite:
        print(f"  [tsv] using cached {rel(TSV_CACHE)}")
    else:
        try:
            with http_client() as c:
                resp = c.get(DANYSAN1_TSV_URL, follow_redirects=True, timeout=30)
                resp.raise_for_status()
                TSV_CACHE.write_bytes(resp.content)
            print(f"  [tsv] fetched {len(resp.content) // 1024}KB from GitHub")
        except httpx.HTTPError as exc:
            if not TSV_CACHE.exists():
                sys.exit(f"TSV fetch failed and no cache: {exc}")
            print(f"  [tsv] fetch failed ({exc}), using cache")

    result: dict[str, dict] = {}
    with TSV_CACHE.open(encoding="utf-8") as f:
        reader = csv.DictReader(
            f,
            delimiter="\t",
            fieldnames=[
                "region",
                "province",
                "file",
                "url_wmit",
                "url_igm",
                "date",
                "latest",
            ],
        )
        next(reader)
        for row in reader:
            if row["latest"].strip() != "yes":
                continue
            code = row["file"][:2].upper()
            result[code] = {
                "region": row["region"].strip(),
                "province": row["province"].strip(),
                "date": row["date"].strip(),
                "url_igm": row["url_igm"].strip(),
                "url_wmit": row["url_wmit"].strip(),
            }
    print(f"  [tsv] {len(result)} latest provinces")
    return result


def _scrape_igm(client: httpx.Client) -> dict[str, dict]:
    found: dict[str, dict] = {}
    try:
        resp = client.get(IGM_DOWNLOAD_URL, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            m = _ZIP_RE.search(a["href"])
            if not m:
                continue
            code, date_str = m.group(1).upper(), m.group(2)
            href = a["href"]
            url = href if href.startswith("http") else _IGM_BASE + href
            if code not in found or date_str > found[code]["date"]:
                found[code] = {"date": date_str, "url": url}
        print(f"  [igm] {len(found)} provinces found on IGM download page")
    except Exception as exc:
        print(f"  [igm] scrape failed ({exc}), using TSV IGM URLs", file=sys.stderr)
    return found


def _merge(tsv: dict[str, dict], igm: dict[str, dict]) -> dict[str, dict]:
    all_codes = sorted(set(tsv) | set(igm))
    sources: dict[str, dict] = {}

    for code in all_codes:
        t = tsv.get(code, {})
        ig = igm.get(code, {})

        tsv_date = t.get("date", "")
        igm_date = ig.get("date", "")
        best_date = max(tsv_date, igm_date)

        primary_url = ig["url"] if (ig and igm_date == best_date) else t.get("url_igm", "")
        url_wmit = t.get("url_wmit", "")
        fallback_url = url_wmit if url_wmit not in ("TODO", "") else primary_url

        status = "missing_in_tsv" if code not in tsv else "newer_available" if best_date > tsv_date else "ok"

        sources[code] = {
            "province": t.get("province", ""),
            "region": t.get("region", ""),
            "date": best_date,
            "url": primary_url,
            "fallback_url": fallback_url,
            "tsv_date": tsv_date,
            "status": status,
        }

    return sources


def _head_size(url: str, client: httpx.Client) -> int | None:
    try:
        r = client.head(url, follow_redirects=True, timeout=10)
        r.raise_for_status()
        size = int(r.headers.get("content-length", 0))
        return size if size > 0 else None
    except Exception:
        return None


def _fetch_sizes(sources: dict[str, dict], client: httpx.Client) -> None:
    mismatches: list[str] = []
    for code, entry in sources.items():
        primary = entry["url"]
        fallback = entry.get("fallback_url", "")
        unique_urls = list(dict.fromkeys(u for u in [primary, fallback] if u))

        sizes = {url: _head_size(url, client) for url in unique_urls}
        entry["zip_size"] = sizes.get(primary) or next((s for s in sizes.values() if s), None)

        if fallback and fallback != primary:
            ps, fs = sizes.get(primary), sizes.get(fallback)
            if ps and fs and ps != fs:
                ratio = max(ps, fs) / min(ps, fs)
                if ratio >= 2:
                    mismatches.append(
                        f"  [warn ] {code}: primary {fmt_size(ps)} vs fallback {fmt_size(fs)}"
                        + (" — consider swapping" if fs > ps else "")
                    )

    found = sum(1 for v in sources.values() if v["zip_size"])
    print(f"  [size] {found}/{len(sources)} provinces with known ZIP size")
    for msg in mismatches:
        print(msg)


def run(overwrite: bool) -> dict:
    print("=== Step 0: Discover ===")
    tsv_data = _fetch_tsv(overwrite)
    with http_client() as client:
        igm_data = _scrape_igm(client)
        sources = _merge(tsv_data, igm_data)
        _fetch_sizes(sources, client)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_JSON.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    newer = sum(1 for v in sources.values() if v["status"] == "newer_available")
    missing = sum(1 for v in sources.values() if v["status"] == "missing_in_tsv")
    print(f"\nTotal: {len(sources)} | Newer available: {newer} | Missing in TSV: {missing}")
    print(f"Written: {rel(SOURCES_JSON)}")
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 0: discover latest DBSN download links")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch TSV even if cached")
    args = parser.parse_args()
    run(args.overwrite)


if __name__ == "__main__":
    main()
