"""Region resolution: maps --region input (Italian region name, province name, or 2-char code) to a filter."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import NoReturn

import polars as pl

from iccu.common import ALL_REGION


@dataclass(frozen=True)
class RegionFilter:
    label: str  # filesystem-safe key (lowercased input)
    expr: pl.Expr | None  # polars filter expression; None = no filter (all Italy)
    conflate_regions: str  # exact Italian region name for conflate --regions


def resolve(term: str, df: pl.DataFrame) -> RegionFilter:
    """Resolve a --region term against clean.csv.

    Accepts (case-insensitive):
    - "all"              → no filter, conflate against all Italy
    - Italian region name (e.g. "Lombardia")
    - Italian province name (e.g. "Milano")
    - 2-char province code (e.g. "MI") — matched against ISIL prefix IT-XX
    """
    if term.lower() == ALL_REGION:
        return RegionFilter(label=ALL_REGION, expr=None, conflate_regions="italy")

    label = term.lower()

    # 2-char province code → match first two chars of ISIL after "IT-"
    if len(term) == 2 and term.isalpha():
        code = term.upper()
        expr = pl.col("codice-isil").str.slice(3, 2).str.to_uppercase() == code
        matched = df.filter(expr)
        if matched.is_empty():
            _fail(f"No libraries found for province code '{code}'.")
        return RegionFilter(label=label, expr=expr, conflate_regions=_province_of(matched))

    # Italian region name
    expr_region = pl.col("regione").str.to_lowercase() == label
    matched = df.filter(expr_region)
    if not matched.is_empty():
        return RegionFilter(label=label, expr=expr_region, conflate_regions=_region_of(matched))

    # Italian province name
    expr_province = pl.col("provincia").str.to_lowercase() == label
    matched = df.filter(expr_province)
    if not matched.is_empty():
        return RegionFilter(label=label, expr=expr_province, conflate_regions=_province_of(matched))

    _fail(
        f"'{term}' did not match any region or province in the dataset. "
        "Use an Italian region name (e.g. 'Lombardia'), province name (e.g. 'Milano'), "
        "or 2-char province code (e.g. 'MI')."
    )


def _region_of(df: pl.DataFrame) -> str:
    return df["regione"][0]


def _province_of(df: pl.DataFrame) -> str:
    return df["provincia"][0]


def _fail(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(1)
