"""Tests for dbsn.layers config resolution and layer_slug."""

from __future__ import annotations

import pytest

from dbsn.common import parse_layers
from dbsn.convert import convert_out_path
from dbsn.layers import get_layers, layer_slug, load_layers_config, resolve_layer_names

ALL = [ld.name for ld in get_layers()]
VE = {"code": "VE", "region": "Veneto"}


def _cfg(tmp_path, text: str) -> dict:
    f = tmp_path / "layers.yaml"
    f.write_text(text)
    return load_layers_config(f)


def test_shipped_config_defaults_to_edifc():
    assert resolve_layer_names(VE, None, None) == ["edifc"]


def test_cli_selection_wins_and_is_registry_ordered(tmp_path):
    cfg = _cfg(tmp_path, "defaults: [edifc]\nareas: {marghera: [mn_ind]}")
    assert resolve_layer_names(VE, "Marghera", ["attr_sp", "edifc"], cfg) == ["edifc", "attr_sp"]


def test_specificity_area_province_region_defaults(tmp_path):
    cfg = _cfg(
        tmp_path,
        "defaults: [edifc]\nregions: {veneto: [mn_ind]}\nprovinces: {VE: [mn_mau]}\n"
        "areas: {isola_dei_petroli: [ponte]}",
    )
    assert resolve_layer_names(VE, "Isola dei Petroli", None, cfg) == ["ponte"]
    assert resolve_layer_names(VE, "Marghera", None, cfg) == ["mn_mau"]
    assert resolve_layer_names({"code": "PD", "region": "Veneto"}, None, None, cfg) == ["mn_ind"]
    assert resolve_layer_names({"code": "MI", "region": "Lombardia"}, None, None, cfg) == ["edifc"]


def test_list_replaces_not_merges(tmp_path):
    cfg = _cfg(tmp_path, "defaults: [edifc, edi_min]\nprovinces: {VE: [attr_sp]}")
    assert resolve_layer_names(VE, None, None, cfg) == ["attr_sp"]


@pytest.mark.parametrize("text", [
    "defaults: [nope]",
    "defaults: [edifc]\nareas: {x: [nope]}",
    "defaults: []",
    "areas: {}",
])
def test_invalid_config_raises(tmp_path, text):
    with pytest.raises(ValueError):
        _cfg(tmp_path, text)


def test_unknown_cli_layer_raises():
    with pytest.raises(ValueError):
        resolve_layer_names(VE, None, ["nope"])


def test_layer_slug():
    assert layer_slug(ALL) == ""
    assert layer_slug(["edifc"]) == "_edifc"
    assert layer_slug(["attr_sp", "edifc"]) == "_edifc+attr_sp"


def test_parse_layers_all_and_none():
    assert parse_layers(None, ALL) is None
    assert parse_layers("all", ALL) == ALL
    assert parse_layers("edifc, attr_sp", ALL) == ["edifc", "attr_sp"]


def test_convert_out_path_carries_area_and_layer_slug():
    p = {"code": "VE", "date": "20260101"}
    assert convert_out_path(p, None, ALL, "osm").name == "VE_20260101.osm"
    assert convert_out_path(p, "Isola dei Petroli", ["edifc"], "osm.bz2").name == (
        "VE_20260101_isola_dei_petroli_edifc.osm.bz2"
    )
