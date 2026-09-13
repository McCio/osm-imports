"""Layer registry for the DBSN pipeline.

Each translate_<layer>.py file owns its LayerDef (name, filter, extension, translate_fn, tag_keys).
This module aggregates them in processing order and exposes get_layer_specs() for extract-time use.
"""

from dbsn.layer_types import LayerDef, LayerSpec


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


_specs_cache: list[LayerSpec] | None = None


def get_layer_specs() -> list[LayerSpec]:
    """Extract-time specs: picklable (no translate_fn), built lazily."""
    global _specs_cache
    if _specs_cache is None:
        _specs_cache = [LayerSpec(ld.name, ld.filter_fn, ld.supports_extension) for ld in get_layers()]
    return _specs_cache
