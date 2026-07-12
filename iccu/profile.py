source = "https://anagrafe.iccu.sbn.it/"  # https://anagrafe.iccu.sbn.it/open-data/
add_source = False
dataset_id = "isil"
query = [("amenity", "library")]
# bbox = [ 35.5, 6.7, 47.1, 18.5 ]  # True  # how to set italy?
bounded_update = True
regions = "it"
max_distance = 200  # meters
duplicate_distance = 1
master_tags = (
    "ref:isil",
    "ref:sbn",
    "ref:acnp",
    "ref:cei",
    "ref:cmbs",
    "ref:rism",
    "official_name",
    "operator",
    "addr:housenumber",
    "contact:phone",
    "contact:website",
)


def dataset(f):
    import polars as pl  # noqa: PLC0415
    from conflate import SourcePoint  # noqa: PLC0415

    from iccu.export import COL_MAPPER as col_mapper, CSV_SCHEMA_OVERRIDES  # noqa: PLC0415

    csv = pl.read_csv(f, schema_overrides=CSV_SCHEMA_OVERRIDES).filter(~(pl.col("latitudine").is_null() | pl.col("longitudine").is_null())).rename(col_mapper)
    rows_to_use = [*col_mapper.values(), "latitudine", "longitudine"]
    for row in csv.select(*rows_to_use).to_dicts():
        if old_name := row["old_name"]:
            row["old_name"] = old_name.split(";")[0]
        if alt_name := row["alt_name"]:
            row["alt_name"] = alt_name.split(";")[0]
        el = {
            "pid": row["ref:isil"],
            "lat": row["latitudine"],
            "lon": row["longitudine"],
            "tags": row,
        }
        yield SourcePoint(**el)


transform = {
    "latitudine": "-",
    "longitudine": "-",
    "phone": ">contact:phone",
    "amenity": "library",
    "name": ".official_name",
}
