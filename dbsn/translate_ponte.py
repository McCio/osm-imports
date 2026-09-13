# coding=UTF-8

"""Tag translation for the DBSN PONTE (ponte/viadotto/cavalcavia) layer."""

from dbsn.layer_types import LayerDef
from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "name",
    "man_made",
    "bridge:movable",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {"man_made": "bridge"}

    nome = attrs.get("ponte_nome", "UNK")
    if nome and nome not in ("UNK", ""):
        tags["name"] = nome

    if attrs.get("ponte_stru") == "06":
        tags["bridge:movable"] = "yes"

    _apply_source_tag(tags, attrs)
    return tags


LAYER = LayerDef(name="ponte", filter_fn=None, supports_extension=False, translate_fn=translate, tag_keys=TAG_KEYS)
