# coding=UTF-8

"""
Tag translation for DBSN EDIFC (buildings) layer.

Based on musuruan/osm_imports DBSN/edifici.py (Andrea Musuruane <musuruan@gmail.com>),
licensed ODbL 1.0.  Local copy maintained here to extend mappings independently.

Changes vs upstream:
  - edifc_ty 07 (campanile): building=yes → man_made=tower + tower:type=bell_tower
  - edifc_ty 10 (castello): building=yes → building=castle
  - edifc_ty 13 (faro): building=yes → man_made=lighthouse
  - edifc_ty 14 (hangar): building=yes → building=hangar
  - edifc_ty 15 (minareto/moschea): building=yes → building=mosque
  - edifc_ty 16 (tempio): building=yes → building=temple
  - edifc_ty 19 (palestra): building=yes → building=sports_hall
  - edifc_ty 20 (sinagoga): building=yes → building=synagogue
  - edifc_ty 21 (stadio): building=yes → building=stadium
  - edifc_ty 22 (cattedrale): building=church → building=cathedral
  - edifc_ty 24 (bastione): building=yes + defensive_works=bastion
  - edifc_ty 25 (mura): building=yes + historic=citywalls
  - edifc_uso 01 (residenziale): building=residential
  - edifc_uso 02 (uffici generici): building=office
  - edifc_uso 0201 (municipio): added building=civic
  - edifc_uso 0203 (uffici regionali/provinciali): building=civic + office=government + admin_level=4
  - edifc_uso 030101 (struttura socio-assistenziale): amenity=social_facility
  - edifc_uso 030102 (ospedale): added building=hospital
  - edifc_uso 030103 (ambulatorio/poliambulatorio): amenity=clinic + healthcare=clinic
  - edifc_uso 030104 (casa di cura/clinica): amenity=hospital + healthcare=hospital + fixme
  - edifc_uso 030301 (sede di scuola): added building=school
  - edifc_uso 030302 (università): added building=university
  - edifc_uso 0306 (caserma polizia/carabinieri): building=police + amenity=police
  - edifc_uso 0307 (caserma pompieri): added building=fire_station
  - edifc_uso 05 (religioso): skip amenity=place_of_worship when edifc_ty=07 (campanile)
  - edifc_uso 060101 (aeroporto): aeroway=aerodrome
  - edifc_uso 060102 (eliporto): aeroway=heliport
  - edifc_uso 060201 (stazione bus): amenity=bus_station + public_transport=station
  - edifc_uso 060202 (parcheggio multipiano): added building=parking
  - edifc_uso 060301 (stazione ferroviaria): added building=train_station
  - edifc_uso 060404 (stazione funivia/teleferica): aerialway=station
  - edifc_uso 0701 (banca): amenity=bank
  - edifc_uso 0702 (grande magazzino): shop=department_store
  - edifc_uso 0703/0704 (supermercato): shop=supermarket
  - edifc_uso 08/0801 (industriale): added building=industrial
  - edifc_uso 0802* (energia/elettricità): building=service + utility=power
  - edifc_uso 0804 (depuratore): man_made=wastewater_plant
  - edifc_uso 0806 (telecomunicazioni): building=service + utility=telecom
  - edifc_uso 0901 (casa singola): building=house
  - edifc_uso 0902/0903/0904 (rurale/agricolo): building=farm_auxiliary
  - edifc_uso 1001 (edificio pubblico generico): building=civic
  - edifc_uso 1002 (impianto sportivo): building=sports_centre
  - edifc_uso 100103 (teatro/auditorium): added amenity=theatre
  - edifc_uso 100104 (museo): added tourism=museum
  - edifc_uso 100201 (piscina coperta): leisure=swimming_pool + indoor=yes
  - edifc_uso 100202 (palestra coperta): leisure=sports_hall
  - edifc_uso 11 (carcere): amenity=prison
  - edifc_uso 1201/1202 (albergo/locanda): added building=hotel + tourism=hotel
  - edifc_uso 1203 (campeggio): tourism=camp_site
  - edifc_uso 1204 (rifugio montano): added tourism=alpine_hut
  - edifc_mon 01 (monumentale): added historic=yes
  - edifc_at (altezza): mapped to height=
  - meta_ist: mapped to source=DBSN;IGM[;…]
  - check_geom 01: fixme:geometry tag

Per-region overrides (via make_translator):
  - Umbria: edifc_mon 01 → historic=monument (+ fixme) instead of generic historic=yes
"""

from collections.abc import Callable

TAG_KEYS = [
    "name",
    "layer",
    "location",
    "building",
    "man_made",
    "tower:type",
    "house",
    "amenity",
    "healthcare",
    "tourism",
    "shop",
    "office",
    "admin_level",
    "aeroway",
    "aerialway",
    "public_transport",
    "railway",
    "leisure",
    "indoor",
    "parking",
    "utility",
    "defensive_works",
    "construction",
    "ruins",
    "historic",
    "height",
    "source",
    "fixme:geometry",
    "fixme:building",
    "fixme:classify",
]


