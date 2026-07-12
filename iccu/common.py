"""Shared paths and helpers for the ICCU library import pipeline."""

import argparse
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "iccu"
SOURCE_DIR = DATA_DIR / "source"
OVERPASS_DIR = DATA_DIR / "overpass"
OSM_DIR = DATA_DIR / "osm"

ETAG_FILE = SOURCE_DIR / "iccu.etag"
ZIP_FILE = DATA_DIR / "iccu.zip"
CLEAN_CSV = DATA_DIR / "clean.csv"
PROFILE_PY = Path(__file__).parent / "profile.py"
SOURCE_URL = "https://opendata.anagrafe.iccu.sbn.it/opendata.zip"
USER_AGENT = "iccu-pipeline/0.1 (OSM ICCU libraries import; https://wiki.openstreetmap.org/wiki/Import/Catalogue/ICCU)"

ALL_REGION = "all"

REGION_METAVAR = f"CODE|NAME|REGION[,...]|{ALL_REGION}"
REGION_HELP = "2-char province code, province name, Italian region name, comma-separated list, or 'all' (default: all)"


def safe_label(term: str) -> str:
    """Return a filesystem- and GitHub-asset-safe label for a region term.

    Lowercases, strips accents via NFKD, then collapses any run of
    non-alphanumeric characters to a single underscore.
    Examples: "L'Aquila" -> "l_aquila", "Reggio Calabria" -> "reggio_calabria",
              "Forlì-Cesena" -> "forli_cesena", "Vallée d'Aoste" -> "vallee_d_aoste".
    """
    nfkd = unicodedata.normalize("NFKD", term.lower())
    ascii_only = nfkd.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_only).strip("_")


def parse_regions(arg: str) -> list[str]:
    return list(dict.fromkeys(r.strip().lower() for r in arg.split(",") if r.strip()))


def parse_args(
    description: str,
    *,
    overwrite: bool = True,
    region: bool = False,
    setup: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    if region:
        parser.add_argument("--region", default=ALL_REGION, metavar=REGION_METAVAR, help=REGION_HELP)
    if overwrite:
        parser.add_argument("--overwrite", action="store_true", help="Re-process even if outputs exist")
    if setup:
        setup(parser)
    return parser.parse_args()


def overpass_cache(region: str) -> Path:
    return OVERPASS_DIR / f"{region.lower()}.osm"


def osm_output_dir(region: str) -> Path:
    return OSM_DIR / region.lower()


def dataset_osm(region: str, compress: bool = False) -> Path:
    return osm_output_dir(region) / ("dataset.osm.bz2" if compress else "dataset.osm")


def dataset_geojson(region: str) -> Path:
    return osm_output_dir(region) / "dataset.geojson"


def changes_osm(region: str) -> Path:
    return osm_output_dir(region) / "changes.osm"


def changes_osc(region: str) -> Path:
    return osm_output_dir(region) / "changes.osc"


def changes_geojson(region: str) -> Path:
    return osm_output_dir(region) / "changes.geojson"


def http_client(**kwargs) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, **kwargs)
