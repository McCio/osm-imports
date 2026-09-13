# coding=UTF-8

"""Tag translation for the DBSN MN_MAU (manufatti monumentali e di arredo urbano) layer."""

from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "name",
    "amenity",
    "historic",
    "man_made",
    "shelter_type",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {}

    if attrs.get("mn_mau_nom", "UNK") not in ("UNK", None, ""):
        tags["name"] = attrs["mn_mau_nom"]

    match attrs.get("mn_mau_ty"):
        case "01":
            tags["amenity"] = "fountain"
        case "02":
            tags["historic"] = "monument"
        case "03":
            tags["amenity"] = "shelter"
            tags["shelter_type"] = "gazebo"
        case "04":
            tags["historic"] = "aqueduct"
            tags["man_made"] = "aqueduct"
        case _:
            tags["historic"] = "yes"

    _apply_source_tag(tags, attrs)
    return tags


from dbsn.layer_types import LayerDef
LAYER = LayerDef(name="mn_mau", filter_fn=None, supports_extension=True, translate_fn=translate, tag_keys=TAG_KEYS)
