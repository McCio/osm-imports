# coding=UTF-8

"""Tag translation for the DBSN SC_DIS (area di scavo o discarica) layer."""

from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "landuse",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {}

    match attrs.get("sc_dis_ty"):
        case "01":
            tags["landuse"] = "landfill"
        case "02":
            tags["landuse"] = "quarry"
        case _:
            return None  # 93/95 → drop

    _apply_source_tag(tags, attrs)
    return tags


from dbsn.layer_types import LayerDef
LAYER = LayerDef(name="sc_dis", filter_fn=lambda p: p.get("sc_dis_ty") in ("01", "02"), supports_extension=True, translate_fn=translate, tag_keys=TAG_KEYS)
