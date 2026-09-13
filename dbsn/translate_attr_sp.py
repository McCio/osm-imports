# coding=UTF-8

"""Tag translation for the DBSN ATTR_SP (attrezzature sportive) layer."""

from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "name",
    "leisure",
    "sport",
    "piste:type",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {}

    if attrs.get("attr_sp_nom", "UNK") not in ("UNK", None, ""):
        tags["name"] = attrs["attr_sp_nom"]

    match attrs.get("attr_sp_ty"):
        case "01":
            tags["leisure"] = "swimming_pool"
        case "02":
            tags["leisure"] = "pitch"
        case "0201":
            tags["leisure"] = "pitch"
            tags["sport"] = "soccer"
        case "0202":
            tags["leisure"] = "pitch"
            tags["sport"] = "tennis"
        case "0203":
            tags["leisure"] = "pitch"
            tags["sport"] = "soccer"
        case "0204":
            tags["leisure"] = "pitch"
            tags["sport"] = "basketball"
        case "0205":
            tags["leisure"] = "pitch"
            tags["sport"] = "boules"
        case "0206":
            tags["leisure"] = "pitch"
            tags["sport"] = "baseball"
        case "0207":
            tags["leisure"] = "pitch"
            tags["sport"] = "rugby_union"
        case "08":
            tags["leisure"] = "track"
        case "0801":
            tags["leisure"] = "track"
            tags["sport"] = "athletics"
        case "0802":
            tags["leisure"] = "track"
            tags["sport"] = "motor"
        case "0803":
            tags["leisure"] = "track"
            tags["sport"] = "karting"
        case "0804":
            tags["leisure"] = "track"
            tags["sport"] = "cycling"
        case "0805":
            tags["leisure"] = "track"
            tags["sport"] = "horse_racing"
        case "0806":
            tags["piste:type"] = "downhill"
        case "0807":
            tags["leisure"] = "ice_rink"
        case "10":
            tags["leisure"] = "pitch"
            tags["sport"] = "shooting"
        case "15":
            tags["leisure"] = "stadium"
        case _:
            tags["leisure"] = "pitch"

    _apply_source_tag(tags, attrs)
    return tags


from dbsn.layer_types import LayerDef
LAYER = LayerDef(name="attr_sp", filter_fn=None, supports_extension=True, translate_fn=translate, tag_keys=TAG_KEYS)
