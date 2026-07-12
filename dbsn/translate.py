# coding=UTF-8

"""
Tag translation for DBSN EDIFC (buildings) layer.

Based on musuruan/osm_imports DBSN/edifici.py (Andrea Musuruane <musuruan@gmail.com>),
licensed ODbL 1.0.  Local copy maintained here to extend mappings independently.

Changes vs upstream:
  - edifc_ty 07 (campanile): building=yes → man_made=tower + tower:type=bell_tower
  - edifc_ty 10 (castello): building=yes → building=castle
  - edifc_ty 13 (faro): building=yes → man_made=lighthouse
  - edifc_ty 15 (minareto/moschea): building=yes → building=mosque
  - edifc_ty 16 (tempio): building=yes → building=temple
  - edifc_ty 20 (sinagoga): building=yes → building=synagogue
  - edifc_ty 22 (cattedrale): building=church → building=cathedral
  - edifc_uso 0201 (municipio): added building=civic
  - edifc_uso 030102 (ospedale): added building=hospital
  - edifc_uso 030301 (sede di scuola): added building=school
  - edifc_uso 030302 (università): added building=university
  - edifc_uso 0307 (caserma pompieri): added building=fire_station
  - edifc_uso 060301 (stazione ferroviaria): added building=train_station
  - edifc_uso 060202 (parcheggio multipiano): added building=parking
  - edifc_uso 08/0801 (industriale): added building=industrial
  - edifc_uso 100103 (teatro/auditorium): added amenity=theatre
  - edifc_uso 100104 (museo): added tourism=museum
  - edifc_uso 1201/1202 (albergo/locanda): added building=hotel + tourism=hotel
  - edifc_uso 1204 (rifugio montano): added tourism=alpine_hut
  - edifc_mon 01 (monumentale): added historic=yes
  - edifc_at (altezza): mapped to height=
"""

TAG_KEYS = [
    "name",
    "layer",
    "building",
    "man_made",
    "tower:type",
    "house",
    "amenity",
    "tourism",
    "construction",
    "ruins",
    "historic",
    "height",
]


def translate(attrs: dict | None) -> dict | None:
    if not attrs:
        return

    tags = {}

    if attrs.get("edifc_nome", "UNK") != "UNK":
        tags["name"] = attrs["edifc_nome"]

    if attrs.get("edifc_sot") == "02":
        tags["layer"] = "-1"

    match attrs.get("edifc_ty"):
        case "05":
            tags["building"] = "house"
            tags["house"] = "terraced"
        case "07":
            tags["building"] = "yes"
            tags["man_made"] = "tower"
            tags["tower:type"] = "bell_tower"
        case "10":
            tags["building"] = "castle"
        case "11":
            tags["building"] = "church"
        case "13":
            tags["man_made"] = "lighthouse"
            tags["building"] = "yes"
        case "15":
            tags["building"] = "mosque"
        case "16":
            tags["building"] = "temple"
        case "20":
            tags["building"] = "synagogue"
        case "22":
            tags["building"] = "cathedral"
        case "23":
            tags["building"] = "roof"
        case _:
            tags["building"] = "yes"

    match attrs.get("edifc_uso"):
        case "0201":
            tags["building"] = "civic"
            tags["amenity"] = "townhall"
        case "030102":
            tags["building"] = "hospital"
            tags["amenity"] = "hospital"
        case "030301":
            tags["building"] = "school"
            tags["amenity"] = "school"
        case "030302":
            tags["building"] = "university"
            tags["amenity"] = "university"
        case "0307":
            tags["building"] = "fire_station"
            tags["amenity"] = "fire_station"
        case "0304":
            tags["amenity"] = "post_office"
        case "05":
            tags["amenity"] = "place_of_worship"
        case "060202":
            tags["building"] = "parking"
        case "060301":
            tags["building"] = "train_station"
        case "08" | "0801":
            tags["building"] = "industrial"
        case "100101":
            tags["amenity"] = "library"
        case "100102":
            tags["amenity"] = "cinema"
        case "100103":
            tags["amenity"] = "theatre"
        case "100104":
            tags["tourism"] = "museum"
        case "1201" | "1202":
            tags["building"] = "hotel"
            tags["tourism"] = "hotel"
        case "1204":
            tags["tourism"] = "alpine_hut"

    match attrs.get("edifc_stat"):
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

    if attrs.get("edifc_mon") == "01":
        tags["historic"] = "yes"

    ht = attrs.get("edifc_at")
    if ht and ht > 0:
        tags["height"] = str(round(ht, 1))

    return tags