def _base_translate(attrs: dict | None) -> dict | None:
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
        case "14":
            tags["building"] = "hangar"
        case "15":
            tags["building"] = "mosque"
        case "16":
            tags["building"] = "temple"
        case "19":
            tags["building"] = "sports_hall"
        case "20":
            tags["building"] = "synagogue"
        case "21":
            tags["building"] = "stadium"
        case "22":
            tags["building"] = "cathedral"
        case "23":
            tags["building"] = "roof"
        case "24":
            tags["building"] = "yes"
            tags["defensive_works"] = "bastion"
        case "25":
            tags["building"] = "yes"
            tags["historic"] = "citywalls"
        case _:
            tags["building"] = "yes"

    match attrs.get("edifc_uso"):
        case "01":
            tags["building"] = "residential"
        case "02":
            tags["building"] = "office"
        case "0201":
            tags["building"] = "civic"
            tags["amenity"] = "townhall"
        case "0203":
            tags["building"] = "civic"
            tags["office"] = "government"
            tags["admin_level"] = "4"
        case "030101":
            tags["amenity"] = "social_facility"
        case "030102":
            tags["building"] = "hospital"
            tags["amenity"] = "hospital"
        case "030103":
            tags["amenity"] = "clinic"
            tags["healthcare"] = "clinic"
        case "030104":
            tags["amenity"] = "hospital"
            tags["healthcare"] = "hospital"
            tags["fixme:classify"] = "check: if no hospitalisation → amenity=clinic + healthcare=clinic"
        case "030301":
            tags["building"] = "school"
            tags["amenity"] = "school"
        case "030302":
            tags["building"] = "university"
            tags["amenity"] = "university"
        case "0304":
            tags["amenity"] = "post_office"
        case "0306":
            tags["building"] = "police"
            tags["amenity"] = "police"
        case "0307":
            tags["building"] = "fire_station"
            tags["amenity"] = "fire_station"
        case "05":
            if attrs.get("edifc_ty") != "07":
                tags["amenity"] = "place_of_worship"
        case "060101":
            tags["aeroway"] = "aerodrome"
        case "060102":
            tags["aeroway"] = "heliport"
        case "060201":
            tags["amenity"] = "bus_station"
            tags["public_transport"] = "station"
        case "060202":
            tags["building"] = "parking"
        case "060301":
            tags["building"] = "train_station"
        case "060404":
            tags["aerialway"] = "station"
        case "0701":
            tags["amenity"] = "bank"
        case "0702":
            tags["shop"] = "department_store"
        case "0703" | "0704":
            tags["shop"] = "supermarket"
        case "08" | "0801":
            tags["building"] = "industrial"
        case "0802" | "080201" | "080202" | "080203" | "080206":
            tags["building"] = "service"
            tags["utility"] = "power"
        case "0804":
            tags["man_made"] = "wastewater_plant"
        case "0806":
            tags["building"] = "service"
            tags["utility"] = "telecom"
        case "0901":
            tags["building"] = "house"
        case "0902" | "0903" | "0904":
            tags["building"] = "farm_auxiliary"
        case "1001":
            tags["building"] = "civic"
        case "1002":
            tags["building"] = "sports_centre"
        case "100101":
            tags["amenity"] = "library"
        case "100102":
            tags["amenity"] = "cinema"
        case "100103":
            tags["amenity"] = "theatre"
        case "100104":
            tags["tourism"] = "museum"
        case "100201":
            tags["leisure"] = "swimming_pool"
            tags["indoor"] = "yes"
        case "100202":
            tags["leisure"] = "sports_hall"
        case "11":
            tags["amenity"] = "prison"
        case "1201" | "1202":
            tags["building"] = "hotel"
            tags["tourism"] = "hotel"
        case "1203":
            tags["tourism"] = "camp_site"
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

    match attrs.get("meta_ist"):
        case "01":
            tags["source"] = "DBSN;IGM"
        case "02":
            tags["source"] = "DBSN;IGM;AGEA"
        case "04":
            tags["source"] = "DBSN;IGM;Regione"
        case "06":
            tags["source"] = "DBSN;IGM;CNR"
        case "0101":
            tags["source"] = "DBSN;IGM;ERM"
        case "0504":
            tags["source"] = "DBSN;IGM;Agenzia Entrate"

    if attrs.get("check_geom") == "01":
        tags["fixme:geometry"] = "check if building is cut on regional border"

    return tags


def _umbria_overrides(tags: dict, attrs: dict) -> None:
    if attrs.get("edifc_mon") == "01":
        tags["historic"] = "monument"
        tags["fixme:building"] = "verify: if real monument add descriptive tags"


_REGION_OVERRIDES: dict[str, Callable[[dict, dict], None]] = {
    "Umbria": _umbria_overrides,
}


def make_translator(province=None) -> Callable[[dict | None], dict | None]:
    """Return a translate fn optionally enhanced with per-region overrides."""
    if not province:
        return translate
    override = _REGION_OVERRIDES.get(province["region"] or "")
    if not override:
        return translate

    def _fn(attrs: dict | None) -> dict | None:
        tags = _base_translate(attrs)
        if tags is not None:
            override(tags, attrs)
        return tags

    return _fn


def translate(attrs: dict | None) -> dict | None:
    return _base_translate(attrs)
