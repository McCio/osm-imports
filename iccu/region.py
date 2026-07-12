"""Region resolution: maps --region input (Italian region name, province name, or 2-char code) to a filter."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import NoReturn

import polars as pl

from iccu.common import ALL_REGION, safe_label


@dataclass(frozen=True)
class RegionFilter:
    label: str  # filesystem-safe key (lowercased input)
    expr: pl.Expr | None  # polars filter expression; None = no filter (all Italy)
    conflate_regions: str  # exact Italian region/province name for conflate --regions
    admin_level: int | None  # OSM admin_level for Overpass area query (4=region, 6=province, None=skip)
    osm_name: str | None = None  # OSM name= tag override for the Overpass area filter; None = use conflate_regions


# OSM name= tag differs from the ICCU name for these regions/provinces.
_OSM_NAME: dict[str, str] = {
    # Regions (admin_level=4): bilingual / minority-language prefixes
    "SARDEGNA": "Sardigna/Sardegna",
    "VALLE D'AOSTA/VALLÉE D'AOSTE": "Valle d'Aosta / Vallée d'Aoste",
    # Provinces (admin_level=6)
    "Roma": "Roma Capitale",
    "Oristano": "Aristanis/Oristano",
    "Imperia": "Provincia di Imperia",
    "Trento": "Provincia di Trento",
    "Bolzano/Bozen": "Bolzano - Bozen",
    "Pordenone": "Pordenone / Pordenon",
    "Gorizia": "Gorizia / Gurize / Gorica",
    "Udine": "Udine / Udin / Videm",
    "Sassari": "Tàttari/Sassari",
    "Cagliari": "Casteddu/Cagliari",
    # Valle d'Aosta doubles as its own province; OSM only has admin_level=4, not 6
    "Valle d'Aosta/Vallée d'Aoste": "Valle d'Aosta / Vallée d'Aoste",
    # Sud Sardegna (created 2016) not yet in OSM; covered by two pre-reform provinces.
    # Sud Sardegna itself included so the filter works once OSM catches up.
    "Sud Sardegna": "(Medio Campidano|Sulcis Iglesiente|Sud Sardegna)",
}

# Provinces where OSM uses admin_level=4 instead of the usual 6
_OSM_ADMIN_LEVEL: dict[str, int] = {
    "Valle d'Aosta/Vallée d'Aoste": 4,
}


def resolve(term: str, df: pl.DataFrame) -> RegionFilter:
    """Resolve a --region term against clean.csv.

    Accepts (case-insensitive):
    - "all"              → no filter, conflate against all Italy
    - Italian region name (e.g. "Lombardia")
    - Italian province name (e.g. "Milano")
    - 2-char province code (e.g. "MI") — matched against ISIL prefix IT-XX
    """
    if term.lower() == ALL_REGION:
        return RegionFilter(label=ALL_REGION, expr=None, conflate_regions="italy", admin_level=None)

    lower = term.lower()
    label = safe_label(term)

    # 2-char province code → match first two chars of ISIL after "IT-"
    if len(term) == 2 and term.isalpha():
        code = term.upper()
        expr = pl.col("codice-isil").str.slice(3, 2).str.to_uppercase() == code
        matched = df.filter(expr)
        if matched.is_empty():
            _fail(f"No libraries found for province code '{code}'.")
        return _province_filter(label, expr, matched)

    # Italian region name
    expr_region = pl.col("regione").str.to_lowercase() == lower
    matched = df.filter(expr_region)
    if not matched.is_empty():
        iccu = _region_of(matched)
        return RegionFilter(label=label, expr=expr_region, conflate_regions=iccu, admin_level=4, osm_name=_OSM_NAME.get(iccu))

    # Italian province name
    expr_province = pl.col("provincia").str.to_lowercase() == lower
    matched = df.filter(expr_province)
    if not matched.is_empty():
        return _province_filter(label, expr_province, matched)

    _fail(
        f"'{term}' did not match any region or province in the dataset. "
        "Use an Italian region name (e.g. 'Lombardia'), province name (e.g. 'Milano'), "
        "or 2-char province code (e.g. 'MI')."
    )


def _province_filter(label: str, expr: pl.Expr, matched: pl.DataFrame) -> RegionFilter:
    iccu = _province_of(matched)
    admin_level = _OSM_ADMIN_LEVEL.get(iccu, 6)
    return RegionFilter(label=label, expr=expr, conflate_regions=iccu, admin_level=admin_level, osm_name=_OSM_NAME.get(iccu))


def _region_of(df: pl.DataFrame) -> str:
    return df["regione"][0]


def _province_of(df: pl.DataFrame) -> str:
    return df["provincia"][0]


def _fail(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(1)
