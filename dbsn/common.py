"""Shared paths, types, and helpers for the DBSN pipeline."""

import argparse
import csv
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict

import httpx

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "dbsn"
TSV_CACHE = DATA_DIR / "dbsn.tsv"
SOURCES_JSON = DATA_DIR / "sources.json"
ZIPS_DIR = DATA_DIR / "zips"
UNZIPPED_DIR = DATA_DIR / "unzipped"
BUILDINGS_DIR = DATA_DIR / "buildings"
OSM_DIR = DATA_DIR / "osm"

EXCLUDE_META_IST: frozenset[str] = frozenset(("03", "21", "23"))

IGM_DOWNLOAD_URL = (
    "https://igmi.esercito.difesa.it/servizi/database-di-sintesi-nazionale/database-di-sintesi-nazionale-download/"
)
DANYSAN1_TSV_URL = "https://raw.githubusercontent.com/Danysan1/dbsn-import/main/dbsn.tsv"

USER_AGENT = (
    "dbsn-pipeline/0.1 (OSM DBSN buildings import; https://wiki.openstreetmap.org/wiki/Import/Catalogue/DBSN/Buildings)"
)


def rel(path) -> str:
    try:
        return str(Path(path).relative_to(DATA_DIR))
    except ValueError:
        return Path(path).name


def fmt_size(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    return f"{n // 1024} KB"


def http_client(**kwargs) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, **kwargs)


class Province(TypedDict):
    region: str
    province: str
    code: str
    zip_name: str
    igm_url: str
    wmit_url: str
    date: str
    tsv_date: str
    status: str
    zip_size: int | None
    neighbours: NotRequired[list[str]]


def max_date() -> str:
    return max(p["date"] for p in read_sources())


def read_sources() -> list[Province]:
    if SOURCES_JSON.exists():
        data = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
        result = []
        for code, v in data.items():
            p: Province = Province(
                code=code,
                zip_name=f"{code}_{v['date']}.zip",
                province=v.get("province", ""),
                region=v.get("region", ""),
                date=v["date"],
                igm_url=v["igm_url"],
                wmit_url=v.get("wmit_url", v["igm_url"]),
                tsv_date=v.get("tsv_date", v["date"]),
                status=v.get("status", "ok"),
                zip_size=v.get("zip_size"),
            )
            if "neighbours" in v:
                p["neighbours"] = v["neighbours"]
            result.append(p)
        return result

    if TSV_CACHE.exists():
        return _parse_tsv(TSV_CACHE)
    with http_client() as client:
        resp = client.get(DANYSAN1_TSV_URL)
        resp.raise_for_status()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TSV_CACHE.write_text(resp.text, encoding="utf-8")
    return _parse_tsv(TSV_CACHE)


def _parse_tsv(path: Path) -> list[Province]:
    provinces = []
    with path.open(encoding="utf-8") as f:
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
            date = row["date"].strip()
            url_igm = row["url_igm"].strip()
            raw_wmit = row["url_wmit"].strip()
            url_wmit = raw_wmit if raw_wmit not in ("TODO", "") else url_igm
            provinces.append(
                Province(
                    region=row["region"].strip(),
                    province=row["province"].strip(),
                    code=code,
                    zip_name=f"{code}_{date}.zip",
                    igm_url=url_igm,
                    wmit_url=url_wmit,
                    date=date,
                    tsv_date=date,
                    status="ok",
                )
            )
    return provinces


def filter_provinces(provinces: list[Province], selector: str) -> list[Province]:
    """Selector: 'all', or comma-separated codes/names/regions."""
    if selector.lower() == "all":
        return provinces
    terms = [s.strip() for s in selector.split(",") if s.strip()]
    seen: set[str] = set()
    result: list[Province] = []
    for term in terms:
        upper, lower = term.upper(), term.lower()
        for p in provinces:
            if p["code"] in seen:
                continue
            if p["code"] == upper or p["province"].lower() == lower or p["region"].lower() == lower:
                seen.add(p["code"])
                result.append(p)
    return result


def parse_args(
    description: str,
    *,
    overwrite: bool = True,
    resolve_provinces: bool = True,
    setup: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--province",
        "-p",
        required=True,
        metavar="CODE|NAME|REGION[,...]|all",
        help="Comma-separated province codes, province names, region names, or 'all'",
    )
    if overwrite:
        parser.add_argument("--overwrite", action="store_true", help="Re-process existing output files")
    if setup:
        setup(parser)
    args = parser.parse_args()
    if resolve_provinces:
        args.provinces = filter_provinces(read_sources(), args.province)
        if not args.provinces:
            parser.error(f"No province matched '{args.province}'")
    return args


def _max_date_main() -> None:
    print(max_date())
