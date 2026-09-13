# coding=UTF-8

"""Tag translation for the DBSN MN_IND (manufatti industriali) layer."""

from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "man_made",
    "power",
    "generator:source",
    "generator:type",
    "building",
    "utility",
    "height",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {}

    match attrs.get("mn_ind_ty"):
        case "01":
            tags["power"] = "substation"
        case "02":
            tags["man_made"] = "yes"
            tags["utility"] = "water"
        case "03":
            tags["man_made"] = "yes"
            tags["utility"] = "gas"
        case "04":
            tags["power"] = "generator"
            tags["generator:source"] = "wind"
            tags["generator:type"] = "horizontal_axis"
        case "06":
            tags["man_made"] = "chimney"
        case "0702":
            tags["man_made"] = "storage_tank"
        case "0703":
            tags["man_made"] = "silo"
        case "07":
            tags["building"] = "industrial"
        case "08":
            tags["power"] = "plant"
            tags["building"] = "yes"
        case "09":
            tags["man_made"] = "pumping_station"
        case "10":
            tags["building"] = "industrial"
        case "11":
            tags["man_made"] = "reservoir"
        case "12":
            tags["man_made"] = "water_tower"
        case "13":
            tags["building"] = "greenhouse"
        case _:
            tags["man_made"] = "yes"

    ht = attrs.get("mn_ind_at")
    if ht and ht > 0:
        tags["height"] = str(round(ht, 1))

    _apply_source_tag(tags, attrs)
    return tags


from dbsn.layer_types import LayerDef
LAYER = LayerDef(name="mn_ind", filter_fn=None, supports_extension=True, translate_fn=translate, tag_keys=TAG_KEYS)
