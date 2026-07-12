"""Join ICCU source files, normalise contacts and addresses, write clean.csv."""

import pandas as pd
import phonenumbers as pn
import polars as pl

from iccu.common import CLEAN_CSV, SOURCE_DIR, parse_args


def _strip_strings(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(df.select(pl.col(pl.Utf8).str.strip_chars()))


def run(overwrite: bool = False) -> None:
    print("=== Step 2: Clean ===")
    if not overwrite and CLEAN_CSV.exists():
        print("clean.csv exists, skipping (use --overwrite to reprocess).")
        return
    contatti = pl.from_pandas(pd.read_xml(SOURCE_DIR / "contatti.xml")).drop("denominazione", "codice-sbn")
    contatti = _strip_strings(contatti)
    fondi_speciali = pl.from_pandas(pd.read_xml(SOURCE_DIR / "fondi-speciali.xml")).drop("denominazione")
    fondi_speciali = _strip_strings(fondi_speciali)
    patrimonio = pl.from_pandas(pd.read_xml(SOURCE_DIR / "patrimonio.xml")).drop("denominazione")
    patrimonio = _strip_strings(patrimonio)
    biblioteche = pl.read_json(SOURCE_DIR / "biblioteche.json")
    biblioteche = (
        biblioteche.drop("metadati")
        .explode("biblioteche")
        .unnest("biblioteche")
        .unnest("codici-identificativi")
        .unnest("denominazioni")
        .rename(
            {
                "isil": "codice-isil",
                "ufficiale": "denominazione",
                "precedenti": "denominazioni-precedenti",
                "alternative": "denominazioni-alternative",
            }
        )
        .drop("indirizzo")
    )
    biblioteche = _strip_strings(biblioteche)
    territorio = pl.read_csv(SOURCE_DIR / "territorio.csv", separator=";").drop(
        "denominazione", "codice-sbn", "codice istat comune", "codice istat provincia"
    )
    territorio = _strip_strings(territorio)
    tipologie = (
        pl.read_csv(SOURCE_DIR / "tipologie.csv", separator=";")
        .drop("denominazione biblioteca")
        .rename({"codice isil": "codice-isil"})
    )
    tipologie = _strip_strings(tipologie)

    print("Libraries before cleanup:", len(biblioteche))
    complete = biblioteche.join(contatti, on="codice-isil", how="left", validate="1:1", coalesce=True)
    complete = complete.join(fondi_speciali, on="codice-isil", how="left", validate="1:1", coalesce=True)
    complete = complete.join(patrimonio, on="codice-isil", how="left", validate="1:1", coalesce=True)
    complete = complete.join(territorio, on="codice-isil", how="left", validate="1:1", coalesce=True)
    complete = complete.join(tipologie, on="codice-isil", how="left", validate="1:1", coalesce=True)

    reserved_access = pl.col("accesso").struct.field("riservato")
    wheelchair_access = pl.col("accesso").struct.field("portatori-handicap")
    complete = complete.with_columns(
        pl.when(reserved_access.eq(True))
        .then(pl.lit("permit"))
        .when(reserved_access.eq(False))
        .then(pl.lit("yes"))
        .alias("access"),
        pl.when(wheelchair_access.eq(True))
        .then(pl.lit("yes"))
        .when(wheelchair_access.eq(False))
        .then(pl.lit("no"))
        .alias("wheelchair"),
        pl.when(pl.col("stato-registrazione").eq_missing(None))
        .then(pl.lit("Biblioteca censita"))
        .otherwise("stato-registrazione")
        .alias("stato-registrazione"),
        pl.col("latitudine").str.replace(",", ".").str.to_decimal(),
        pl.col("longitudine").str.replace(",", ".").str.to_decimal(),
    )
    complete = complete.filter(
        pl.col("stato-registrazione").ne(pl.lit("Biblioteca non più esistente"))
        & pl.col("stato-registrazione").ne(pl.lit("Biblioteca non censita"))
        & pl.col("stato-registrazione").ne(pl.lit("Altri istituti collegati all'attività dell'ICCU"))
        & ~pl.col("stato-registrazione").str.starts_with(pl.lit("Biblioteca confluita"))
        & ~(pl.col("indirizzo").eq("") & pl.col("latitudine").is_null() & pl.col("longitudine").is_null())
        & pl.col("comune").ne("")
    )
    complete = complete.drop("accesso", "anno-censimento", "stato-registrazione")

    # ── contacts ──────────────────────────────────────────────────────────────

    def _flat(tipo, col):
        return complete.select(
            "codice-isil", pl.lit(tipo).alias("tipo"), pl.col(col).alias("valore"), pl.lit(None).alias("note")
        )

    contatti = pl.concat(
        [
            complete.explode("contatti").select("codice-isil", "contatti").unnest("contatti"),
            _flat("Telefono", "telefono"),
            _flat("E-mail", "email"),
            _flat("Fax", "fax"),
            _flat("Url", "url"),
            _flat(None, "contatto"),
        ]
    )

    print("Contacts before cleanup:", len(contatti))

    contatti = contatti.filter(pl.col("valore").ne_missing(None))
    contatti = contatti.with_columns(pl.col("valore").str.strip_chars(';:"/ ('))
    contatti = _strip_strings(contatti)
    contatti = contatti.with_columns(pl.col("valore").str.replace_all("^\\+39\\s*$", ""))
    contatti = contatti.with_columns(pl.col("valore").str.replace_all(".*\\+54.*", ""))
    contatti = contatti.filter(pl.col("valore").ne(""))
    contatti = contatti.filter(
        pl.col("tipo").ne_missing("Telex") & pl.col("valore").str.contains("^\\d{1,6}($| ?[A-Z].+$)").not_()
    )

    # urls
    contatti = contatti.with_columns(pl.col("valore").str.replace_all("h+tt+p(s)?[;:]//+\\s*", "http${1}://"))
    contatti = contatti.with_columns(
        pl.when(pl.col("valore").str.to_lowercase().str.contains("^(http|www|[^/]+.(it|eu|com|org|net|site)(/|$))"))
        .then(pl.lit("Url"))
        .otherwise("tipo")
        .alias("tipo")
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("valore").str.contains("facebook", literal=True))
        .then(pl.lit("facebook"))
        .when(pl.col("valore").str.contains("instagram", literal=True))
        .then(pl.lit("instagram"))
        .when(pl.col("valore").str.contains("twitter", literal=True))
        .then(pl.lit("twitter"))
        .otherwise("tipo")
        .alias("tipo")
    )

    # email / pec
    contatti = contatti.with_columns(
        pl.when(
            pl.col("valore")
            .str.to_lowercase()
            .str.contains(".+@[a-z0-9\\.-]*(pec|legalmail|(posta)?cert(ificata)?)[a-z0-9\\.-]*\\.[a-z0-9\\.-]+$")
        )
        .then(pl.lit("PEC"))
        .when(pl.col("valore").str.to_lowercase().str.contains("[^@]*pec[^@]*@[a-z0-9\\.-]+\\.[a-z0-9\\.-]+$"))
        .then(pl.lit("PEC"))
        .when(pl.col("valore").str.contains(".+@.+"))
        .then(pl.lit("E-mail"))
        .otherwise("tipo")
        .alias("tipo")
    )

    # phone / fax
    contatti = contatti.with_columns(pl.col("valore").str.replace_all("^\\(?\\+\\s*3\\s*9[\\.:;\\s\\)]*", "+39 "))
    contatti = contatti.with_columns(
        pl.when(pl.col("valore").str.contains("^(00|\\+)39") & pl.col("tipo").ne_missing("Fax"))
        .then(pl.lit("Telefono"))
        .when(pl.col("valore").str.contains("^[03][\\d /-]{6,26}$") & pl.col("tipo").eq_missing(None))
        .then(pl.lit("Telefono"))
        .otherwise("tipo")
        .alias("tipo")
    )
    contatti = contatti.with_columns(
        pl.col("valore").str.replace_all("^\\+39\\s*\\+39\\s*", "+39 ").str.replace_all("^\\+37\\s*", "37")
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").ne_missing("Telefono") & pl.col("tipo").ne_missing("Fax"))
        .then("valore")
        .otherwise(pl.col("valore").str.replace_all("[\\. \\(\\)]", "").str.replace("^(00|\\+)39", "+39 "))
        .alias("valore")
    )
    contatti = contatti.with_columns(
        pl.when(
            (pl.col("tipo").ne_missing("Telefono") & pl.col("tipo").ne_missing("Fax"))
            | pl.col("valore").str.starts_with("+39")
        )
        .then("valore")
        .otherwise(pl.col("valore").str.replace("^", "+39 "))
        .alias("valore")
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("valore").str.contains("^\\+39 .+(int|dig)"))
        .then(pl.col("valore").str.extract("(int(erno)?|digitare)(\\d+)", 0))
        .otherwise("note")
        .alias("note"),
        pl.when(pl.col("valore").str.contains("^\\+39 .+(int|dig)"))
        .then(pl.col("valore").str.replace("(int(erno)?|digitare)(\\d+)$", ""))
        .otherwise("valore")
        .alias("valore"),
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").ne_missing("Telefono") & pl.col("tipo").ne_missing("Fax"))
        .then("valore")
        .otherwise(pl.col("valore").str.replace(" (\\d{1,6})(/|-)", " ${1}"))
        .alias("valore")
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("valore").str.contains("^\\+39 .+fax$")).then(pl.lit("Fax")).otherwise("tipo").alias("tipo"),
        pl.when(pl.col("valore").str.contains("^\\+39 .+fax$"))
        .then(pl.col("valore").str.replace("fax$", ""))
        .otherwise("valore")
        .alias("valore"),
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("note").str.contains("^[Ff]ax1?$")).then(None).otherwise("note").alias("note")
    )
    contatti = contatti.filter(
        ~(
            (pl.col("tipo").eq_missing("Fax") | pl.col("tipo").eq_missing("Telefono"))
            & pl.col("valore").str.ends_with("omune")
        )
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").eq_missing("Telefono") | pl.col("tipo").eq_missing("Fax"))
        .then(
            pl.col("valore").map_elements(
                lambda n: pn.format_number(pn.parse(n, "IT"), pn.PhoneNumberFormat.INTERNATIONAL), return_dtype=str
            )
        )
        .otherwise("valore")
    )
    phone_values = contatti.filter(pl.col("tipo").eq_missing("Telefono")).select("valore").unique()
    contatti = contatti.filter(~(pl.col("tipo").eq_missing("Fax") & pl.col("valore").is_in(phone_values)))

    # socials
    contatti = contatti.with_columns(
        pl.when(
            pl.col("valore").str.starts_with("@")
            & pl.col("note").str.to_lowercase().str.contains("instagram", literal=True)
        )
        .then(pl.lit("instagram"))
        .when(
            pl.col("valore").str.starts_with("@")
            & pl.col("note").str.to_lowercase().str.contains("twitter", literal=True)
        )
        .then(pl.lit("twitter"))
        .otherwise("tipo")
        .alias("tipo")
    )
    contatti = contatti.filter(
        (
            pl.col("tipo").eq_missing("instagram") & pl.col("valore").str.contains("/invites/contact", literal=True)
        ).not_()
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").eq_missing("twitter"))
        .then(pl.col("valore").str.replace("^(@|(https://)?(www\\.)?twitter\\.com/)", "").str.replace("(/|\\?).*$", ""))
        .when(pl.col("tipo").eq_missing("instagram"))
        .then(
            pl.col("valore").str.replace("^(@|(https://)?(www\\.)?instagram\\.com/)", "").str.replace("(/|\\?).*$", "")
        )
        .otherwise("valore")
        .alias("valore")
    )
    contatti = contatti.with_columns(
        pl.col("note")
        .str.replace("^([Pp]agina|[Gg]ruppo|[Pp]rofilo)?\\s*([Ff]ace[Bb]+oo?k|[Ii]nstagram|[Tt]witter)$", "")
        .replace("", None)
    )

    contatti = contatti.filter(~pl.col("valore").str.contains("^([\\d]{1,3}[\\./]){4}/"))
    contatti = contatti.unique(subset=["codice-isil", "tipo", "valore"]).sort(
        "codice-isil", "tipo", "valore", nulls_last=True
    )
    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").eq_missing("Url"))
        .then(pl.lit("contact_website"))
        .when(pl.col("tipo").eq_missing("E-mail"))
        .then(pl.lit("contact_email"))
        .when(pl.col("tipo").eq_missing("PEC"))
        .then(pl.lit("contact_pec"))
        .when(pl.col("tipo").eq_missing("Fax"))
        .then(pl.lit("contact_fax"))
        .when(pl.col("tipo").eq_missing("Telefono"))
        .then(pl.lit("contact_phone"))
        .when(pl.col("tipo").eq_missing("facebook"))
        .then(pl.lit("contact_facebook"))
        .when(pl.col("tipo").eq_missing("instagram"))
        .then(pl.lit("contact_instagram"))
        .when(pl.col("tipo").eq_missing("twitter"))
        .then(pl.lit("contact_twitter"))
        .otherwise("tipo")
        .alias("tipo")
    )
    contatti = contatti.filter(pl.col("tipo").ne_missing(None))
    print("Contacts after cleanup & deduplication:", len(contatti))
    contatti_grouped = (
        contatti.group_by("codice-isil", "tipo").agg("valore", "note").pivot(on="tipo", index="codice-isil")
    )

    complete = complete.drop("contatti", "contatto", "url", "fax", "email", "telefono")
    complete = complete.join(contatti_grouped, on="codice-isil", how="left", validate="1:1")

    # ── address parsing ────────────────────────────────────────────────────────

    snc = "s\\.?n\\.?c?"
    km = f"[Kk][Mm]\\.?\\s*(\\d+([\\.,]\\d+)?)\\s*({snc})?"
    hn = f"({snc}|{km}|\\d+(|\\s*[/-]?[/\\sa-nrRA-N0-9]+|/?\\s*bis( B)?|\\s*rosso))"
    additional_info = (
        "("
        "([\\(,]\\s*)?([Cc]/[Oo]|[Cc]/da|[Pp]resso|[Gg]ià|[Ii]nt[\\.]?(erno)?)\\s*.+"
        "|\\s*[–-][^–-]*"  # noqa: RUF001  (EN dash is intentional — matches Italian address dashes)
        "|\\([^\\)]+\\)"
        "|([Ee]d(ificio)?|[Pp]alazz[io](na)?)\\s[A-Za-z0-9].+"
        "|piano (terra|primo|secondo|[0-9]).+"
        ")"
    )
    complete = complete.with_columns(
        pl.col("indirizzo").str.extract(f"({additional_info})$", 1).alias("address_more_info"),
        pl.col("indirizzo")
        .str.extract(f"\\s*,?\\s*{hn}?\\s*({additional_info})?$", 1)
        .str.replace(km, "km ${1}")
        .str.replace(snc, "snc")
        .str.replace("[\\.,]", ",")
        .alias("address_housenumber"),
        pl.col("indirizzo").str.replace(f"\\s*,?\\s*{hn}?\\s*({additional_info})?$", "").alias("address_street"),
    )

    print("Libraries after cleanup:", len(complete))

    list_cols = [
        "denominazioni-precedenti",
        "denominazioni-alternative",
        *[
            f"valore_contact_{t}"
            for t in ("website", "email", "pec", "fax", "phone", "facebook", "instagram", "twitter")
        ],
        *[f"note_contact_{t}" for t in ("website", "email", "pec", "fax", "phone", "facebook", "instagram", "twitter")],
    ]
    complete.with_columns(
        pl.col(col).list.join(";").replace("", None) for col in list_cols if col in complete.columns
    ).write_csv(CLEAN_CSV)
    print(f"Written: {CLEAN_CSV}")


def main() -> None:
    args = parse_args("Step 2: join and clean ICCU source data → clean.csv")
    run(args.overwrite)


if __name__ == "__main__":
    main()
