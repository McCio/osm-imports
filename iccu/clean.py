"""Join ICCU source files, normalise contacts and addresses, write clean.csv."""

import re
import sys

import pandas as pd
import phonenumbers as pn
import polars as pl

from iccu.common import CLEAN_CSV, SOURCE_DIR, parse_args


def _split_phone_variants(val: str) -> list[str]:
    """Split compound phone values, reconstructing short-suffix variants.

    '+39 0812536361;6140;6073'  → ['+39 0812536361', '+39 0812536140', '+39 0812536073']
    '+39 0817743166–0812581232' → ['+39 0817743166', '+39 0812581232']  (long: separate number)
    """
    if not val:
        return []
    val = val.replace(";", "/").replace("–", "/").replace("\xa0", "/")
    val = re.sub(r"(\d{7,})-(\d)", r"\1/\2", val)
    parts = val.split("/")
    if len(parts) == 1:
        return [val] if val.strip() else []

    result: list[str] = []
    base = parts[0].strip()
    base_number = base[4:] if base.startswith("+39 ") else base
    base_digits = "".join(c for c in base_number if c.isdigit())
    if base:
        result.append(base)

    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue
        part_digits = "".join(c for c in part if c.isdigit())
        n = len(part_digits)
        if n == 0:
            continue
        if n >= 7 or len(base_digits) <= n:
            result.append(part if part.startswith("+39") else "+39 " + part)
        else:
            result.append("+39 " + base_digits[:-n] + part_digits)

    return result


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
    territorio = pl.read_csv(SOURCE_DIR / "territorio.csv", separator=";", schema_overrides={"cap": pl.Utf8}).drop(
        "denominazione", "codice-sbn", "codice istat comune", "codice istat provincia"
    )
    territorio = _strip_strings(territorio)
    if "cap" in territorio.columns:
        _pad_cap = territorio.filter(
            pl.col("cap").is_not_null()
            & pl.col("cap").str.contains(r"^\d{1,4}$")
        )
        if len(_pad_cap):
            print(f"  [warn] {len(_pad_cap)} cap values have <5 digits (zero-padded):", file=sys.stderr)
            for _r in _pad_cap.select("codice-isil", "cap").to_dicts():
                print(f"    {_r['codice-isil']}  cap={_r['cap']!r}", file=sys.stderr)
        _null_cap = territorio.filter(
            pl.col("cap").is_not_null()
            & ~pl.col("cap").str.contains(r"^\d{1,5}$")
        )
        if len(_null_cap):
            print(f"  [warn] {len(_null_cap)} cap values are non-numeric (nulled):", file=sys.stderr)
            for _r in _null_cap.select("codice-isil", "cap").to_dicts():
                print(f"    {_r['codice-isil']}  cap={_r['cap']!r}", file=sys.stderr)
        territorio = territorio.with_columns(
            pl.when(pl.col("cap").str.contains(r"^\d{5}$"))
            .then(pl.col("cap"))
            .when(pl.col("cap").str.contains(r"^\d{1,4}$"))
            .then(pl.col("cap").str.zfill(5))
            .otherwise(None)
            .alias("cap")
        )
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
        pl.when(wheelchair_access.eq("Accessibile"))
        .then(pl.lit("yes"))
        .when(wheelchair_access.eq("Parzialmente accessibile"))
        .then(pl.lit("limited"))
        .when(wheelchair_access.eq("Non accessibile"))
        .then(pl.lit("no"))
        .alias("wheelchair"),
        pl.when(pl.col("stato-registrazione").eq_missing(None))
        .then(pl.lit("Biblioteca censita"))
        .otherwise("stato-registrazione")
        .alias("stato-registrazione"),
        pl.col("latitudine").str.replace(",", ".").cast(pl.Float64, strict=False),
        pl.col("longitudine").str.replace(",", ".").cast(pl.Float64, strict=False),
    )
    no_addr_no_coords = complete.filter(
        pl.col("indirizzo").eq("") & pl.col("latitudine").is_null() & pl.col("longitudine").is_null()
    )
    if len(no_addr_no_coords):
        print(f"  [warn] {len(no_addr_no_coords)} rows have no address and no coordinates (dropped):", file=sys.stderr)
        for row in no_addr_no_coords.select("codice-isil", "denominazione").to_dicts():
            print(f"    {row['codice-isil']}  {row['denominazione']!r}", file=sys.stderr)

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
    # Detect WhatsApp-labelled phone numbers: clone as "whatsapp" tipo before stripping suffix
    _wa_mask = (pl.col("tipo").eq_missing("Telefono") | pl.col("tipo").eq_missing("Fax")) & pl.col(
        "valore"
    ).str.to_lowercase().str.contains("whatsapp", literal=True)
    _wa_rows = contatti.filter(_wa_mask).with_columns(
        pl.lit("whatsapp").alias("tipo"),
        pl.col("valore").str.replace_all("(?i)\\s*whatsapp\\s*", "").str.strip_chars().alias("valore"),
    )
    contatti = pl.concat([contatti, _wa_rows])

    contatti = contatti.with_columns(
        pl.when(pl.col("tipo").eq_missing("Telefono") | pl.col("tipo").eq_missing("Fax"))
        .then(
            pl.col("valore")
            .str.replace_all("^\\+39 \\+0?39\\s*", "+39 ")  # fix double/variant prefix (+39 +039, +39 +39)
            .str.replace_all("O", "0")  # OCR fix: letter O misread as digit 0
            .str.replace_all("[A-Za-z].*$", "")  # strip trailing text (works even when followed by ,digit)
            .str.strip_chars()
        )
        .otherwise("valore")
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
    # Expand compound phone/fax values (multiple numbers joined by "/") into separate rows
    _phone_fax = pl.col("tipo").eq_missing("Telefono") | pl.col("tipo").eq_missing("Fax")
    _phones = contatti.filter(_phone_fax)
    _others = contatti.filter(~_phone_fax)
    _phones = (
        _phones.with_columns(
            pl.col("valore").map_elements(_split_phone_variants, return_dtype=pl.List(str)).alias("valore")
        )
        .explode("valore")
        .with_columns(pl.col("valore").str.strip_chars())
        .filter(pl.col("valore").is_not_null() & pl.col("valore").ne(""))
    )
    contatti = pl.concat([_phones, _others])

    is_phone_or_fax = pl.col("tipo").eq_missing("Telefono") | pl.col("tipo").eq_missing("Fax")
    unparseable = is_phone_or_fax & (
        ~pl.col("valore").str.contains(r"^\+39 \d{5,}") | pl.col("valore").str.contains("?", literal=True)
    )
    for row in contatti.filter(unparseable).select("codice-isil", "tipo", "valore").to_dicts():
        print(f"  [drop] {row['tipo']} {row['codice-isil']} {row['valore']!r}: pattern mismatch", file=sys.stderr)
    contatti = contatti.filter(~unparseable)

    _parse_failures: list[tuple[str, str]] = []

    def _fmt_phone(n: str) -> str:
        # polars evaluates map_elements on every row; non-phone rows reach here but
        # are discarded by the surrounding when/then — return as-is for them
        if not n.startswith("+39 "):
            return n
        try:
            return pn.format_number(pn.parse(n, "IT"), pn.PhoneNumberFormat.INTERNATIONAL)
        except Exception as exc:
            _parse_failures.append((n, str(exc)))
            return n  # placeholder; filtered out below

    contatti = contatti.with_columns(
        pl.when(is_phone_or_fax).then(pl.col("valore").map_elements(_fmt_phone, return_dtype=str)).otherwise("valore")
    )
    if _parse_failures:
        bad_vals = {v for v, _ in _parse_failures}
        for val, err in sorted({(v, e) for v, e in _parse_failures}):
            print(f"  [drop] Telefono/Fax {val!r}: {err}", file=sys.stderr)
        contatti = contatti.filter(~(is_phone_or_fax & pl.col("valore").is_in(list(bad_vals))))
    phone_values = contatti.filter(pl.col("tipo").eq_missing("Telefono"))["valore"].unique()
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
        .when(pl.col("tipo").eq_missing("whatsapp"))
        .then(pl.lit("contact_whatsapp"))
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
        .str.replace_all(r"(\d+)/\s*([a-zA-Z])\b", "${1}${2}")
        .str.to_lowercase()
        .alias("address_housenumber"),
        pl.col("indirizzo").str.replace(f"\\s*,?\\s*{hn}?\\s*({additional_info})?$", "").alias("address_street"),
    )

    print("Libraries after cleanup:", len(complete))

    list_cols = [
        "denominazioni-precedenti",
        "denominazioni-alternative",
        *[
            f"valore_contact_{t}"
            for t in ("website", "email", "pec", "fax", "phone", "facebook", "instagram", "twitter", "whatsapp")
        ],
        *[
            f"note_contact_{t}"
            for t in ("website", "email", "pec", "fax", "phone", "facebook", "instagram", "twitter", "whatsapp")
        ],
    ]
    result = complete.with_columns(
        pl.col(col).list.join(";").replace("", None) for col in list_cols if col in complete.columns
    )
    result.write_csv(CLEAN_CSV)
    print(f"Written: {CLEAN_CSV}")

    no_coords = result.filter(pl.col("latitudine").is_null() | pl.col("longitudine").is_null())
    if len(no_coords):
        print(f"  [warn] {len(no_coords)} rows have address but no coordinates (will be skipped in export/conflate):", file=sys.stderr)
        for row in no_coords.select("codice-isil", "denominazione", "indirizzo").to_dicts():
            print(f"    {row['codice-isil']}  {row['denominazione']!r}  {row['indirizzo']!r}", file=sys.stderr)


def main() -> None:
    args = parse_args("Step 2: join and clean ICCU source data → clean.csv")
    run(args.overwrite)


if __name__ == "__main__":
    main()
