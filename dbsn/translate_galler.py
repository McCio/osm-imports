# coding=UTF-8

"""Tag translation for the DBSN GALLER (galleria) layer."""

from dbsn.layer_types import LayerDef
from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "name",
    "man_made",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {"man_made": "tunnel"}

    nome = attrs.get("galler_nom", "UNK")
    if nome and nome not in ("UNK", ""):
        tags["name"] = nome

    _apply_source_tag(tags, attrs)
    return tags


LAYER = LayerDef(name="galler", filter_fn=None, supports_extension=False, translate_fn=translate, tag_keys=TAG_KEYS)
