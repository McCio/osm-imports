"""Layer registry for the DBSN pipeline.

Each translate_<layer>.py file owns its LayerDef (name, filter, extension, translate_fn, tag_keys).
This module aggregates them in processing order and exposes get_layer_specs() for extract-time use.
"""

from collections.abc import Mapping
from pathlib import Path

import yaml

from dbsn.layer_types import LayerDef, LayerSpec

_CONFIG_FILE = Path(__file__).parent / "layers.yaml"
_SECTIONS = ("areas", "provinces", "regions")


def _load() -> list[LayerDef]:
    from dbsn.translate_attr_sp import LAYER as attr_sp  # noqa: PLC0415
    from dbsn.translate_edi_min import LAYER as edi_min  # noqa: PLC0415
    from dbsn.translate_edifc import LAYER as edifc  # noqa: PLC0415
    from dbsn.translate_galler import LAYER as galler  # noqa: PLC0415
    from dbsn.translate_mn_ind import LAYER as mn_ind  # noqa: PLC0415
    from dbsn.translate_mn_mau import LAYER as mn_mau  # noqa: PLC0415
    from dbsn.translate_ponte import LAYER as ponte  # noqa: PLC0415
    from dbsn.translate_sc_dis import LAYER as sc_dis  # noqa: PLC0415
    return [edifc, edi_min, mn_ind, mn_mau, attr_sp, sc_dis, ponte, galler]


_cache: list[LayerDef] | None = None


def get_layers() -> list[LayerDef]:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def get_layer(name: str) -> LayerDef:
    for ld in get_layers():
        if ld.name == name:
            return ld
    raise KeyError(name)


def area_key(area: str) -> str:
    """Area name as used in output file names and in layers.yaml keys."""
    return area.lower().replace(" ", "_")


def _checked(names: object, where: str) -> list[str]:
    known = [ld.name for ld in get_layers()]
    if not isinstance(names, list) or not names:
        raise ValueError(f"{where}: expected a non-empty list of layer names")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"{where}: unknown layer(s) {', '.join(map(str, unknown))}. Available: {', '.join(known)}")
    return names


def load_layers_config(path: Path = _CONFIG_FILE) -> dict:
    """Read and validate layers.yaml: every list is non-empty and names only known layers."""
    raw = yaml.safe_load(path.read_text()) or {}
    cfg: dict = {"defaults": _checked(raw.get("defaults"), "defaults")}
    for section in _SECTIONS:
        upper = section == "provinces"
        cfg[section] = {
            (str(k).upper() if upper else area_key(str(k))): _checked(v, f"{section}.{k}")
            for k, v in (raw.get(section) or {}).items()
        }
    return cfg


def _in_registry_order(names: list[str]) -> list[str]:
    return [ld.name for ld in get_layers() if ld.name in names]


def resolve_layer_names(
    p: Mapping[str, str], area: str | None, selected: list[str] | None, config: dict | None = None,
) -> list[str]:
    """Layers to process for province p: CLI selection, else layers.yaml (area, province, region, defaults)."""
    if selected is not None:
        return _in_registry_order(_checked(selected, "--layers"))
    cfg = config if config is not None else load_layers_config()
    for section, key in (
        ("areas", area_key(area) if area else None),
        ("provinces", p["code"].upper()),
        ("regions", area_key(p.get("region", ""))),
    ):
        if key and key in cfg[section]:
            return _in_registry_order(cfg[section][key])
    return _in_registry_order(cfg["defaults"])


def layer_slug(names: list[str]) -> str:
    """'' when every layer is selected (legacy file names), else '_a+b' in registry order."""
    ordered = _in_registry_order(names)
    if len(ordered) == len(get_layers()):
        return ""
    return "_" + "+".join(ordered)


_specs_cache: list[LayerSpec] | None = None


def get_layer_specs() -> list[LayerSpec]:
    """Extract-time specs: picklable (no translate_fn), built lazily."""
    global _specs_cache
    if _specs_cache is None:
        _specs_cache = [LayerSpec(ld.name, ld.filter_fn, ld.supports_extension) for ld in get_layers()]
    return _specs_cache
