# coding=UTF-8

"""Tag translation for the DBSN EDI_MIN (minor buildings) layer."""

from dbsn.translate_edifc import _apply_source_tag

TAG_KEYS = [
    "name",
    "layer",
    "building",
    "man_made",
    "barrier",
    "amenity",
    "religion",
    "covered",
    "construction",
    "ruins",
    "height",
    "source",
]


def translate(attrs: dict | None, region: str | None = None) -> dict | None:
    if not attrs:
        return

    tags = {}

    if attrs.get("edi_min_nm", "UNK") != "UNK":
        tags["name"] = attrs["edi_min_nm"]

    match attrs.get("edi_min_ty"):
        case "01":
            tags["building"] = "shed"
        case "02":
            tags["building"] = "kiosk"
        case "03" | "04":
            tags["building"] = "tomb"
        case "05":
            tags["man_made"] = "tower"
            tags["building"] = "yes"
        case "07":
            tags["building"] = "garage"
        case "08":
            tags["building"] = "yes"
            tags["barrier"] = "toll_booth"
        case "14":
            tags["building"] = "yes"
            tags["covered"] = "yes"
        case "17":
            tags["building"] = "roof"
            tags["layer"] = "1"
        case "18":
            tags["building"] = "chapel"
            tags["amenity"] = "place_of_worship"
            tags["religion"] = "christian"
        case _:
            tags["building"] = "yes"

    match attrs.get("edi_min_st"):
        case "01":
            if tags.get("building", "yes") != "yes":
                tags["construction"] = tags["building"]
            elif tags.get("man_made"):
                tags["construction"] = tags["man_made"]
            else:
                tags["construction"] = "yes"
            tags["building"] = "construction"
        case "02":
            tags["ruins"] = "yes"

    ht = attrs.get("edi_min_at")
    if ht and ht > 0:
        tags["height"] = str(round(ht, 1))

    _apply_source_tag(tags, attrs)
    return tags


from dbsn.layer_types import LayerDef
LAYER = LayerDef(name="edi_min", filter_fn=lambda p: p.get("edi_min_pr") == "02", supports_extension=False, translate_fn=translate, tag_keys=TAG_KEYS)
