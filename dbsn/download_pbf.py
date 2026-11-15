"""DAG task: download osmit-estratti province PBF and optionally clip to an area."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unicodedata
from datetime import date as _date
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup
from shapely.geometry import mapping

from dbsn.common import CONFLATE_DIR, Province, http_client, load_area_clip
from dbsn.download import DownloadTask
from utils.dag import Task

OSMIT_BASE = "https://osmit-estratti.wmcloud.org/"


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = nfkd.encode("ASCII", "ignore").decode("ASCII")
    return re.sub(r"[^a-z0-9]", "", ascii_s.lower())


def _fetch_province_listing(client) -> dict[str, str]:
    resp = client.get(f"{OSMIT_BASE}output/pbf/province/", timeout=30)
    resp.raise_for_status()
    result: dict[str, str] = {}
    for a in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
        href: str = a["href"]
        if href.endswith(".osm.pbf"):
            stem = href.removesuffix(".osm.pbf")
            parts = stem.split("_", 1)
            if len(parts) == 2:
                result[_normalize(parts[1])] = href
    return result


def resolve_url(province_name: str, listing: dict[str, str]) -> str:
    candidates = [province_name, *re.split(r"[/\-,]", province_name)]
    for candidate in candidates:
        key = _normalize(candidate.strip())
        if key in listing:
            return f"{OSMIT_BASE}output/pbf/province/{listing[key]}"
    raise ValueError(
        f"No osmit-estratti PBF found for province '{province_name}'. "
        f"Available (normalised): {sorted(listing)}"
    )


def find_pbf(code: str, area_slug: str = "") -> Path | None:
    """Return the newest PBF for this province code (and optional area slug), or None."""
    suffix = f"_{area_slug}" if area_slug else ""
    matches = sorted(CONFLATE_DIR.glob(f"{code}_*/{code}_*{suffix}.osm.pbf"))
    if not area_slug:
        # empty slug = full-province PBF only; "." sorts before "_", so an area-clipped one would win
        full = re.compile(rf"{re.escape(code)}_\d{{4}}-\d{{2}}-\d{{2}}\.osm\.pbf")
        matches = [m for m in matches if full.fullmatch(m.name)]
    return matches[-1] if matches else None


def _province_pbf_path(code: str, server_date: str) -> Path:
    return CONFLATE_DIR / f"{code}_{server_date}" / f"{code}_{server_date}.osm.pbf"


def _area_pbf_path(code: str, server_date: str, area_slug: str) -> Path:
    return CONFLATE_DIR / f"{code}_{server_date}" / f"{code}_{server_date}_{area_slug}.osm.pbf"


def _osmium_extract(src: Path, dst: Path, clip_geom) -> None:
    """Clip src PBF to clip_geom polygon using osmium extract."""
    geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": mapping(clip_geom), "properties": {}}],
    }
    with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w", delete=False) as f:
        json.dump(geojson, f)
        clip_path = f.name
    dst.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            ["osmium", "extract", "--polygon", clip_path,
             "--set-bounds", "-o", str(dst), str(src)],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"osmium extract failed (exit {result.returncode}):\n{result.stderr.strip()}"
            )
    finally:
        Path(clip_path).unlink()


class DownloadPbfTask(Task):
    step = "pbf"

    def __init__(self, prov: Province, area: str | None = None) -> None:
        self._prov = prov
        self._area = area
        self._area_slug = _normalize(area) if area else ""

    @property
    def name(self) -> str:
        return f"pbf:{self._prov['code']}"

    def skip_if(self) -> bool:
        return find_pbf(self._prov["code"], self._area_slug) is not None

    def dependencies(self) -> list[Task]:
        if not self._area:
            return []
        # Need the DBSN zip to read the clip geometry for osmium extract
        return [DownloadTask(self._prov)]

    def run(self) -> None:
        p = self._prov

        with http_client(timeout=300) as client:
            listing = _fetch_province_listing(client)
            url = resolve_url(p["province"], listing)
            print(f"  [pbf    ] {p['code']} {p['province']}: downloading {url}")
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                last_mod = resp.headers.get("Last-Modified")
                if last_mod:
                    dt = parsedate_to_datetime(last_mod)
                    server_date = dt.strftime("%Y-%m-%d")
                    mtime: float | None = dt.timestamp()
                else:
                    server_date = _date.today().isoformat()
                    mtime = None

                province_pbf = _province_pbf_path(p["code"], server_date)
                province_pbf.parent.mkdir(parents=True, exist_ok=True)
                with province_pbf.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)

        if mtime is not None:
            os.utime(province_pbf, (mtime, mtime))
        size_kb = province_pbf.stat().st_size // 1024
        print(f"  [pbf    ] {p['code']}: {size_kb} KB → {province_pbf.name}")

        if not self._area:
            return

        # Clip province PBF to the area polygon using osmium extract
        clip_geom = load_area_clip(p, self._area)
        if clip_geom is None:
            print(f"  [pbf    ] {p['code']}: area clip unavailable, province PBF used as-is")
            return

        area_pbf = _area_pbf_path(p["code"], server_date, self._area_slug)
        print(f"  [pbf    ] {p['code']}: extracting area '{self._area}' → {area_pbf.name}")
        _osmium_extract(province_pbf, area_pbf, clip_geom)
        size_kb = area_pbf.stat().st_size // 1024
        print(f"  [pbf    ] {p['code']}: {size_kb} KB → {area_pbf.name}")
